package com.harshiv.githubdrive.transfer

import android.Manifest
import android.app.Notification
import android.app.PendingIntent
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
import androidx.work.ForegroundInfo
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import com.harshiv.githubdrive.GdApp
import com.harshiv.githubdrive.MainActivity
import com.harshiv.githubdrive.R
import com.harshiv.githubdrive.drive.Picking
import com.harshiv.githubdrive.drive.UploadItem
import com.harshiv.githubdrive.drive.Uploader
import com.harshiv.githubdrive.github.GitHubClient
import java.io.IOException
import java.time.Duration
import java.time.ZonedDateTime
import java.util.concurrent.TimeUnit

/**
 * Backs the camera roll up on its own.
 *
 * Runs once a night. WorkManager is the scheduler the phones this has to survive - Samsung,
 * Xiaomi - still honour after they freeze a background app, and a backup that happens while the
 * phone is on charge and on Wi-Fi is worth more than one that races the camera shutter.
 *
 * Each photo is uploaded as its own archive, exactly as picking it by hand would.
 */
object AutoUpload {

    private const val WORK_NAME = "gallery-backup"
    private const val WORK_NAME_NOW = "gallery-backup-now"

    /**
     * How many are taken per run.
     *
     * A run that still has work left when it hits this queues an immediate continuation rather
     * than holding one wake-up open indefinitely.
     */
    private const val BATCH = 25

    private const val NOTIFICATION_ID = 4211

    /**
     * Puts the nightly run in the calendar. Safe to call repeatedly.
     *
     * [force] replaces whatever is already scheduled, which is what a change to the settings
     * wants; the default leaves an existing run alone so opening the app cannot cancel a backup
     * that is mid-flight.
     */
    fun sync(context: Context, force: Boolean = false) {
        val app = context.applicationContext
        val prefs = (app as GdApp).prefs
        val work = WorkManager.getInstance(app)

        if (!prefs.autoUpload || !prefs.isSignedIn) {
            work.cancelUniqueWork(WORK_NAME)
            return
        }

        work.enqueueUniquePeriodicWork(
            WORK_NAME,
            if (force) ExistingPeriodicWorkPolicy.UPDATE else ExistingPeriodicWorkPolicy.KEEP,
            nightlyRequest(prefs.autoUploadWifiOnly)
        )
    }

    /** Runs a pass now rather than waiting for tonight - used when the setting is switched on. */
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

    /**
     * A daily run, first due at the next midnight.
     *
     * WorkManager owns the repeat rather than each run booking the next one: re-enqueuing the same
     * unique name from inside the worker would cancel the very run doing the enqueuing.
     *
     * It will not fire to the second either way. Doze holds jobs until a maintenance window and
     * the Wi-Fi and battery constraints have to be met, so this means "overnight", not "at
     * 00:00:00" - which for a backup is the better behaviour anyway.
     */
    private fun nightlyRequest(wifiOnly: Boolean) =
        PeriodicWorkRequestBuilder<Worker>(24, TimeUnit.HOURS)
            .setInitialDelay(millisUntilMidnight(), TimeUnit.MILLISECONDS)
            .setConstraints(constraints(wifiOnly))
            .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.MINUTES)
            .build()

    private fun millisUntilMidnight(): Long {
        val now = ZonedDateTime.now()
        val midnight = now.toLocalDate().plusDays(1).atStartOfDay(now.zone)
        return Duration.between(now, midnight).toMillis().coerceAtLeast(0L)
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

        override suspend fun getForegroundInfo(): ForegroundInfo = foregroundInfo()

        override suspend fun doWork(): Result {
            val app = applicationContext as GdApp
            val prefs = app.prefs

            if (!prefs.autoUpload) return Result.success()
            if (!canReadGallery(applicationContext)) return Result.success()
            val token = prefs.token ?: return Result.success()
            val owner = prefs.repoOwner ?: return Result.success()

            val pending = newMedia(prefs.autoUploadSince, prefs.autoUploadLastId)
            if (pending.isEmpty()) return Result.success()

            // A backup is a long upload, and a plain background worker is stopped after ten
            // minutes. Running in the foreground buys the time one large video needs.
            runCatching { setForeground(foregroundInfo()) }

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

            // A full batch means the camera roll probably has more waiting. Rather than hold this
            // wake-up open, hand the rest to a fresh run under the same constraints.
            if (pending.size >= BATCH) runNow(applicationContext)
            return Result.success()
        }

        private fun foregroundInfo(): ForegroundInfo {
            TransferService.ensureChannel(applicationContext)
            val open = PendingIntent.getActivity(
                applicationContext,
                0,
                android.content.Intent(applicationContext, MainActivity::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val notification = Notification.Builder(applicationContext, TransferService.CHANNEL_ID)
                .setContentTitle(applicationContext.getString(R.string.app_name))
                .setContentText("Backing up your photos")
                .setSmallIcon(android.R.drawable.stat_sys_upload)
                .setOngoing(true)
                .setOnlyAlertOnce(true)
                .setContentIntent(open)
                .build()

            return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                ForegroundInfo(
                    NOTIFICATION_ID,
                    notification,
                    android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC
                )
            } else {
                ForegroundInfo(NOTIFICATION_ID, notification)
            }
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
                            val uri = ContentUris.withAppendedId(collection, id)
                            // A reported zero is not proof of an empty file, so it is measured
                            // before being written off - otherwise the watermark would step past
                            // a real photo and never come back to it.
                            val size = Picking.trueSize(
                                applicationContext,
                                uri,
                                cursor.getLong(sizeColumn)
                            )
                            if (size <= 0L) continue
                            out.add(
                                Media(
                                    uri = uri,
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
