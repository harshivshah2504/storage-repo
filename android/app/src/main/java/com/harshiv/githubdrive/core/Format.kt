package com.harshiv.githubdrive.core

import org.json.JSONObject
import java.util.Locale
import java.util.UUID

/**
 * The github-drive on-the-wire format, ported from `github_drive/storage.py` and `api.py`.
 *
 * Every constant and string layout here is load-bearing: the Flask web app parses the same
 * releases, so anything the phone writes has to come out byte-identical to what Python writes.
 */
object Format {

    const val STORAGE_FORMAT = "github-drive-archive"
    const val METADATA_VERSION = 1
    const val ARCHIVE_MARKER = "GITHUB_DRIVE_ARCHIVE="
    const val ARCHIVE_TAG_PREFIX = "github-drive-"

    const val MANIFEST_ASSET_NAME = "_manifest.json"
    const val COVER_ASSET_NAME = "_cover.jpg"

    const val STORAGE_MODE_FILE_ASSETS = "file-assets"
    const val STORAGE_MODE_BUNDLE_ASSETS = "bundle-assets"

    const val ENCRYPTED_SUFFIX = ".enc"

    /** `DEFAULT_CHUNK_BYTES` - decimal 1.9 GB, deliberately under GitHub's 2 GB asset cap. */
    const val DEFAULT_CHUNK_BYTES = 1_900_000_000L

    private val SAFE_NAME_RE = Regex("[^A-Za-z0-9._-]+")

    // ---------------------------------------------------------------- identity

    /** 12 uppercase hex characters, matching `uuid.uuid4().hex[:12].upper()`. */
    fun newArchiveId(): String =
        UUID.randomUUID().toString().replace("-", "").take(12).uppercase(Locale.ROOT)

    fun tagFor(archiveId: String): String = ARCHIVE_TAG_PREFIX + archiveId.lowercase(Locale.ROOT)

    fun titleFor(sourceName: String, totalItems: Int): String {
        val safe = sourceName.replace(Regex("\\s+"), " ").trim().ifEmpty { "archive" }
        return "GitHub Drive | " + safe + " | " + totalItems + " items"
    }

    /**
     * Python's `datetime.now(timezone.utc).replace(microsecond=0).isoformat()`.
     * Note the explicit `+00:00` offset - Java's default `Z` suffix would not round-trip.
     */
    fun nowUtcIso(): String {
        val fmt = java.text.SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.ROOT)
        fmt.timeZone = java.util.TimeZone.getTimeZone("UTC")
        return fmt.format(java.util.Date()) + "+00:00"
    }

    // ---------------------------------------------------------------- release body

    fun encodeArchiveBody(metadata: Map<String, Any?>): String {
        val payload = PyJson.compact(metadata)
        return "GitHub Drive archive. Do not edit the marker line below; it is parsed by the tool.\n\n" +
            "```\n" + ARCHIVE_MARKER + payload + "\n```\n"
    }

    /** Returns the decoded metadata, or null when the release is not a github-drive archive. */
    fun decodeArchiveBody(body: String?): JSONObject? {
        if (body.isNullOrEmpty()) return null
        for (rawLine in body.split("\n")) {
            val line = rawLine.trim()
            if (!line.startsWith(ARCHIVE_MARKER)) continue
            val payload = line.substring(ARCHIVE_MARKER.length)
            return try {
                JSONObject(payload)
            } catch (e: Exception) {
                null
            }
        }
        return null
    }

    // ---------------------------------------------------------------- asset naming

    fun sanitizeForAssetName(value: String): String {
        val cleaned = SAFE_NAME_RE.replace(value, "-").trim('-', '.')
        return cleaned.ifEmpty { "file" }
    }

    private fun flatten(relativePath: String): String =
        relativePath.replace("/", "__").replace("\\", "__")

    /** `_asset_name_for` - `NNNN-<safe>[.enc]`, safe part truncated to 180 chars. */
    fun assetNameFor(order: Int, relativePath: String, encrypted: Boolean): String {
        val safe = sanitizeForAssetName(flatten(relativePath)).take(180)
        val suffix = if (encrypted) ENCRYPTED_SUFFIX else ""
        return String.format(Locale.ROOT, "%04d-%s%s", order, safe, suffix)
    }

    /** `_part_asset_name_for` - `NNNN-<safe>.partKKKK[.enc]`, safe part truncated to 160 chars. */
    fun partAssetNameFor(order: Int, relativePath: String, chunkIndex: Int, encrypted: Boolean): String {
        val safe = sanitizeForAssetName(flatten(relativePath)).take(160)
        val suffix = if (encrypted) ENCRYPTED_SUFFIX else ""
        return String.format(Locale.ROOT, "%04d-%s.part%04d%s", order, safe, chunkIndex, suffix)
    }

    // ---------------------------------------------------------------- paths

    /**
     * `_safe_upload_relative_path` - backslashes to `/`, no NUL, no leading `/`, `.` segments
     * dropped, `..` and control characters rejected.
     */
    fun safeRelativePath(raw: String): String {
        val normalized = raw.replace("\\", "/").filter { it.code != 0 }
        val segments = ArrayList<String>()
        for (segment in normalized.split("/")) {
            if (segment.isEmpty() || segment == ".") continue
            require(segment != "..") { "Unsafe path segment in " + raw }
            require(segment.none { it.code < 0x20 || it.code == 0x7F }) { "Control character in " + raw }
            segments.add(segment)
        }
        require(segments.isNotEmpty()) { "Empty path: " + raw }
        return segments.joinToString("/")
    }

    /**
     * `collect_file_entries` sorts `Path` objects, which on POSIX compares the whole `/`-separated
     * string. Sorting segment-by-segment would give a different order, so compare the full string.
     */
    fun <T> sortedByPath(items: List<T>, path: (T) -> String): List<T> =
        items.sortedWith(compareBy { path(it) })

    // ---------------------------------------------------------------- classification

    private val IMAGE_EXT = setOf(".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff")
    private val VIDEO_EXT = setOf(".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".m2ts", ".wmv", ".flv")
    private val AUDIO_EXT = setOf(".mp3", ".flac", ".wav", ".aac", ".ogg", ".opus", ".m4a")
    private val DOC_EXT = setOf(".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".txt", ".md", ".csv", ".rtf", ".odt")
    private val ARCHIVE_EXT = setOf(".zip", ".tar", ".gz", ".tgz", ".bz2", ".7z", ".rar")
    private val CODE_EXT = setOf(
        ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
        ".go", ".rs", ".rb", ".php", ".sh", ".html", ".css", ".json", ".yaml", ".yml", ".toml"
    )

    val KIND_KEYS = listOf("image", "video", "audio", "document", "archive", "code", "other")

    fun extensionOf(path: String): String {
        val name = path.substringAfterLast('/')
        val dot = name.lastIndexOf('.')
        if (dot <= 0) return ""
        return name.substring(dot).lowercase(Locale.ROOT)
    }

    fun classifyExtension(ext: String): String = when (ext) {
        in IMAGE_EXT -> "image"
        in VIDEO_EXT -> "video"
        in AUDIO_EXT -> "audio"
        in DOC_EXT -> "document"
        in ARCHIVE_EXT -> "archive"
        in CODE_EXT -> "code"
        else -> "other"
    }

    fun classifyPath(path: String): String = classifyExtension(extensionOf(path))

    fun isVisual(path: String): Boolean {
        val kind = classifyPath(path)
        return kind == "image" || kind == "video"
    }

    /** Always all seven keys, in the order Python emits them. */
    fun classifyCounts(paths: List<String>): LinkedHashMap<String, Any?> {
        val counts = LinkedHashMap<String, Any?>()
        for (key in KIND_KEYS) counts[key] = 0
        for (path in paths) {
            val kind = classifyPath(path)
            counts[kind] = (counts[kind] as Int) + 1
        }
        return counts
    }

    fun guessContentType(path: String): String {
        val ext = extensionOf(path).removePrefix(".")
        if (ext.isEmpty()) return "application/octet-stream"
        val mime = android.webkit.MimeTypeMap.getSingleton()
            .getMimeTypeFromExtension(ext.lowercase(Locale.ROOT))
        return mime ?: "application/octet-stream"
    }

    // ---------------------------------------------------------------- folders

    fun normalizeFolderPath(raw: String): String {
        val normalized = raw.replace("\\", "/").trim().trim('/')
        val segments = ArrayList<String>()
        for (segment in normalized.split("/")) {
            if (segment.isEmpty() || segment == ".") continue
            require(segment != "..") { "Unsafe folder path: " + raw }
            segments.add(segment)
        }
        return segments.joinToString("/")
    }

    fun folderAncestors(path: String): List<String> {
        val segments = path.split("/").filter { it.isNotEmpty() }
        val out = ArrayList<String>()
        val builder = StringBuilder()
        for (segment in segments) {
            if (builder.isNotEmpty()) builder.append('/')
            builder.append(segment)
            out.add(builder.toString())
        }
        return out
    }
}
