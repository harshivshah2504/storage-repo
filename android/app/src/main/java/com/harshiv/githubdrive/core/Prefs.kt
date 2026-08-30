package com.harshiv.githubdrive.core

import android.content.Context
import android.content.SharedPreferences
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Small settings store. The GitHub token is the only secret here, and it is wrapped with an
 * AES/GCM key held in the Android Keystore so it never sits in SharedPreferences as plaintext.
 */
class Prefs(context: Context) {

    private val prefs: SharedPreferences =
        context.applicationContext.getSharedPreferences("gd_prefs", Context.MODE_PRIVATE)

    var token: String?
        get() = prefs.getString(KEY_TOKEN, null)?.let { decryptOrNull(it) }
        set(value) {
            val editor = prefs.edit()
            if (value == null) editor.remove(KEY_TOKEN) else editor.putString(KEY_TOKEN, encrypt(value))
            editor.apply()
        }

    var login: String?
        get() = prefs.getString(KEY_LOGIN, null)
        set(value) = prefs.edit().putString(KEY_LOGIN, value).apply()

    var repoOwner: String?
        get() = prefs.getString(KEY_REPO_OWNER, null)
        set(value) = prefs.edit().putString(KEY_REPO_OWNER, value).apply()

    /**
     * Which storage this install uses.
     *
     * Falls back to the name derived from the signed-in account, so an install from before the
     * name was recorded still resolves to the same place a fresh sign-in would.
     */
    var repoName: String
        get() = prefs.getString(KEY_REPO_NAME, null)?.takeIf { it.isNotEmpty() }
            ?: login?.takeIf { it.isNotEmpty() }?.let { defaultRepoFor(it) }
            ?: UNSET_REPO
        set(value) = prefs.edit().putString(KEY_REPO_NAME, value).apply()

    /** False until a sign-in has settled which storage this install uses. */
    val hasRepoName: Boolean get() = !prefs.getString(KEY_REPO_NAME, null).isNullOrEmpty()

    val isSignedIn: Boolean get() = !token.isNullOrEmpty() && !repoOwner.isNullOrEmpty()

    /**
     * A running total of what has been stored, kept here so Settings can answer instantly.
     *
     * Counting it properly means walking every release, because a repository's reported size does
     * not include release assets. Adding each upload to a number as it finishes costs nothing and
     * is right in the ordinary case; the real walk still runs quietly to correct any drift - an
     * upload from the web app or another phone is invisible to this counter.
     */
    var storedBytes: Long
        get() = prefs.getLong(KEY_STORED_BYTES, 0L).coerceAtLeast(0L)
        set(value) = prefs.edit().putLong(KEY_STORED_BYTES, value.coerceAtLeast(0L)).apply()

    fun addStoredBytes(delta: Long) {
        storedBytes = storedBytes + delta
    }

    // ------------------------------------------------------------------ gallery backup

    var autoUpload: Boolean
        get() = prefs.getBoolean(KEY_AUTO_UPLOAD, false)
        set(value) = prefs.edit().putBoolean(KEY_AUTO_UPLOAD, value).apply()

    var autoUploadWifiOnly: Boolean
        get() = prefs.getBoolean(KEY_AUTO_UPLOAD_WIFI, true)
        set(value) = prefs.edit().putBoolean(KEY_AUTO_UPLOAD_WIFI, value).apply()

    /**
     * How far the camera roll has been backed up, as `(date_added, _id)`.
     *
     * The pair matters: a burst of photos shares one `date_added` second, and a watermark of only
     * the timestamp would skip every shot after the first in that second.
     */
    var autoUploadSince: Long
        get() = prefs.getLong(KEY_AUTO_UPLOAD_SINCE, 0L)
        set(value) = prefs.edit().putLong(KEY_AUTO_UPLOAD_SINCE, value).apply()

    var autoUploadLastId: Long
        get() = prefs.getLong(KEY_AUTO_UPLOAD_LAST_ID, 0L)
        set(value) = prefs.edit().putLong(KEY_AUTO_UPLOAD_LAST_ID, value).apply()

    fun clear() {
        prefs.edit().clear().apply()
        runCatching {
            val store = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
            if (store.containsAlias(KEY_ALIAS)) store.deleteEntry(KEY_ALIAS)
        }
    }

    // ------------------------------------------------------------------ crypto

    private fun secretKey(): SecretKey {
        val store = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
        (store.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build()
        )
        return generator.generateKey()
    }

    private fun encrypt(plain: String): String {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val iv = cipher.iv
        val body = cipher.doFinal(plain.toByteArray(Charsets.UTF_8))
        val out = ByteArray(1 + iv.size + body.size)
        out[0] = iv.size.toByte()
        System.arraycopy(iv, 0, out, 1, iv.size)
        System.arraycopy(body, 0, out, 1 + iv.size, body.size)
        return Base64.encodeToString(out, Base64.NO_WRAP)
    }

    private fun decryptOrNull(encoded: String): String? = runCatching {
        val raw = Base64.decode(encoded, Base64.NO_WRAP)
        val ivLen = raw[0].toInt()
        val iv = raw.copyOfRange(1, 1 + ivLen)
        val body = raw.copyOfRange(1 + ivLen, raw.size)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, iv))
        String(cipher.doFinal(body), Charsets.UTF_8)
    }.getOrNull()

    companion object {
        /** Storage is named after the account that owns it. */
        fun defaultRepoFor(login: String): String = "$login-storage"

        /** Stands in until an account is known; nothing is ever read or written under it. */
        const val UNSET_REPO = "storage"

        private const val ANDROID_KEYSTORE = "AndroidKeyStore"
        private const val KEY_ALIAS = "gd_token_key"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"

        private const val KEY_TOKEN = "token"
        private const val KEY_LOGIN = "login"
        private const val KEY_REPO_OWNER = "repo_owner"
        private const val KEY_REPO_NAME = "repo_name"
        private const val KEY_STORED_BYTES = "stored_bytes"
        private const val KEY_AUTO_UPLOAD = "auto_upload"
        private const val KEY_AUTO_UPLOAD_WIFI = "auto_upload_wifi_only"
        private const val KEY_AUTO_UPLOAD_SINCE = "auto_upload_since"
        private const val KEY_AUTO_UPLOAD_LAST_ID = "auto_upload_last_id"
    }
}
