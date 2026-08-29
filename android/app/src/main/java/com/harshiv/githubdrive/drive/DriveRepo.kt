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

    suspend fun loadDetail(releaseId: Long): ArchiveDetail {
        val release = client.getRelease(releaseId)
        val meta = Format.decodeArchiveBody(release.optString("body"))
            ?: throw IllegalStateException("This release is not a GitHub Drive archive.")
        val summary = ArchiveSummary.from(release, meta)

        val assets = client.listReleaseAssets(releaseId)
        val assetsByName = assets.associateBy { it.optString("name") }

        val manifest = assetsByName[Format.MANIFEST_ASSET_NAME]?.let { asset ->
            runCatching {
                JSONObject(String(client.downloadAssetBytes(asset.optLong("id")), Charsets.UTF_8))
            }.getOrNull()
        }

        // The manifest wins over the release body wherever they disagree.
        val storageMode = manifest?.optString("storage_mode")?.takeIf { it.isNotEmpty() }
            ?: summary.storageMode
        val encrypted = manifest?.optBoolean("encrypted", summary.encrypted) ?: summary.encrypted

        val entries = when {
            manifest != null && storageMode == Format.STORAGE_MODE_BUNDLE_ASSETS ->
                bundleEntries(manifest, assetsByName, encrypted)
            manifest != null -> fileEntries(manifest, assetsByName)
            else -> fallbackEntries(assets, encrypted)
        }

        val virtualFolders = ArrayList<String>()
        meta.optJSONArray("virtual_folders")?.let { array ->
            for (i in 0 until array.length()) virtualFolders.add(array.optString(i))
        }

        return ArchiveDetail(
            summary = summary.copy(storageMode = storageMode, encrypted = encrypted),
            entries = entries.sortedBy { it.relativePath },
            virtualFolders = virtualFolders.filter { it.isNotEmpty() },
            storageMode = storageMode,
            encrypted = encrypted
        )
    }

    private fun parseParts(item: JSONObject, assetsByName: Map<String, JSONObject>): List<PartRef> {
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
                        assetId = asset.optLong("id"),
                        size = asset.optLong("size", part.optLong("size", 0L))
                    )
                )
            }
        }
        if (parts.isEmpty()) {
            // Legacy single-asset item: synthesise one part from the top-level asset name.
            val name = item.optString("asset_name")
            assetsByName[name]?.let { asset ->
                parts.add(PartRef(0, name, asset.optLong("id"), asset.optLong("size", 0L)))
            }
        }
        return parts.sortedBy { it.order }
    }

    private fun fileEntries(
        manifest: JSONObject,
        assetsByName: Map<String, JSONObject>
    ): List<ArchiveEntry> {
        val items = manifest.optJSONArray("items") ?: return emptyList()
        val entries = ArrayList<ArchiveEntry>()
        for (i in 0 until items.length()) {
            val item = items.getJSONObject(i)
            val parts = parseParts(item, assetsByName)
            if (parts.isEmpty()) continue
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
        return entries
    }

    private fun bundleEntries(
        manifest: JSONObject,
        assetsByName: Map<String, JSONObject>,
        archiveEncrypted: Boolean
    ): List<ArchiveEntry> {
        val items = manifest.optJSONArray("items") ?: return emptyList()
        val entries = ArrayList<ArchiveEntry>()
        for (i in 0 until items.length()) {
            val item = items.getJSONObject(i)
            val parts = parseParts(item, assetsByName)
            if (parts.isEmpty()) continue
            val bundle = BundleRef(
                relativePath = item.optString("relative_path"),
                parts = parts,
                encrypted = item.optBoolean("encrypted", archiveEncrypted)
            )
            val members = item.optJSONArray("members") ?: continue
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
        return entries
    }

    /** No manifest: decode names heuristically. Chunked files are not recoverable this way. */
    private fun fallbackEntries(
        assets: List<JSONObject>,
        archiveEncrypted: Boolean
    ): List<ArchiveEntry> {
        val entries = ArrayList<ArchiveEntry>()
        for (asset in assets) {
            var name = asset.optString("name")
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
            val size = asset.optLong("size", 0L)
            entries.add(
                ArchiveEntry(
                    order = order,
                    relativePath = name.replace("__", "/"),
                    originalSize = size,
                    encrypted = encrypted,
                    contentType = asset.optString("content_type", "application/octet-stream"),
                    parts = listOf(PartRef(0, asset.optString("name"), asset.optLong("id"), size))
                )
            )
        }
        return entries
    }

    // ------------------------------------------------------------------ cover

    suspend fun coverBytes(releaseId: Long): ByteArray? {
        return try {
            val assets = client.listReleaseAssets(releaseId)
            val cover = assets.firstOrNull { it.optString("name") == Format.COVER_ASSET_NAME }
                ?: return null
            client.downloadAssetBytes(cover.optLong("id"))
        } catch (e: Exception) {
            null
        }
    }

    suspend fun coverBytesFromAssets(assets: List<JSONObject>): ByteArray? {
        val cover = assets.firstOrNull { it.optString("name") == Format.COVER_ASSET_NAME } ?: return null
        return runCatching { client.downloadAssetBytes(cover.optLong("id")) }.getOrNull()
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
}
