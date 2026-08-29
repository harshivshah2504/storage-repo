package com.harshiv.githubdrive.transfer

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import com.harshiv.githubdrive.drive.DriveRepo
import com.harshiv.githubdrive.drive.ArchiveEntry
import com.harshiv.githubdrive.drive.UploadItem
import com.harshiv.githubdrive.drive.Uploader
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import java.util.concurrent.atomic.AtomicLong

enum class TransferKind { UPLOAD, DOWNLOAD }

enum class TransferState { RUNNING, DONE, FAILED, CANCELLED }

data class Transfer(
    val id: Long,
    val kind: TransferKind,
    val title: String,
    val detail: String = "",
    val bytesDone: Long = 0L,
    val bytesTotal: Long = 0L,
    val itemsDone: Int = 0,
    val itemsTotal: Int = 0,
    val state: TransferState = TransferState.RUNNING,
    val error: String? = null
) {
    val fraction: Float
        get() = if (bytesTotal <= 0L) 0f else (bytesDone.toFloat() / bytesTotal.toFloat()).coerceIn(0f, 1f)
}

/**
 * Owns every in-flight upload and download.
 *
 * Lives on an application-scoped coroutine scope so a transfer survives screen rotation and the
 * user navigating away, and pairs with [TransferService] so it also survives the app going to the
 * background mid-upload.
 */
object TransferManager {

    private val scope = CoroutineScope(SupervisorJob())

    /**
     * Uploads run one at a time. Picking twenty files now starts twenty uploads, and twenty
     * concurrent release creations and asset streams earn a secondary rate limit from GitHub and
     * saturate a phone's uplink. Queued transfers stay cancellable while they wait.
     */
    private val uploadGate = Mutex()
    private val nextId = AtomicLong(1L)
    private val jobs = java.util.concurrent.ConcurrentHashMap<Long, Job>()

    private val _transfers = MutableStateFlow<List<Transfer>>(emptyList())
    val transfers: StateFlow<List<Transfer>> = _transfers

    val hasActive: Boolean get() = _transfers.value.any { it.state == TransferState.RUNNING }

    fun startUpload(
        context: Context,
        uploader: Uploader,
        sourceName: String,
        items: List<UploadItem>,
        virtualFolders: List<String> = emptyList(),
        onFinished: () -> Unit = {}
    ): Long {
        val appContext = context.applicationContext
        val id = nextId.getAndIncrement()
        val total = items.sumOf { it.size }
        put(
            Transfer(
                id = id,
                kind = TransferKind.UPLOAD,
                title = sourceName,
                detail = "${items.size} file${if (items.size == 1) "" else "s"}",
                bytesTotal = total,
                itemsTotal = items.size
            )
        )
        startService(appContext)

        val job = scope.launch {
            try {
                if (uploadGate.isLocked) update(id) { it.copy(detail = "Waiting its turn") }
                uploadGate.withLock {
                    uploader.upload(sourceName, items, virtualFolders) { progress ->
                        update(id) {
                            it.copy(
                                bytesDone = progress.bytesSent,
                                bytesTotal = progress.totalBytes,
                                itemsDone = progress.completedItems,
                                itemsTotal = progress.totalItems,
                                detail = progress.currentName.ifEmpty { it.detail }
                            )
                        }
                    }
                }
                update(id) { it.copy(state = TransferState.DONE, bytesDone = it.bytesTotal, detail = "Uploaded") }
            } catch (e: kotlinx.coroutines.CancellationException) {
                update(id) { it.copy(state = TransferState.CANCELLED, detail = "Cancelled") }
            } catch (e: Exception) {
                update(id) { it.copy(state = TransferState.FAILED, error = e.message ?: "Upload failed") }
            } finally {
                jobs.remove(id)
                onFinished()
                stopServiceIfIdle(appContext)
            }
        }
        jobs[id] = job
        return id
    }

    fun startDownload(
        context: Context,
        repo: DriveRepo,
        entry: ArchiveEntry,
        target: Uri,
        onFinished: () -> Unit = {}
    ): Long {
        val appContext = context.applicationContext
        val id = nextId.getAndIncrement()
        put(
            Transfer(
                id = id,
                kind = TransferKind.DOWNLOAD,
                title = entry.name,
                detail = "Saving to your device",
                bytesTotal = entry.originalSize,
                itemsTotal = 1
            )
        )
        startService(appContext)

        val job = scope.launch {
            try {
                val stream = appContext.contentResolver.openOutputStream(target)
                    ?: throw IllegalStateException("Could not open the destination file.")
                stream.use { output ->
                    repo.downloadEntry(entry, output) { done ->
                        update(id) { it.copy(bytesDone = done) }
                    }
                }
                update(id) {
                    it.copy(state = TransferState.DONE, itemsDone = 1, bytesDone = it.bytesTotal, detail = "Saved")
                }
            } catch (e: kotlinx.coroutines.CancellationException) {
                update(id) { it.copy(state = TransferState.CANCELLED, detail = "Cancelled") }
            } catch (e: Exception) {
                update(id) { it.copy(state = TransferState.FAILED, error = e.message ?: "Download failed") }
            } finally {
                jobs.remove(id)
                onFinished()
                stopServiceIfIdle(appContext)
            }
        }
        jobs[id] = job
        return id
    }

    fun cancel(id: Long) {
        jobs[id]?.cancel()
    }

    fun clearFinished() {
        _transfers.update { list -> list.filter { it.state == TransferState.RUNNING } }
    }

    /** Summary line the notification shows while anything is running. */
    fun activeSummary(): String {
        val active = _transfers.value.filter { it.state == TransferState.RUNNING }
        if (active.isEmpty()) return "Finishing up"
        val first = active.first()
        val verb = if (first.kind == TransferKind.UPLOAD) "Uploading" else "Downloading"
        return if (active.size == 1) "$verb ${first.title}" else "$verb ${active.size} items"
    }

    fun activeFractionPercent(): Int {
        val active = _transfers.value.filter { it.state == TransferState.RUNNING }
        if (active.isEmpty()) return 0
        val done = active.sumOf { it.bytesDone }
        val total = active.sumOf { it.bytesTotal }
        if (total <= 0L) return 0
        return ((done * 100L) / total).toInt().coerceIn(0, 100)
    }

    private fun put(transfer: Transfer) {
        _transfers.update { it + transfer }
    }

    private fun update(id: Long, block: (Transfer) -> Transfer) {
        _transfers.update { list -> list.map { if (it.id == id) block(it) else it } }
    }

    private fun startService(context: Context) {
        val intent = Intent(context, TransferService::class.java)
        try {
            context.startForegroundService(intent)
        } catch (e: Exception) {
            // Android 15 caps dataSync services at 6h/24h and refuses the start once the quota is
            // spent. The transfer itself still runs; it just loses the progress notification.
        }
    }

    private fun stopServiceIfIdle(context: Context) {
        if (!hasActive) {
            context.stopService(Intent(context, TransferService::class.java))
        }
    }
}
