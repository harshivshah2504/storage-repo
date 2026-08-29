package com.harshiv.githubdrive.drive

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.documentfile.provider.DocumentFile
import com.harshiv.githubdrive.core.Format

/** One file queued for upload, with the path it will occupy inside the archive. */
data class UploadItem(
    val uri: Uri,
    val relativePath: String,
    val size: Long
)

/** Turns what Android's pickers hand back into a sorted, collision-free list of upload items. */
object Picking {

    /** Files picked individually (or shared in from another app) sit flat at the archive root. */
    fun fromFiles(context: Context, uris: List<Uri>): List<UploadItem> {
        val used = HashSet<String>()
        val items = ArrayList<UploadItem>()
        for (uri in uris) {
            val (name, size) = queryNameAndSize(context, uri)
            val relativePath = uniquePath(used, sanitizeName(name))
            items.add(UploadItem(uri, relativePath, size))
        }
        return Format.sortedByPath(items) { it.relativePath }
    }

    /** A picked folder keeps its structure; the folder name becomes the archive name. */
    fun fromTree(context: Context, treeUri: Uri): Pair<String, List<UploadItem>> {
        val root = DocumentFile.fromTreeUri(context, treeUri)
            ?: return Pair("archive", emptyList())
        val sourceName = root.name ?: "archive"
        val items = ArrayList<UploadItem>()
        walk(root, "", items)
        return Pair(sourceName, Format.sortedByPath(items) { it.relativePath })
    }

    private fun walk(dir: DocumentFile, prefix: String, out: MutableList<UploadItem>) {
        for (child in dir.listFiles()) {
            val name = child.name ?: continue
            val path = if (prefix.isEmpty()) sanitizeName(name) else "$prefix/${sanitizeName(name)}"
            if (child.isDirectory) {
                walk(child, path, out)
            } else if (child.isFile) {
                out.add(UploadItem(child.uri, path, child.length()))
            }
        }
    }

    /** Every empty folder in the picked tree, so the archive can remember them. */
    fun emptyFolders(context: Context, treeUri: Uri): List<String> {
        val root = DocumentFile.fromTreeUri(context, treeUri) ?: return emptyList()
        val folders = ArrayList<String>()
        collectFolders(root, "", folders)
        return folders.sorted()
    }

    private fun collectFolders(dir: DocumentFile, prefix: String, out: MutableList<String>) {
        for (child in dir.listFiles()) {
            if (!child.isDirectory) continue
            val name = child.name ?: continue
            val path = if (prefix.isEmpty()) sanitizeName(name) else "$prefix/${sanitizeName(name)}"
            out.add(path)
            collectFolders(child, path, out)
        }
    }

    fun queryNameAndSize(context: Context, uri: Uri): Pair<String, Long> {
        var name = uri.lastPathSegment?.substringAfterLast('/') ?: "file"
        var size = -1L
        runCatching {
            context.contentResolver.query(uri, null, null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) {
                    val nameIndex = cursor.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                    if (nameIndex >= 0 && !cursor.isNull(nameIndex)) name = cursor.getString(nameIndex)
                    val sizeIndex = cursor.getColumnIndex(OpenableColumns.SIZE)
                    if (sizeIndex >= 0 && !cursor.isNull(sizeIndex)) size = cursor.getLong(sizeIndex)
                }
            }
        }
        if (size < 0) {
            size = runCatching {
                context.contentResolver.openAssetFileDescriptor(uri, "r")?.use { it.length }
            }.getOrNull()?.takeIf { it >= 0L } ?: measureByReading(context, uri)
        }
        return Pair(name, size)
    }

    /** Some providers report no size; the only honest answer then is to read it once. */
    private fun measureByReading(context: Context, uri: Uri): Long = runCatching {
        context.contentResolver.openInputStream(uri)?.use { input ->
            var total = 0L
            val buffer = ByteArray(256 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                total += read
            }
            total
        } ?: 0L
    }.getOrDefault(0L)

    private fun sanitizeName(raw: String): String {
        val cleaned = raw.replace('\\', '_').replace('/', '_').trim()
        return cleaned.ifEmpty { "file" }
    }

    private fun uniquePath(used: MutableSet<String>, candidate: String): String {
        if (used.add(candidate)) return candidate
        val stem = candidate.substringBeforeLast('.', candidate)
        val ext = candidate.substringAfterLast('.', "")
        var counter = 2
        while (true) {
            val next = if (ext.isEmpty()) "$stem ($counter)" else "$stem ($counter).$ext"
            if (used.add(next)) return next
            counter++
        }
    }
}
