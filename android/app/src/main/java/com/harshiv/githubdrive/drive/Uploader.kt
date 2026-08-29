package com.harshiv.githubdrive.drive

import android.content.Context
import android.net.Uri
import com.harshiv.githubdrive.core.Format
import com.harshiv.githubdrive.core.PyJson
import com.harshiv.githubdrive.github.GitHubClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import kotlin.coroutines.coroutineContext
import kotlin.math.min

/**
 * Write side: turns a set of picked files into one GitHub Release laid out exactly the way
 * `storage.py` lays it out, so the web app can read back what the phone uploaded.
 *
 * This build writes `file-assets` archives only - no client-side encryption and no auto-bundling.
 * Both are additive: readers key off `storage_mode` and the per-item `encrypted` flag.
 */
class Uploader(
    private val context: Context,
    private val client: GitHubClient
) {

    data class Progress(
        val completedItems: Int,
        val totalItems: Int,
        val bytesSent: Long,
        val totalBytes: Long,
        val currentName: String
    )

    class UploadException(message: String, cause: Throwable? = null) : RuntimeException(message, cause)

    suspend fun upload(
        sourceName: String,
        items: List<UploadItem>,
        virtualFolders: List<String> = emptyList(),
        resumeTag: String? = null,
        onProgress: (Progress) -> Unit = {}
    ): ArchiveSummary = withContext(Dispatchers.IO) {
        if (items.isEmpty()) throw UploadException("No files were found to upload.")

        val entries = Format.sortedByPath(items) { it.relativePath }
        val paths = entries.map { it.relativePath }
        val totalBytes = entries.sumOf { it.size }
        val sourceType = if (entries.size == 1 && !entries[0].relativePath.contains('/')) "file" else "directory"

        // ---- resolve the release we are writing into -------------------------------------
        var archiveId = Format.newArchiveId()
        var createdAt = Format.nowUtcIso()
        var release: JSONObject? = null

        if (!resumeTag.isNullOrEmpty()) {
            val existing = client.getReleaseByTag(resumeTag)
                ?: throw UploadException("Could not find the archive to resume.")
            val existingMeta = Format.decodeArchiveBody(existing.optString("body"))
                ?: throw UploadException("Release $resumeTag is not a GitHub Drive archive.")
            if (existingMeta.optBoolean("encrypted", false)) {
                throw UploadException("That archive is encrypted; this app cannot add to it yet.")
            }
            val existingMode = existingMeta.optString("storage_mode", Format.STORAGE_MODE_FILE_ASSETS)
            if (existingMode != Format.STORAGE_MODE_FILE_ASSETS) {
                throw UploadException("Resuming into a bundled archive is not supported.")
            }
            archiveId = existingMeta.optString("archive_id").ifEmpty { archiveId }
            createdAt = existingMeta.optString("created_at").ifEmpty { createdAt }
            release = existing
        }

        val tag = Format.tagFor(archiveId)
        val meta = buildMeta(archiveId, createdAt, sourceName, sourceType, paths, virtualFolders)
        val title = Format.titleFor(sourceName, entries.size)

        if (release == null) {
            release = client.createRelease(
                tag = tag,
                name = title,
                body = Format.encodeArchiveBody(meta)
            )
        } else {
            client.updateRelease(release.optLong("id"), title, Format.encodeArchiveBody(meta))
        }
        val releaseId = release.optLong("id")

        // Snapshot taken once; concurrent name clashes fall back to GitHub's already_exists handling.
        val existingAssets = client.listReleaseAssets(releaseId).associateBy { it.optString("name") }

        // ---- upload each file ------------------------------------------------------------
        val manifestItems = ArrayList<Map<String, Any?>>(entries.size)
        var bytesSent = 0L

        entries.forEachIndexed { order, item ->
            coroutineContext.ensureActive()
            onProgress(Progress(order, entries.size, bytesSent, totalBytes, item.relativePath))

            val plan = planChunks(order, item)
            val allPresent = plan.all { existingAssets.containsKey(it.assetName) }

            val parts: List<Map<String, Any?>>
            val sha: String
            val contentType: String

            if (allPresent) {
                // Whole-entry resume skip: never read, never hashed, matching the Python behaviour.
                val first = existingAssets.getValue(plan[0].assetName)
                contentType = first.optString("content_type", "application/octet-stream")
                sha = ""
                parts = plan.map { chunk ->
                    val asset = existingAssets.getValue(chunk.assetName)
                    partMap(chunk.index, chunk.assetName, asset.optLong("id"), asset.optLong("size", 0L))
                }
                bytesSent += item.size
            } else {
                contentType = Format.guessContentType(item.relativePath)
                sha = if (item.size in 1..SHA_MAX_BYTES) sha256(item.uri) else ""
                val built = ArrayList<Map<String, Any?>>(plan.size)
                for (chunk in plan) {
                    coroutineContext.ensureActive()
                    val already = existingAssets[chunk.assetName]
                    if (already != null) {
                        built.add(
                            partMap(
                                chunk.index,
                                chunk.assetName,
                                already.optLong("id"),
                                already.optLong("size", 0L)
                            )
                        )
                        bytesSent += chunk.length
                        continue
                    }
                    val baseBytes = bytesSent
                    val asset = client.uploadAssetStream(
                        releaseId = releaseId,
                        assetName = chunk.assetName,
                        contentType = contentType,
                        contentLength = chunk.length,
                        onProgress = { sentInChunk ->
                            onProgress(
                                Progress(
                                    order,
                                    entries.size,
                                    baseBytes + sentInChunk,
                                    totalBytes,
                                    item.relativePath
                                )
                            )
                        }
                    ) { openAt(item.uri, chunk.offset) }
                    built.add(
                        partMap(
                            chunk.index,
                            chunk.assetName,
                            asset.optLong("id"),
                            asset.optLong("size", chunk.length)
                        )
                    )
                    bytesSent = baseBytes + chunk.length
                }
                parts = built
            }

            manifestItems.add(
                itemMap(
                    order = order,
                    assetName = plan[0].assetName,
                    assetId = (parts[0]["asset_id"] as Number).toLong(),
                    relativePath = item.relativePath,
                    originalSize = item.size,
                    sha256 = sha,
                    contentType = contentType,
                    parts = parts
                )
            )
        }

        // ---- manifest last, always rewritten ---------------------------------------------
        existingAssets[Format.MANIFEST_ASSET_NAME]?.let { stale ->
            runCatching { client.deleteAsset(stale.optLong("id")) }
        }
        val manifest = buildManifest(archiveId, createdAt, sourceName, sourceType, entries.size, manifestItems)
        client.uploadAssetBytes(
            releaseId = releaseId,
            assetName = Format.MANIFEST_ASSET_NAME,
            payload = PyJson.indented(manifest).toByteArray(Charsets.UTF_8),
            contentType = "application/json"
        )

        // ---- cover, best effort ----------------------------------------------------------
        if (!existingAssets.containsKey(Format.COVER_ASSET_NAME)) {
            entries.firstOrNull { Format.classifyPath(it.relativePath) == "image" }?.let { imageItem ->
                Cover.buildJpeg(context, imageItem.uri)?.let { jpeg ->
                    runCatching {
                        client.uploadAssetBytes(releaseId, Format.COVER_ASSET_NAME, jpeg, "image/jpeg")
                    }
                }
            }
        }

        onProgress(Progress(entries.size, entries.size, totalBytes, totalBytes, ""))

        val finalRelease = client.getRelease(releaseId)
        ArchiveSummary.from(finalRelease, Format.decodeArchiveBody(finalRelease.optString("body")) ?: JSONObject())
    }

    // ------------------------------------------------------------------ metadata

    private fun buildMeta(
        archiveId: String,
        createdAt: String,
        sourceName: String,
        sourceType: String,
        paths: List<String>,
        virtualFolders: List<String>
    ): LinkedHashMap<String, Any?> {
        val meta = LinkedHashMap<String, Any?>()
        meta["storage_format"] = Format.STORAGE_FORMAT
        meta["metadata_version"] = Format.METADATA_VERSION
        meta["created_at"] = createdAt
        meta["source_name"] = sourceName
        meta["source_type"] = sourceType
        meta["source_path"] = sourceName
        meta["total_items"] = paths.size
        meta["encrypted"] = false
        meta["storage_mode"] = Format.STORAGE_MODE_FILE_ASSETS
        meta["kinds"] = Format.classifyCounts(paths)
        meta["cover_asset_name"] =
            if (paths.any { Format.classifyPath(it) == "image" }) Format.COVER_ASSET_NAME else null
        if (virtualFolders.isNotEmpty()) {
            meta["virtual_folders"] = expandFolders(virtualFolders, paths)
        }
        meta["archive_id"] = archiveId
        return meta
    }

    /** `_normalize_virtual_folders` - every ancestor of every folder and of every file's parent. */
    private fun expandFolders(folders: List<String>, paths: List<String>): List<String> {
        val out = sortedSetOf<String>()
        for (folder in folders) {
            val normalized = runCatching { Format.normalizeFolderPath(folder) }.getOrDefault("")
            if (normalized.isEmpty()) continue
            out.addAll(Format.folderAncestors(normalized))
        }
        for (path in paths) {
            if (!path.contains('/')) continue
            out.addAll(Format.folderAncestors(path.substringBeforeLast('/')))
        }
        return out.toList()
    }

    private fun buildManifest(
        archiveId: String,
        createdAt: String,
        sourceName: String,
        sourceType: String,
        totalItems: Int,
        items: List<Map<String, Any?>>
    ): LinkedHashMap<String, Any?> {
        val manifest = LinkedHashMap<String, Any?>()
        manifest["storage_format"] = Format.STORAGE_FORMAT
        manifest["metadata_version"] = Format.METADATA_VERSION
        manifest["archive_id"] = archiveId
        manifest["created_at"] = createdAt
        manifest["source_name"] = sourceName
        manifest["source_type"] = sourceType
        manifest["source_path"] = sourceName
        manifest["total_items"] = totalItems
        manifest["encrypted"] = false
        manifest["storage_mode"] = Format.STORAGE_MODE_FILE_ASSETS
        manifest["items"] = items
        return manifest
    }

    /** `ArchiveItem` field order, which `asdict` preserves on the wire. */
    private fun itemMap(
        order: Int,
        assetName: String,
        assetId: Long,
        relativePath: String,
        originalSize: Long,
        sha256: String,
        contentType: String,
        parts: List<Map<String, Any?>>
    ): LinkedHashMap<String, Any?> {
        val item = LinkedHashMap<String, Any?>()
        item["order"] = order
        item["asset_name"] = assetName
        item["asset_id"] = assetId
        item["relative_path"] = relativePath
        item["original_size"] = originalSize
        item["source_sha256"] = sha256
        item["encrypted"] = false
        item["content_type"] = contentType
        item["parts"] = parts
        item["members"] = emptyList<Any?>()
        return item
    }

    /** Fresh uploads write parts as order, asset_name, asset_id, size - in that order. */
    private fun partMap(order: Int, assetName: String, assetId: Long, size: Long): LinkedHashMap<String, Any?> {
        val part = LinkedHashMap<String, Any?>()
        part["order"] = order
        part["asset_name"] = assetName
        part["asset_id"] = assetId
        part["size"] = size
        return part
    }

    // ------------------------------------------------------------------ chunking + io

    private data class Chunk(val index: Int, val offset: Long, val length: Long, val assetName: String)

    private fun planChunks(order: Int, item: UploadItem): List<Chunk> {
        val threshold = Format.DEFAULT_CHUNK_BYTES
        if (item.size <= threshold) {
            return listOf(
                Chunk(0, 0L, item.size, Format.assetNameFor(order, item.relativePath, false))
            )
        }
        val chunks = ArrayList<Chunk>()
        var offset = 0L
        var remaining = item.size
        var index = 0
        while (remaining > 0) {
            val length = min(threshold, remaining)
            chunks.add(
                Chunk(index, offset, length, Format.partAssetNameFor(order, item.relativePath, index, false))
            )
            offset += length
            remaining -= length
            index++
        }
        return chunks
    }

    private fun openAt(uri: Uri, offset: Long): InputStream {
        val input = context.contentResolver.openInputStream(uri)
            ?: throw IOException("Could not open $uri")
        var remaining = offset
        while (remaining > 0) {
            val skipped = input.skip(remaining)
            if (skipped <= 0) {
                // Some providers refuse to skip; fall back to reading the bytes away.
                val buffer = ByteArray(min(remaining, 256L * 1024L).toInt())
                val read = input.read(buffer)
                if (read <= 0) throw IOException("Could not seek to $offset in $uri")
                remaining -= read
            } else {
                remaining -= skipped
            }
        }
        return input
    }

    /**
     * Hashes small and medium files only. The format explicitly tolerates an empty hash - resumed
     * entries write `""` - and re-reading a multi-gigabyte video off a phone just to hash it costs
     * more than the checksum is worth here.
     */
    private fun sha256(uri: Uri): String = try {
        val digest = MessageDigest.getInstance("SHA-256")
        context.contentResolver.openInputStream(uri)?.use { input ->
            val buffer = ByteArray(256 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                digest.update(buffer, 0, read)
            }
        }
        digest.digest().joinToString("") { "%02x".format(it) }
    } catch (e: Exception) {
        ""
    }

    companion object {
        private const val SHA_MAX_BYTES = 256L * 1024L * 1024L
    }
}
