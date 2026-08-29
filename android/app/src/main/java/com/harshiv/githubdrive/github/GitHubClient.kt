package com.harshiv.githubdrive.github

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.Headers
import okhttp3.MediaType
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okio.BufferedSink
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.random.Random

class GitHubException(
    val status: Int,
    val reason: String,
    val body: String = ""
) : RuntimeException("GitHub $status $reason: ${body.take(400)}")

/**
 * Thin GitHub REST client covering exactly the calls github-drive uses.
 *
 * Mirrors `github_drive/api.py`: the same default headers, the same 2 GB-aware asset endpoints,
 * and the same retry policy for primary and secondary rate limits.
 */
class GitHubClient(
    private val token: String,
    var owner: String = "",
    var repo: String = ""
) {

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(600, TimeUnit.SECONDS)
        .writeTimeout(600, TimeUnit.SECONDS)
        .callTimeout(0, TimeUnit.MILLISECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private fun baseHeaders(): Headers = Headers.Builder()
        .add("Authorization", "Bearer $token")
        .add("Accept", "application/vnd.github+json")
        .add("X-GitHub-Api-Version", API_VERSION)
        .add("User-Agent", "github-drive")
        .build()

    private val repoPath: String get() = "$API_BASE/repos/$owner/$repo"

    // ------------------------------------------------------------------ plumbing

    private fun execute(request: Request, allowedStatuses: Set<Int> = emptySet()): Response {
        var attempt = 0
        var lastError: Exception? = null
        while (attempt < MAX_ATTEMPTS) {
            attempt++
            try {
                val response = http.newCall(request).execute()
                if (response.isSuccessful || response.code in allowedStatuses) return response
                if (attempt < MAX_ATTEMPTS && isRetryable(response)) {
                    val delay = retryDelayMillis(response, attempt)
                    response.close()
                    Thread.sleep(delay)
                    continue
                }
                val body = response.body?.string().orEmpty()
                val code = response.code
                val message = response.message
                response.close()
                throw GitHubException(code, message, body)
            } catch (e: GitHubException) {
                throw e
            } catch (e: InterruptedException) {
                throw e
            } catch (e: Exception) {
                lastError = e
                if (attempt >= MAX_ATTEMPTS) break
                Thread.sleep(min(1000L * (1L shl (attempt - 1)), 8000L))
            }
        }
        throw GitHubException(0, "request failed after $MAX_ATTEMPTS attempts", lastError?.message.orEmpty())
    }

    private fun isRetryable(response: Response): Boolean {
        if (response.code in setOf(429, 500, 502, 503, 504)) return true
        if (response.code == 403) {
            if (response.header("Retry-After") != null) return true
            if (response.header("X-RateLimit-Remaining") == "0") return true
            val peek = runCatching { response.peekBody(4096).string().lowercase() }.getOrDefault("")
            if (peek.contains("rate limit")) return true
        }
        return false
    }

    private fun retryDelayMillis(response: Response, attempt: Int): Long {
        response.header("Retry-After")?.toLongOrNull()?.let {
            return it.coerceIn(1L, 30L) * 1000L
        }
        if (response.header("X-RateLimit-Remaining") == "0") {
            val reset = response.header("X-RateLimit-Reset")?.toLongOrNull()
            if (reset != null) {
                val wait = reset - System.currentTimeMillis() / 1000L
                return wait.coerceIn(1L, 30L) * 1000L
            }
        }
        val backoff = min(1L shl (attempt - 1), 30L) * 1000L
        return backoff + Random.nextLong(0, 1000)
    }

    private fun getJson(url: String, allow404: Boolean = false): JSONObject? {
        val request = Request.Builder().url(url).headers(baseHeaders()).get().build()
        val response = execute(request, if (allow404) setOf(404) else emptySet())
        response.use {
            if (allow404 && it.code == 404) return null
            return JSONObject(it.body?.string().orEmpty())
        }
    }

    private fun sendJson(method: String, url: String, payload: JSONObject?): JSONObject {
        val body = (payload?.toString() ?: "{}").toRequestBodyJson()
        val request = Request.Builder().url(url).headers(baseHeaders()).method(method, body).build()
        execute(request).use { response ->
            val text = response.body?.string().orEmpty()
            return if (text.isBlank()) JSONObject() else JSONObject(text)
        }
    }

    private fun String.toRequestBodyJson(): RequestBody =
        this.toRequestBody("application/json; charset=utf-8".toMediaType())

    // ------------------------------------------------------------------ user + repo

    suspend fun viewerLogin(): String = withContext(Dispatchers.IO) {
        getJson("$API_BASE/user")!!.getString("login")
    }

    suspend fun repoExists(): Boolean = withContext(Dispatchers.IO) {
        getJson(repoPath, allow404 = true) != null
    }

    /** `ensure_repo` - GET the repo, and only create it when GitHub answers 404. */
    suspend fun ensureRepo(private: Boolean = true, description: String = "GitHub Drive archives") =
        withContext(Dispatchers.IO) {
            if (getJson(repoPath, allow404 = true) != null) return@withContext
            val payload = JSONObject()
                .put("name", repo)
                .put("private", private)
                .put("description", description)
                .put("auto_init", true)
            sendJson("POST", "$API_BASE/user/repos", payload)
            Unit
        }

    // ------------------------------------------------------------------ releases

    /** One page of releases plus whether a `rel="next"` link was present. */
    suspend fun listReleasesPage(page: Int, perPage: Int = 24): Pair<List<JSONObject>, Boolean> =
        withContext(Dispatchers.IO) {
            val safePage = page.coerceAtLeast(1)
            val safePerPage = perPage.coerceIn(1, 100)
            val url = "$repoPath/releases?per_page=$safePerPage&page=$safePage"
            val request = Request.Builder().url(url).headers(baseHeaders()).get().build()
            execute(request).use { response ->
                val text = response.body?.string().orEmpty()
                val array = JSONArray(text)
                val out = ArrayList<JSONObject>(array.length())
                for (i in 0 until array.length()) out.add(array.getJSONObject(i))
                val hasMore = response.header("Link")?.contains("rel=\"next\"") == true
                Pair(out, hasMore)
            }
        }

    suspend fun getRelease(releaseId: Long): JSONObject = withContext(Dispatchers.IO) {
        getJson("$repoPath/releases/$releaseId")!!
    }

    /** 404 means "no such archive", which is a normal answer rather than an error. */
    suspend fun getReleaseByTag(tag: String): JSONObject? = withContext(Dispatchers.IO) {
        getJson("$repoPath/releases/tags/$tag", allow404 = true)
    }

    suspend fun createRelease(
        tag: String,
        name: String,
        body: String,
        draft: Boolean = false,
        prerelease: Boolean = false
    ): JSONObject = withContext(Dispatchers.IO) {
        val payload = JSONObject()
            .put("tag_name", tag)
            .put("name", name)
            .put("body", body)
            .put("draft", draft)
            .put("prerelease", prerelease)
        try {
            sendJson("POST", "$repoPath/releases", payload)
        } catch (e: GitHubException) {
            // A concurrent upload may have created the tag already; adopt it instead of failing.
            if (alreadyExists(e, "tag_name")) {
                getReleaseByTag(tag) ?: throw e
            } else {
                throw e
            }
        }
    }

    suspend fun updateRelease(releaseId: Long, name: String?, body: String?): JSONObject =
        withContext(Dispatchers.IO) {
            val payload = JSONObject()
            if (name != null) payload.put("name", name)
            if (body != null) payload.put("body", body)
            sendJson("PATCH", "$repoPath/releases/$releaseId", payload)
        }

    suspend fun deleteRelease(releaseId: Long) = withContext(Dispatchers.IO) {
        val request = Request.Builder().url("$repoPath/releases/$releaseId")
            .headers(baseHeaders()).delete().build()
        execute(request).close()
    }

    /** `delete_tag` - a missing ref is fine, the release is already gone. */
    suspend fun deleteTag(tag: String) = withContext(Dispatchers.IO) {
        val request = Request.Builder().url("$repoPath/git/refs/tags/$tag")
            .headers(baseHeaders()).delete().build()
        execute(request, allowedStatuses = setOf(404)).close()
    }

    // ------------------------------------------------------------------ assets

    suspend fun listReleaseAssets(releaseId: Long): List<JSONObject> = withContext(Dispatchers.IO) {
        val out = ArrayList<JSONObject>()
        var url: String? = "$repoPath/releases/$releaseId/assets?per_page=100"
        while (url != null) {
            val request = Request.Builder().url(url).headers(baseHeaders()).get().build()
            execute(request).use { response ->
                val array = JSONArray(response.body?.string().orEmpty())
                for (i in 0 until array.length()) out.add(array.getJSONObject(i))
                url = nextLink(response.header("Link"))
            }
        }
        out
    }

    private fun nextLink(link: String?): String? {
        if (link.isNullOrEmpty()) return null
        for (part in link.split(",")) {
            val segments = part.split(";")
            if (segments.size < 2) continue
            if (!segments[1].contains("rel=\"next\"")) continue
            return segments[0].trim().removePrefix("<").removeSuffix(">")
        }
        return null
    }

    suspend fun uploadAssetBytes(
        releaseId: Long,
        assetName: String,
        payload: ByteArray,
        contentType: String
    ): JSONObject = withContext(Dispatchers.IO) {
        val body = payload.toRequestBody(contentType.toMediaType())
        uploadAsset(releaseId, assetName, body, contentType)
    }

    /**
     * Streams one asset from an [InputStream] factory. The factory is re-invoked on retry, so it
     * must hand back a fresh stream positioned at the start of the chunk.
     */
    suspend fun uploadAssetStream(
        releaseId: Long,
        assetName: String,
        contentType: String,
        contentLength: Long,
        onProgress: ((Long) -> Unit)? = null,
        streamFactory: () -> InputStream
    ): JSONObject = withContext(Dispatchers.IO) {
        val body = object : RequestBody() {
            override fun contentType(): MediaType? = contentType.toMediaType()
            override fun contentLength(): Long = contentLength
            override fun writeTo(sink: BufferedSink) {
                streamFactory().use { input ->
                    val buffer = ByteArray(COPY_BUFFER)
                    var written = 0L
                    while (written < contentLength) {
                        val want = min(buffer.size.toLong(), contentLength - written).toInt()
                        val read = input.read(buffer, 0, want)
                        if (read <= 0) break
                        sink.write(buffer, 0, read)
                        written += read
                        onProgress?.invoke(written)
                    }
                }
            }
        }
        uploadAsset(releaseId, assetName, body, contentType)
    }

    private fun uploadAsset(
        releaseId: Long,
        assetName: String,
        body: RequestBody,
        contentType: String
    ): JSONObject {
        val encodedName = java.net.URLEncoder.encode(assetName, "UTF-8").replace("+", "%20")
        val url = "$UPLOADS_BASE/repos/$owner/$repo/releases/$releaseId/assets?name=$encodedName"
        val request = Request.Builder()
            .url(url)
            .headers(baseHeaders())
            .header("Content-Type", contentType)
            .post(body)
            .build()
        return try {
            execute(request).use { response ->
                JSONObject(response.body?.string().orEmpty())
            }
        } catch (e: GitHubException) {
            // Same asset name uploaded concurrently - adopt whatever landed.
            if (alreadyExists(e, "name")) {
                findAssetByName(releaseId, assetName) ?: throw e
            } else {
                throw e
            }
        }
    }

    private fun findAssetByName(releaseId: Long, assetName: String): JSONObject? {
        var url: String? = "$repoPath/releases/$releaseId/assets?per_page=100"
        while (url != null) {
            val request = Request.Builder().url(url).headers(baseHeaders()).get().build()
            execute(request).use { response ->
                val array = JSONArray(response.body?.string().orEmpty())
                for (i in 0 until array.length()) {
                    val asset = array.getJSONObject(i)
                    if (asset.optString("name") == assetName) return asset
                }
                url = nextLink(response.header("Link"))
            }
        }
        return null
    }

    suspend fun deleteAsset(assetId: Long) = withContext(Dispatchers.IO) {
        val request = Request.Builder().url("$repoPath/releases/assets/$assetId")
            .headers(baseHeaders()).delete().build()
        execute(request, allowedStatuses = setOf(404)).close()
    }

    private fun assetRequest(assetId: Long): Request = Request.Builder()
        .url("$repoPath/releases/assets/$assetId")
        .headers(baseHeaders())
        .header("Accept", "application/octet-stream")
        .get()
        .build()

    suspend fun downloadAssetBytes(assetId: Long): ByteArray = withContext(Dispatchers.IO) {
        execute(assetRequest(assetId)).use { it.body!!.bytes() }
    }

    /** Appends the asset to [target]; [onBytes] reports cumulative bytes for this asset. */
    suspend fun downloadAssetTo(
        assetId: Long,
        target: File,
        append: Boolean = false,
        onBytes: ((Long) -> Unit)? = null
    ) = withContext(Dispatchers.IO) {
        execute(assetRequest(assetId)).use { response ->
            val input = response.body!!.byteStream()
            java.io.FileOutputStream(target, append).use { output ->
                val buffer = ByteArray(COPY_BUFFER)
                var total = 0L
                while (true) {
                    ensureActive()
                    val read = input.read(buffer)
                    if (read <= 0) break
                    output.write(buffer, 0, read)
                    total += read
                    onBytes?.invoke(total)
                }
                output.flush()
            }
        }
    }

    /** Streams an asset straight into an arbitrary sink, used for SAF "save to device". */
    suspend fun downloadAssetToStream(
        assetId: Long,
        output: java.io.OutputStream,
        onBytes: ((Long) -> Unit)? = null
    ) = withContext(Dispatchers.IO) {
        execute(assetRequest(assetId)).use { response ->
            val input = response.body!!.byteStream()
            val buffer = ByteArray(COPY_BUFFER)
            var total = 0L
            while (true) {
                ensureActive()
                val read = input.read(buffer)
                if (read <= 0) break
                output.write(buffer, 0, read)
                total += read
                onBytes?.invoke(total)
            }
            output.flush()
        }
    }

    private fun alreadyExists(e: GitHubException, field: String): Boolean {
        if (e.status != 422) return false
        return runCatching {
            val errors = JSONObject(e.body).optJSONArray("errors") ?: return false
            for (i in 0 until errors.length()) {
                val item = errors.getJSONObject(i)
                if (item.optString("code").trim() != "already_exists") continue
                if (item.optString("field").trim() != field) continue
                return true
            }
            false
        }.getOrDefault(false)
    }

    companion object {
        const val API_BASE = "https://api.github.com"
        const val UPLOADS_BASE = "https://uploads.github.com"
        const val API_VERSION = "2022-11-28"
        private const val MAX_ATTEMPTS = 3
        private const val COPY_BUFFER = 512 * 1024
    }
}
