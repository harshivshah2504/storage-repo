package com.harshiv.githubdrive.ui

import android.app.Application
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.harshiv.githubdrive.BuildConfig
import com.harshiv.githubdrive.GdApp
import com.harshiv.githubdrive.core.Prefs
import com.harshiv.githubdrive.drive.ArchiveDetail
import com.harshiv.githubdrive.drive.ArchiveEntry
import com.harshiv.githubdrive.drive.ArchiveSummary
import com.harshiv.githubdrive.drive.DriveRepo
import com.harshiv.githubdrive.drive.Picking
import com.harshiv.githubdrive.drive.UploadItem
import com.harshiv.githubdrive.drive.Uploader
import com.harshiv.githubdrive.github.DeviceFlow
import com.harshiv.githubdrive.github.GitHubClient
import com.harshiv.githubdrive.transfer.TransferManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

sealed interface SignInPhase {
    data object Idle : SignInPhase
    data class Preparing(val message: String) : SignInPhase
    data class AwaitingApproval(val codes: DeviceFlow.Codes) : SignInPhase
    data class Finishing(val message: String) : SignInPhase
    data class Failed(val message: String) : SignInPhase
}

class AppViewModel(app: Application) : AndroidViewModel(app) {

    private val prefs: Prefs get() = (getApplication<Application>() as GdApp).prefs

    var signedIn by mutableStateOf(prefs.isSignedIn)
        private set
    var login by mutableStateOf(prefs.login)
        private set
    var repoName by mutableStateOf(prefs.repoName)
        private set

    var signInPhase by mutableStateOf<SignInPhase>(SignInPhase.Idle)
        private set

    var archives by mutableStateOf<List<ArchiveSummary>>(emptyList())
        private set
    var archivesLoading by mutableStateOf(false)
        private set
    var archivesError by mutableStateOf<String?>(null)
        private set
    var hasMore by mutableStateOf(false)
        private set
    private var page = 1

    var detail by mutableStateOf<ArchiveDetail?>(null)
        private set
    var detailLoading by mutableStateOf(false)
        private set
    var detailError by mutableStateOf<String?>(null)
        private set
    var currentPath by mutableStateOf("")
        private set

    var banner by mutableStateOf<String?>(null)

    /** Cover bytes keyed by release id; null means "looked and there wasn't one". */
    val covers = mutableStateMapOf<Long, ByteArray?>()

    private var signInJob: Job? = null

    private var cachedClient: GitHubClient? = null
    private var cachedToken: String? = null

    private fun client(): GitHubClient? {
        val token = prefs.token ?: return null
        val owner = prefs.repoOwner ?: return null
        val existing = cachedClient
        if (existing != null && cachedToken == token) {
            existing.owner = owner
            existing.repo = prefs.repoName
            return existing
        }
        val fresh = GitHubClient(token, owner, prefs.repoName)
        cachedClient = fresh
        cachedToken = token
        return fresh
    }

    fun repo(): DriveRepo? = client()?.let { DriveRepo(it, getApplication<Application>().cacheDir) }

    fun uploader(): Uploader? = client()?.let { Uploader(getApplication(), it) }

    // ------------------------------------------------------------------ sign in

    fun startSignIn() {
        if (signInJob?.isActive == true) return
        val clientId = BuildConfig.GITHUB_OAUTH_CLIENT_ID
        if (clientId.isBlank() || clientId == "REPLACE_WITH_YOUR_CLIENT_ID") {
            signInPhase = SignInPhase.Failed(
                "This build has no GitHub client ID. Rebuild it with your OAuth App's client ID."
            )
            return
        }

        signInJob = viewModelScope.launch {
            try {
                signInPhase = SignInPhase.Preparing("Asking GitHub for a sign-in code...")
                val codes = DeviceFlow.requestCodes(clientId)
                signInPhase = SignInPhase.AwaitingApproval(codes)

                val token = DeviceFlow.awaitToken(clientId, codes)

                signInPhase = SignInPhase.Finishing("Setting up your storage...")
                val probe = GitHubClient(token)
                val user = probe.viewerLogin()
                probe.owner = user
                probe.repo = prefs.repoName
                probe.ensureRepo(private = true)

                // Generating the Keystore key can take a few hundred milliseconds.
                withContext(Dispatchers.IO) {
                    prefs.token = token
                    prefs.login = user
                    prefs.repoOwner = user
                }
                cachedClient = null
                cachedToken = null

                login = user
                signedIn = true
                signInPhase = SignInPhase.Idle
                refreshArchives()
            } catch (e: Exception) {
                signInPhase = SignInPhase.Failed(e.message ?: "Sign-in failed.")
            }
        }
    }

    fun cancelSignIn() {
        signInJob?.cancel()
        signInJob = null
        signInPhase = SignInPhase.Idle
    }

    fun signOut() {
        signInJob?.cancel()
        prefs.clear()
        cachedClient = null
        cachedToken = null
        covers.clear()
        archives = emptyList()
        detail = null
        signedIn = false
        login = null
        repoName = Prefs.DEFAULT_REPO
        signInPhase = SignInPhase.Idle
    }

    // ------------------------------------------------------------------ archives

    fun refreshArchives() {
        val repo = repo() ?: return
        page = 1
        archivesLoading = true
        archivesError = null
        viewModelScope.launch {
            try {
                val (list, more) = repo.listArchives(page)
                archives = list
                hasMore = more
            } catch (e: Exception) {
                archivesError = friendly(e)
            } finally {
                archivesLoading = false
            }
        }
    }

    fun loadMoreArchives() {
        val repo = repo() ?: return
        if (archivesLoading || !hasMore) return
        archivesLoading = true
        viewModelScope.launch {
            try {
                val (list, more) = repo.listArchives(page + 1)
                page += 1
                // Offset paging can repeat a release if one is published mid-scroll, and a
                // duplicate key crashes the lazy grid.
                archives = (archives + list).distinctBy { it.releaseId }
                hasMore = more
            } catch (e: Exception) {
                archivesError = friendly(e)
            } finally {
                archivesLoading = false
            }
        }
    }

    fun loadCover(summary: ArchiveSummary) {
        if (covers.containsKey(summary.releaseId)) return
        if (summary.coverAssetName == null) {
            covers[summary.releaseId] = null
            return
        }
        val repo = repo() ?: return
        covers[summary.releaseId] = null
        viewModelScope.launch {
            val bytes = withContext(Dispatchers.IO) { repo.coverBytes(summary.releaseId) }
            if (bytes != null) covers[summary.releaseId] = bytes
        }
    }

    fun openArchive(summary: ArchiveSummary) {
        val repo = repo() ?: return
        detail = null
        currentPath = ""
        detailLoading = true
        detailError = null
        viewModelScope.launch {
            try {
                detail = repo.loadDetail(summary.releaseId)
            } catch (e: Exception) {
                detailError = friendly(e)
            } finally {
                detailLoading = false
            }
        }
    }

    fun enterFolder(path: String) {
        currentPath = path
    }

    /** Returns false when already at the archive root, so the caller can pop the back stack. */
    fun goUp(): Boolean {
        if (currentPath.isEmpty()) return false
        currentPath = currentPath.substringBeforeLast('/', "")
        return true
    }

    fun deleteArchive(summary: ArchiveSummary) {
        val repo = repo() ?: return
        viewModelScope.launch {
            try {
                repo.deleteArchive(summary)
                archives = archives.filterNot { it.releaseId == summary.releaseId }
                covers.remove(summary.releaseId)
                banner = "Deleted ${summary.sourceName}"
            } catch (e: Exception) {
                banner = friendly(e)
            }
        }
    }

    // ------------------------------------------------------------------ transfers

    fun uploadFiles(uris: List<Uri>) {
        val uploader = uploader() ?: return
        if (uris.isEmpty()) return
        viewModelScope.launch {
            val items = withContext(Dispatchers.IO) { Picking.fromFiles(getApplication(), uris) }
            if (items.isEmpty()) {
                banner = "Nothing to upload."
                return@launch
            }
            val name = if (items.size == 1) items[0].relativePath else "${items.size} files"
            startUpload(uploader, name, items, emptyList())
        }
    }

    fun uploadFolder(treeUri: Uri) {
        val uploader = uploader() ?: return
        viewModelScope.launch {
            val context = getApplication<Application>()
            val (name, items) = withContext(Dispatchers.IO) { Picking.fromTree(context, treeUri) }
            if (items.isEmpty()) {
                banner = "That folder has no files in it."
                return@launch
            }
            val folders = withContext(Dispatchers.IO) { Picking.emptyFolders(context, treeUri) }
            startUpload(uploader, name, items, folders)
        }
    }

    private fun startUpload(
        uploader: Uploader,
        name: String,
        items: List<UploadItem>,
        folders: List<String>
    ) {
        TransferManager.startUpload(getApplication(), uploader, name, items, folders) {
            viewModelScope.launch { refreshArchives() }
        }
        banner = "Uploading $name"
    }

    fun download(entry: ArchiveEntry, target: Uri) {
        val repo = repo() ?: return
        TransferManager.startDownload(getApplication(), repo, entry, target)
        banner = "Saving ${entry.name}"
    }

    private fun friendly(e: Exception): String {
        val message = e.message ?: "Something went wrong."
        return when {
            message.contains("401") -> "GitHub rejected the sign-in. Sign out and sign in again."
            message.contains("404") -> "That storage repository is gone. Check Settings."
            message.contains("rate limit", ignoreCase = true) ->
                "GitHub is rate limiting this account. Try again in a few minutes."
            else -> message
        }
    }
}
