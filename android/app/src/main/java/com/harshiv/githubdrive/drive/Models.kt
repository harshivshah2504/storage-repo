package com.harshiv.githubdrive.drive

import com.harshiv.githubdrive.core.Format
import org.json.JSONObject

/** One release that carries the github-drive marker, as shown in the archive grid. */
data class ArchiveSummary(
    val releaseId: Long,
    val tag: String,
    val title: String,
    val htmlUrl: String,
    val assetCount: Int,
    val totalAssetBytes: Long,
    val createdAt: String,
    val archiveId: String,
    val sourceName: String,
    val sourceType: String,
    val totalItems: Int,
    val encrypted: Boolean,
    val storageMode: String,
    val coverAssetName: String?,
    val kinds: Map<String, Int>
) {
    val isBundle: Boolean get() = storageMode == Format.STORAGE_MODE_BUNDLE_ASSETS

    companion object {
        fun from(release: JSONObject, meta: JSONObject): ArchiveSummary {
            val assets = release.optJSONArray("assets")
            var totalBytes = 0L
            var count = 0
            if (assets != null) {
                count = assets.length()
                for (i in 0 until assets.length()) {
                    totalBytes += assets.getJSONObject(i).optLong("size", 0L)
                }
            }
            val kinds = HashMap<String, Int>()
            meta.optJSONObject("kinds")?.let { obj ->
                for (key in Format.KIND_KEYS) kinds[key] = obj.optInt(key, 0)
            }
            val tag = release.optString("tag_name", "")
            return ArchiveSummary(
                releaseId = release.optLong("id"),
                tag = tag,
                title = release.optString("name").ifEmpty { tag },
                htmlUrl = release.optString("html_url", ""),
                assetCount = count,
                totalAssetBytes = totalBytes,
                createdAt = meta.optString("created_at").ifEmpty { release.optString("created_at", "") },
                archiveId = meta.optString("archive_id", ""),
                sourceName = meta.optString("source_name", tag),
                sourceType = meta.optString("source_type", "directory"),
                totalItems = meta.optInt("total_items", 0),
                encrypted = meta.optBoolean("encrypted", false),
                storageMode = meta.optString("storage_mode", Format.STORAGE_MODE_FILE_ASSETS),
                coverAssetName = meta.optString("cover_asset_name").takeIf { it.isNotEmpty() && it != "null" },
                kinds = kinds
            )
        }
    }
}

/** One chunk of a stored file. Reassembly is always by ascending [order], never array order. */
data class PartRef(
    val order: Int,
    val assetName: String,
    val assetId: Long,
    val size: Long
)

/**
 * A file inside an archive. In `bundle-assets` mode the parts belong to the enclosing zip and
 * [memberOf] points at the bundle entry that has to be fetched to get at this file.
 */
data class ArchiveEntry(
    val order: Int,
    val relativePath: String,
    val originalSize: Long,
    val encrypted: Boolean,
    val contentType: String,
    val parts: List<PartRef>,
    val isFolder: Boolean = false,
    val memberOf: BundleRef? = null
) {
    val name: String get() = relativePath.substringAfterLast('/')
    val parentPath: String get() = relativePath.substringBeforeLast('/', "")
    val kind: String get() = if (isFolder) "folder" else Format.classifyPath(relativePath)
    val totalWireBytes: Long get() = parts.sumOf { it.size }
}

/** The zip that holds a bundled archive's files. */
data class BundleRef(
    val relativePath: String,
    val parts: List<PartRef>,
    val encrypted: Boolean
)

/** Everything needed to browse one archive. */
data class ArchiveDetail(
    val summary: ArchiveSummary,
    val entries: List<ArchiveEntry>,
    val virtualFolders: List<String>,
    val storageMode: String,
    val encrypted: Boolean
) {
    val supportsFileDelete: Boolean get() = storageMode == Format.STORAGE_MODE_FILE_ASSETS

    /** Files and folders directly under [path] (use `""` for the archive root). */
    fun childrenOf(path: String): List<ArchiveEntry> {
        val prefix = if (path.isEmpty()) "" else "$path/"
        val folders = LinkedHashSet<String>()
        val files = ArrayList<ArchiveEntry>()

        for (entry in entries) {
            if (!entry.relativePath.startsWith(prefix)) continue
            val remainder = entry.relativePath.substring(prefix.length)
            if (remainder.isEmpty()) continue
            val slash = remainder.indexOf('/')
            if (slash >= 0) folders.add(remainder.substring(0, slash)) else files.add(entry)
        }
        for (folder in virtualFolders) {
            if (!folder.startsWith(prefix)) continue
            val remainder = folder.substring(prefix.length)
            if (remainder.isEmpty()) continue
            val slash = remainder.indexOf('/')
            folders.add(if (slash >= 0) remainder.substring(0, slash) else remainder)
        }

        val folderEntries = folders.sorted().map { folderName ->
            ArchiveEntry(
                order = -1,
                relativePath = prefix + folderName,
                originalSize = 0L,
                encrypted = false,
                contentType = "",
                parts = emptyList(),
                isFolder = true
            )
        }
        return folderEntries + files.sortedBy { it.relativePath }
    }
}
