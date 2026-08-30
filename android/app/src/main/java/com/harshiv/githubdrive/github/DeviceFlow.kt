package com.harshiv.githubdrive.github

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext
import okhttp3.FormBody
import okhttp3.OkHttpClient
import okhttp3.Request
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * GitHub OAuth Device Flow.
 *
 * Chosen over the redirect flow because it needs no client secret in the APK and no redirect URI:
 * the app shows a short code, the person approves it on github.com, and the app polls for the token.
 * Requires "Enable Device Flow" to be ticked on the OAuth App.
 */
object DeviceFlow {

    private const val DEVICE_CODE_URL = "https://github.com/login/device/code"
    private const val TOKEN_URL = "https://github.com/login/oauth/access_token"
    const val VERIFICATION_URL = "https://github.com/login/device"

    /** `repo` covers creating the private storage repo and managing its releases and assets. */
    const val SCOPE = "repo"

    private val http = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    data class Codes(
        val deviceCode: String,
        val userCode: String,
        val verificationUri: String,
        val expiresInSeconds: Int,
        val intervalSeconds: Int
    )

    sealed interface PollResult {
        data class Success(val accessToken: String) : PollResult
        data object Pending : PollResult
        data class SlowDown(val newIntervalSeconds: Int) : PollResult
        data class Failed(val error: String, val description: String) : PollResult
    }

    suspend fun requestCodes(clientId: String): Codes = withContext(Dispatchers.IO) {
        val form = FormBody.Builder()
            .add("client_id", clientId)
            .add("scope", SCOPE)
            .build()
        val request = Request.Builder()
            .url(DEVICE_CODE_URL)
            .header("Accept", "application/json")
            .header("User-Agent", "memvault")
            .post(form)
            .build()
        http.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) {
                throw GitHubException(response.code, response.message, text)
            }
            val json = JSONObject(text)
            if (json.has("error")) {
                throw GitHubException(400, json.optString("error"), json.optString("error_description"))
            }
            Codes(
                deviceCode = json.getString("device_code"),
                userCode = json.getString("user_code"),
                verificationUri = json.optString("verification_uri", VERIFICATION_URL),
                expiresInSeconds = json.optInt("expires_in", 900),
                intervalSeconds = json.optInt("interval", 5)
            )
        }
    }

    suspend fun poll(clientId: String, deviceCode: String): PollResult = withContext(Dispatchers.IO) {
        val form = FormBody.Builder()
            .add("client_id", clientId)
            .add("device_code", deviceCode)
            .add("grant_type", "urn:ietf:params:oauth:grant-type:device_code")
            .build()
        val request = Request.Builder()
            .url(TOKEN_URL)
            .header("Accept", "application/json")
            .header("User-Agent", "memvault")
            .post(form)
            .build()
        try {
            http.newCall(request).execute().use { response ->
                val json = runCatching { JSONObject(response.body?.string().orEmpty()) }
                    .getOrElse { return@use PollResult.Pending }

                json.optString("access_token").takeIf { it.isNotEmpty() }?.let {
                    return@use PollResult.Success(it)
                }

                when (val error = json.optString("error")) {
                    "authorization_pending" -> PollResult.Pending
                    "slow_down" -> PollResult.SlowDown(json.optInt("interval", 10))
                    "" -> PollResult.Pending
                    else -> PollResult.Failed(error, json.optString("error_description", error))
                }
            }
        } catch (e: java.io.IOException) {
            // Samsung/Xiaomi freeze a backgrounded app and cut its network (netd: isBlocked=true).
            // Sign-in inherently backgrounds us while the user authorizes, so a dead poll means
            // "not right now", never "sign-in failed". We keep trying until the code expires.
            PollResult.Pending
        }
    }

    /**
     * Polls until GitHub hands over a token, the codes expire, or the person denies the request.
     * [onTick] reports the seconds left so the UI can show a countdown.
     */
    suspend fun awaitToken(
        clientId: String,
        codes: Codes,
        onTick: (secondsLeft: Int) -> Unit = {}
    ): String {
        var interval = codes.intervalSeconds.coerceAtLeast(5)
        val deadline = System.currentTimeMillis() + codes.expiresInSeconds * 1000L

        while (System.currentTimeMillis() < deadline) {
            delay(interval * 1000L)
            onTick(((deadline - System.currentTimeMillis()) / 1000L).toInt().coerceAtLeast(0))

            when (val result = poll(clientId, codes.deviceCode)) {
                is PollResult.Success -> return result.accessToken
                is PollResult.Pending -> Unit
                is PollResult.SlowDown -> interval = result.newIntervalSeconds.coerceAtLeast(interval + 5)
                is PollResult.Failed -> throw DeviceFlowException(result.error, friendly(result))
            }
        }
        throw DeviceFlowException("expired_token", "The sign-in code expired. Tap sign in to get a new one.")
    }

    private fun friendly(result: PollResult.Failed): String = when (result.error) {
        "access_denied" -> "Sign-in was cancelled on GitHub."
        "expired_token" -> "The sign-in code expired. Tap sign in to get a new one."
        "incorrect_client_credentials" ->
            "This app's GitHub client ID is not set up. Rebuild it with a valid OAuth App client ID."
        "device_flow_disabled" ->
            "Device Flow is turned off for this OAuth App. Enable it in the app's GitHub settings."
        else -> result.description.ifBlank { "GitHub refused the sign-in (${result.error})." }
    }
}

class DeviceFlowException(val error: String, message: String) : RuntimeException(message)
