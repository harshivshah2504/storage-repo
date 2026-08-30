package com.harshiv.githubdrive.transfer

import android.Manifest
import android.content.ContentUris
import android.content.Context
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.MediaStore
import androidx.core.content.ContextCompat
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.harshiv.githubdrive.GdApp
import com.harshiv.githubdrive.drive.UploadItem
import com.harshiv.githubdrive.drive.Uploader
import com.harshiv.githubdrive.github.GitHubClient
import java.io.IOException
import java.util.concurrent.TimeUnit

/**
 * Backs the camera roll up on its own.
 *
 * Deliberately a plain periodic worker rather than anything cleverer: the phones this has to
 * survive - Samsung, Xiaomi - freeze background apps within seconds, and WorkManager is the one
 * scheduler they still honour. It means a new photo is picked up within about a quarter of an hour
 * rather than instantly, which is the right trade for a backup.
 *
 * Each photo is uploaded as its own archive, exactly as picking it by hand would.
 */
object AutoUpload {

    private const val WORK_NAME = "gallery-backup"
    private const val WORK_NAME_NOW = "gallery-backup-now"

    /** How many are taken per run, so one wake-up cannot spend the whole battery. */
    private const val BATCH = 20

    /** Applies whatever the settings currently say. Safe to call repeatedly. */
    fun sync(context: Context) {
        val app = context.applicationContext
        val prefs = (app as GdApp).prefs
        val work = WorkManager.getInstance(app)

        if (!prefs.autoUpload || !prefs.isSignedIn) {
            work.cancelUniqueWork(WORK_NAME)
            return
        }

        val request = PeriodicWorkRequestBuilder<Worker>(15, TimeUnit.MINUTES)
            .setConstraints(constraints(prefs.autoUploadWifiOnly))
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 5, TimeUnit.MINUTES)
            .build()

        work.enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.UPDATE, request)
    }

    /** Runs a pass now, without waiting for the next window - used when the setting is switched on. */
    fun runNow(context: Context) {
        val app = context.applicationContext
        val prefs = (app as GdApp).prefs
        if (!prefs.autoUpload || !prefs.isSignedIn) return

        val request = OneTimeWorkRequestBuilder<Worker>()
            .setConstraints(constraints(prefs.autoUploadWifiOnly))
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 1, TimeUnit.MINUTES)
            .build()

        WorkManager.getInstance(app)
            .enqueueUniqueWork(WORK_NAME_NOW, ExistingWorkPolicy.REPLACE, request)
    }

    private fun constraints(wifiOnly: Boolean) = Constraints.Builder()
        .setRequiredNetworkType(if (wifiOnly) NetworkType.UNMETERED else NetworkType.CONNECTED)
        .setRequiresBatteryNotLow(true)
        .build()

    /** The permissions a backup needs, for the version of Android this is running on. */
    fun mediaPermissions(): Array<String> = when {
        Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU -> arrayOf(
            Manifest.permission.READ_MEDIA_IMAGES,
            Manifest.permission.READ_MEDIA_VIDEO
        )
        else -> arrayOf(Manifest.permission.READ_EXTERNAL_STORAGE)
    }

    /**
     * True when the camera roll is readable at all.
     *
     * Android 14 lets someone grant access to a hand-picked set of photos instead of all of them.
     * That counts: the backup then simply sees the pictures it was given.
     */
    fun canReadGallery(context: Context): Boolean {
        val granted = mediaPermissions().any { permission ->
            ContextCompat.checkSelfPermission(context, permission) == PackageManager.PERMISSION_GRANTED
        }
        if (granted) return true
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.UPSIDE_DOWN_CAKE) return false
        return ContextCompat.checkSelfPermission(
            context,
            Manifest.permission.READ_MEDIA_VISUAL_USER_SELECTED
        ) == PackageManager.PERMISSION_GRANTED
    }

    /** One picture or video waiting to go up. */
    private data class Media(
        val uri: Uri,
        val name: String,
        val size: Long,
        val dateAdded: Long,
        val id: Long
    )

    class Worker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {

        override suspend fun doWork(): Result {
            val app = applicationContext as GdApp
            val prefs = app.prefs

            if (!prefs.autoUpload) return Result.success()
            if (!canReadGallery(applicationContext)) return Result.success()
            val token = prefs.token ?: return Result.success()
            val owner = prefs.repoOwner ?: return Result.success()

            val pending = newMedia(prefs.autoUploadSince, prefs.autoUploadLastId)
            if (pending.isEmpty()) return Result.success()

            val uploader = Uploader(applicationContext, GitHubClient(token, owner, prefs.repoName))

            for (media in pending) {
                if (isStopped) return Result.retry()
                try {
                    uploader.upload(media.name, listOf(UploadItem(media.uri, media.name, media.size)))
                } catch (e: IOException) {
                    // The network went away mid-backup. Leave the watermark where it is and let
                    // WorkManager bring us back; nothing is lost and nothing uploads twice.
                    return Result.retry()
                } catch (e: Exception) {
                    // A file that cannot be read or that GitHub refuses must not wedge the queue
                    // forever, so it is stepped over rather than retried until the end of time.
                }
                prefs.autoUploadSince = media.dateAdded
                prefs.autoUploadLastId = media.id
            }
            return Result.success()
        }

        /**
         * Everything added to the camera roll after the watermark, oldest first.
         *
         * Images and videos share one id space in MediaStore, so the two collections can be merged
         * and walked with a single `(date_added, _id)` cursor.
         */
        private fun newMedia(since: Long, lastId: Long): List<Media> {
            val collections = listOf(
                MediaStore.Images.Media.EXTERNAL_CONTENT_URI,
                MediaStore.Video.Media.EXTERNAL_CONTENT_URI
            )
            val found = ArrayList<Media>()
            for (collection in collections) {
                found.addAll(query(collection, since, lastId))
            }
            return found
                .sortedWith(compareBy({ it.dateAdded }, { it.id }))
                .take(BATCH)
        }

        private fun query(collection: Uri, since: Long, lastId: Long): List<Media> {
            val columns = arrayOf(
                MediaStore.MediaColumns._ID,
                MediaStore.MediaColumns.DISPLAY_NAME,
                MediaStore.MediaColumns.SIZE,
                MediaStore.MediaColumns.DATE_ADDED
            )
            val selection = "${MediaStore.MediaColumns.DATE_ADDED} > ? OR " +
                "(${MediaStore.MediaColumns.DATE_ADDED} = ? AND ${MediaStore.MediaColumns._ID} > ?)"
            val args = arrayOf(since.toString(), since.toString(), lastId.toString())
            val order = "${MediaStore.MediaColumns.DATE_ADDED} ASC, ${MediaStore.MediaColumns._ID} ASC"

            val out = ArrayList<Media>()
            runCatching {
                applicationContext.contentResolver
                    .query(collection, columns, selection, args, order)
                    ?.use { cursor ->
                        val idColumn = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns._ID)
                        val nameColumn = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DISPLAY_NAME)
                        val sizeColumn = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.SIZE)
                        val dateColumn = cursor.getColumnIndexOrThrow(MediaStore.MediaColumns.DATE_ADDED)
                        while (cursor.moveToNext() && out.size < BATCH) {
                            val id = cursor.getLong(idColumn)
                            val size = cursor.getLong(sizeColumn)
                            if (size <= 0L) continue
                            out.add(
                                Media(
                                    uri = ContentUris.withAppendedId(collection, id),
                                    name = cursor.getString(nameColumn) ?: "photo-$id",
                                    size = size,
                                    dateAdded = cursor.getLong(dateColumn),
                                    id = id
                                )
                            )
                        }
                    }
            }
            return out
        }
    }
}
