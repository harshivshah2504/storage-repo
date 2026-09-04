package com.harshiv.githubdrive.drive

import com.harshiv.githubdrive.core.Format

/**
 * Builds `_manifest.json`, the authoritative index inside an archive.
 *
 * Shared by the writer and by editing an existing archive, because both have to emit exactly the
 * same shape - key order included. The Flask app parses these bytes, so this is the one place the
 * layout is allowed to be decided.
 */
object Manifest {

    fun payload(
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
    fun item(
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
    fun part(order: Int, assetName: String, assetId: Long, size: Long): LinkedHashMap<String, Any?> {
        val part = LinkedHashMap<String, Any?>()
        part["order"] = order
        part["asset_name"] = assetName
        part["asset_id"] = assetId
        part["size"] = size
        return part
    }

    /**
     * Rebuilds an item from an entry that is already in an archive.
     *
     * The hash is dropped, matching the spec: a rewritten manifest carries `""` for
     * `source_sha256` because nothing re-reads the original file to compute one.
     */
    fun itemFrom(entry: ArchiveEntry): LinkedHashMap<String, Any?> {
        val parts = entry.parts.map { part(it.order, it.assetName, it.assetId, it.size) }
        return item(
            order = entry.order,
            assetName = entry.parts.firstOrNull()?.assetName.orEmpty(),
            assetId = entry.parts.firstOrNull()?.assetId ?: 0L,
            relativePath = entry.relativePath,
            originalSize = entry.originalSize,
            sha256 = "",
            contentType = entry.contentType,
            parts = parts
        )
    }
}
