package com.harshiv.githubdrive.drive

import com.harshiv.githubdrive.core.Format
import com.harshiv.githubdrive.github.GitHubClient
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.OutputStream
import java.util.zip.ZipFile

/** Read-side operations: list archives, browse one, fetch a file out of it, delete. */
class DriveRepo(private val client: GitHubClient, private val cacheDir: File) {

    class EncryptedArchiveException :
        RuntimeException("This archive is encrypted. Open it in the web app - the phone app cannot decrypt it yet.")

    // ------------------------------------------------------------------ listing

    suspend fun listArchives(page: Int, perPage: Int = 24): Pair<List<ArchiveSummary>, Boolean> {
        val (releases, hasMore) = client.listReleasesPage(page, perPage)
        val archives = ArrayList<ArchiveSummary>()
        for (release in releases) {
            // The marker line in the body is the filter, not the tag prefix.
            val meta = Format.decodeArchiveBody(release.optString("body")) ?: continue
            archives.add(ArchiveSummary.from(release, meta))
        }
        archives.sortByDescending { it.createdAt }
        return Pair(archives, hasMore)
    }

    // ------------------------------------------------------------------ one archive

    /**
     * Opens one archive.
     *
     * The grid listing already carried the release body and every asset, so the usual cost here is
     * a single request - the manifest - rather than a release fetch, an asset listing and then the
     * manifest in sequence. Assets embedded in a listing can lag an upload that has only just
     * finished, so anything the manifest references but the cached assets cannot satisfy triggers
     * one authoritative re-read.
     */
    suspend fun loadDetail(summary: ArchiveSummary): ArchiveDetail {
        val cached = if (summary.assets.isEmpty()) null else build(summary, summary.assets)
        if (cached != null && cached.complete) return cached.detail

        val assets = client.listReleaseAssets(summary.releaseId).map { AssetRef.from(it) }
        return build(summary, assets).detail
    }

    private class Built(val detail: ArchiveDetail, val complete: Boolean)

    private suspend fun build(summary: ArchiveSummary, assets: List<AssetRef>): Built {
        val assetsByName = assets.associateBy { it.name }

        val manifest = assetsByName[Format.MANIFEST_ASSET_NAME]?.let { asset ->
            runCatching {
                JSONObject(String(client.downloadAssetBytes(asset.id), Charsets.UTF_8))
            }.getOrNull()
        }

        // The manifest wins over the release body wherever they disagree.
        val storageMode = manifest?.optString("storage_mode")?.takeIf { it.isNotEmpty() }
            ?: summary.storageMode
        val encrypted = manifest?.optBoolean("encrypted", summary.encrypted) ?: summary.encrypted

        val built = when {
            manifest != null && storageMode == Format.STORAGE_MODE_BUNDLE_ASSETS ->
                bundleEntries(manifest, assetsByName, encrypted)
            manifest != null -> fileEntries(manifest, assetsByName)
            else -> Entries(fallbackEntries(assets, encrypted), dropped = 0)
        }

        val detail = ArchiveDetail(
            summary = summary.copy(storageMode = storageMode, encrypted = encrypted),
            entries = built.entries.sortedBy { it.relativePath },
            virtualFolders = summary.virtualFolders,
            storageMode = storageMode,
            encrypted = encrypted
        )
        return Built(detail, complete = manifest != null && built.dropped == 0)
    }

    /** Entries plus the count of manifest items whose assets could not be resolved. */
    private class Entries(val entries: List<ArchiveEntry>, val dropped: Int)

    private fun parseParts(item: JSONObject, assetsByName: Map<String, AssetRef>): List<PartRef> {
        val parts = ArrayList<PartRef>()
        item.optJSONArray("parts")?.let { array ->
            for (i in 0 until array.length()) {
                val part = array.getJSONObject(i)
                val name = part.optString("asset_name")
                // Parts whose asset has vanished from the release are dropped, as the web app does.
                val asset = assetsByName[name] ?: continue
                parts.add(
                    PartRef(
                        order = part.optInt("order", i),
                        assetName = name,
                        assetId = asset.id,
                        size = if (asset.size > 0L) asset.size else part.optLong("size", 0L)
                    )
                )
            }
        }
        if (parts.isEmpty()) {
            // Legacy single-asset item: synthesise one part from the top-level asset name.
            val name = item.optString("asset_name")
            assetsByName[name]?.let { asset ->
                parts.add(PartRef(0, name, asset.id, asset.size))
            }
        }
        return parts.sortedBy { it.order }
    }

    private fun fileEntries(
        manifest: JSONObject,
        assetsByName: Map<String, AssetRef>
    ): Entries {
        val items = manifest.optJSONArray("items") ?: return Entries(emptyList(), 0)
        val entries = ArrayList<ArchiveEntry>()
        var dropped = 0
        for (i in 0 until items.length()) {
            val item = items.getJSONObject(i)
            val parts = parseParts(item, assetsByName)
            if (parts.isEmpty()) {
                dropped++
                continue
            }
            entries.add(
                ArchiveEntry(
                    order = item.optInt("order", i),
                    relativePath = item.optString("relative_path"),
                    originalSize = item.optLong("original_size", 0L),
                    encrypted = item.optBoolean("encrypted", false),
                    contentType = item.optString("content_type", "application/octet-stream"),
                    parts = parts
                )
            )
        }
        return Entries(entries, dropped)
    }

    private fun bundleEntries(
        manifest: JSONObject,
        assetsByName: Map<String, AssetRef>,
        archiveEncrypted: Boolean
    ): Entries {
        val items = manifest.optJSONArray("items") ?: return Entries(emptyList(), 0)
        val entries = ArrayList<ArchiveEntry>()
        var dropped = 0
        for (i in 0 until items.length()) {
            val item = items.getJSONObject(i)
            val parts = parseParts(item, assetsByName)
            if (parts.isEmpty()) {
                dropped++
                continue
            }
            val bundle = BundleRef(
                relativePath = item.optString("relative_path"),
                parts = parts,
                encrypted = item.optBoolean("encrypted", archiveEncrypted)
            )
            val members = item.optJSONArray("members")
            if (members == null) {
                dropped++
                continue
            }
            for (m in 0 until members.length()) {
                val member = members.getJSONObject(m)
                entries.add(
                    ArchiveEntry(
                        order = m,
                        relativePath = member.optString("relative_path"),
                        originalSize = member.optLong("original_size", 0L),
                        encrypted = bundle.encrypted,
                        contentType = member.optString("content_type", "application/octet-stream"),
                        parts = emptyList(),
                        memberOf = bundle
                    )
                )
            }
        }
        return Entries(entries, dropped)
    }

    /** No manifest: decode names heuristically. Chunked files are not recoverable this way. */
    private fun fallbackEntries(
        assets: List<AssetRef>,
        archiveEncrypted: Boolean
    ): List<ArchiveEntry> {
        val entries = ArrayList<ArchiveEntry>()
        for (asset in assets) {
            var name = asset.name
            if (name == Format.MANIFEST_ASSET_NAME || name == Format.COVER_ASSET_NAME) continue
            val encrypted = name.endsWith(Format.ENCRYPTED_SUFFIX) || archiveEncrypted
            if (name.endsWith(Format.ENCRYPTED_SUFFIX)) {
                name = name.dropLast(Format.ENCRYPTED_SUFFIX.length)
            }
            var order = 0
            val dash = name.indexOf('-')
            if (dash > 0) {
                val head = name.substring(0, dash)
                if (head.isNotEmpty() && head.all { it.isDigit() }) {
                    order = head.toInt()
                    name = name.substring(dash + 1)
                }
            }
            entries.add(
                ArchiveEntry(
                    order = order,
                    relativePath = name.replace("__", "/"),
                    originalSize = asset.size,
                    encrypted = encrypted,
                    contentType = asset.contentType,
                    parts = listOf(PartRef(0, asset.name, asset.id, asset.size))
                )
            )
        }
        return entries
    }

    // ------------------------------------------------------------------ cover

    /** Fetches one asset's bytes. Used for covers and for the thumbnails inside an archive. */
    suspend fun assetBytes(assetId: Long): ByteArray? =
        runCatching { client.downloadAssetBytes(assetId) }.getOrNull()

    /**
     * A square thumbnail for one image inside an archive.
     *
     * Release assets have no thumbnail service, so the only way to show a picture is to fetch it
     * and shrink it here. That is worth doing once and never again: the 480px JPEG is kept in the
     * cache directory, so scrolling back through a folder costs nothing. Anything that is not a
     * plain, reasonably sized, unencrypted image is skipped rather than downloaded on spec.
     */
    suspend fun thumbnail(entry: ArchiveEntry): ByteArray? = withContext(Dispatchers.IO) {
        if (entry.encrypted || entry.isFolder) return@withContext null
        if (entry.kind != "image") return@withContext null
        if (entry.memberOf != null) return@withContext null
        val part = entry.parts.singleOrNull() ?: return@withContext null
        if (part.size > THUMB_SOURCE_MAX_BYTES) return@withContext null

        val cached = File(cacheDir, "thumb-${part.assetId}.jpg")
        if (cached.exists() && cached.length() > 0L) {
            return@withContext runCatching { cached.readBytes() }.getOrNull()
        }

        val source = runCatching { client.downloadAssetBytes(part.assetId) }.getOrNull()
            ?: return@withContext null
        val thumb = Cover.buildJpeg(source) ?: return@withContext null
        runCatching { cached.writeBytes(thumb) }
        thumb
    }

    fun clearThumbnailCache() {
        cacheDir.listFiles()?.forEach { file ->
            if (file.name.startsWith("thumb-")) file.delete()
        }
    }

    /** Cover for an archive whose listing did not carry its assets. */
    suspend fun coverBytes(releaseId: Long): ByteArray? = try {
        val cover = client.listReleaseAssets(releaseId)
            .firstOrNull { it.optString("name") == Format.COVER_ASSET_NAME }
        cover?.let { client.downloadAssetBytes(it.optLong("id")) }
    } catch (e: Exception) {
        null
    }

    // ------------------------------------------------------------------ download

    /**
     * Writes one archive entry to [output]. Progress is reported as bytes written so far.
     * Encrypted entries are refused in this build rather than written out as ciphertext.
     */
    suspend fun downloadEntry(
        entry: ArchiveEntry,
        output: OutputStream,
        onProgress: (Long) -> Unit = {}
    ) = withContext(Dispatchers.IO) {
        if (entry.encrypted) throw EncryptedArchiveException()

        val bundle = entry.memberOf
        if (bundle != null) {
            extractFromBundle(bundle, entry, output, onProgress)
            return@withContext
        }

        var written = 0L
        for (part in entry.parts) {
            client.downloadAssetToStream(part.assetId, output) { partBytes ->
                onProgress(written + partBytes)
            }
            written += part.size
        }
        output.flush()
    }

    private suspend fun extractFromBundle(
        bundle: BundleRef,
        entry: ArchiveEntry,
        output: OutputStream,
        onProgress: (Long) -> Unit
    ) {
        val zipFile = materializeBundle(bundle, onProgress)
        ZipFile(zipFile).use { zip ->
            val zipEntry = zip.getEntry(entry.relativePath)
                ?: throw IllegalStateException("${entry.relativePath} is missing from the bundle.")
            zip.getInputStream(zipEntry).use { input ->
                val buffer = ByteArray(256 * 1024)
                var total = 0L
                while (true) {
                    val read = input.read(buffer)
                    if (read <= 0) break
                    output.write(buffer, 0, read)
                    total += read
                    onProgress(total)
                }
            }
        }
        output.flush()
    }

    /**
     * Bundled archives store every file inside one zip, so a single member still needs the whole
     * zip on disk. It is cached so browsing several files in a row only pays for it once.
     */
    private suspend fun materializeBundle(bundle: BundleRef, onProgress: (Long) -> Unit): File {
        if (bundle.encrypted) throw EncryptedArchiveException()

        val expectedSize = bundle.parts.sumOf { it.size }
        val cacheKey = bundle.parts.joinToString("-") { it.assetId.toString() }.hashCode()
        val cached = File(cacheDir, "bundle-$cacheKey.zip")
        if (cached.exists() && cached.length() == expectedSize) return cached

        val temp = File(cacheDir, "bundle-$cacheKey.part")
        if (temp.exists()) temp.delete()
        var written = 0L
        for (part in bundle.parts) {
            client.downloadAssetTo(part.assetId, temp, append = true) { partBytes ->
                onProgress(written + partBytes)
            }
            written += part.size
        }
        if (cached.exists()) cached.delete()
        if (!temp.renameTo(cached)) throw IllegalStateException("Could not stage the bundle for reading.")
        return cached
    }

    // ------------------------------------------------------------------ mutate

    suspend fun deleteArchive(summary: ArchiveSummary) {
        client.deleteRelease(summary.releaseId)
        if (summary.tag.isNotEmpty()) client.deleteTag(summary.tag)
    }

    fun clearBundleCache() {
        cacheDir.listFiles()?.forEach { file ->
            if (file.name.startsWith("bundle-")) file.delete()
        }
    }

    companion object {
        /**
         * Above this, fetching the original just to shrink it costs more data than the picture is
         * worth on a phone connection. The file is still listed and still downloadable in full.
         */
        private const val THUMB_SOURCE_MAX_BYTES = 40L * 1024L * 1024L
    }
}
