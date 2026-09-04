package com.harshiv.githubdrive.ui

import android.app.Application
import android.net.Uri
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.harshiv.githubdrive.BuildConfig
import com.harshiv.githubdrive.GdApp
import com.harshiv.githubdrive.core.Prefs
import androidx.documentfile.provider.DocumentFile
import com.harshiv.githubdrive.drive.ArchiveDetail
import com.harshiv.githubdrive.drive.ArchiveEntry
import com.harshiv.githubdrive.drive.ArchiveSummary
import com.harshiv.githubdrive.drive.DriveRepo
import com.harshiv.githubdrive.drive.Picking
import com.harshiv.githubdrive.drive.UploadItem
import com.harshiv.githubdrive.drive.Uploader
import com.harshiv.githubdrive.github.DeviceFlow
import com.harshiv.githubdrive.github.GitHubClient
import com.harshiv.githubdrive.transfer.AutoUpload
import com.harshiv.githubdrive.transfer.TransferManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext

/** How the files inside an archive are laid out. */
enum class BrowseView { LIST, TILE }

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

    /** Files ticked inside the open archive, by path. */
    val selected = mutableStateListOf<String>()

    /**
     * Whether the browse screen is picking rather than opening.
     *
     * Kept separately from [selected] being non-empty so that tapping Select puts the screen into
     * picking mode with nothing chosen yet - otherwise the only way in is a long press, which
     * nobody can see.
     */
    var selectionMode by mutableStateOf(false)
        private set

    var banner by mutableStateOf<String?>(null)

    /** Cover bytes keyed by release id; null means "looked and there wasn't one". */
    val covers = mutableStateMapOf<Long, ByteArray?>()

    /** Archives already opened this session, so reopening one is instant. */
    private val details = HashMap<Long, ArchiveDetail>()

    /** Answers instantly: a running total the uploader keeps, corrected in the background. */
    var storedBytes by mutableStateOf(prefs.storedBytes)
        private set

    /** Thumbnail bytes keyed by asset id; null means "looked and there isn't one". */
    val thumbs = mutableStateMapOf<Long, ByteArray?>()

    /**
     * Thumbnails mean downloading the picture itself, so only a few are ever in flight. Without
     * this a folder of photos would open forty connections at once and stall the list it is
     * decorating.
     */
    private val thumbGate = Semaphore(3)

    var browseView by mutableStateOf(BrowseView.LIST)
        private set

    var autoUpload by mutableStateOf(prefs.autoUpload)
        private set
    var autoUploadWifiOnly by mutableStateOf(prefs.autoUploadWifiOnly)
        private set

    private var signInJob: Job? = null

    /** A granted token whose account setup has not finished yet. Survives a failed setup attempt. */
    private var pendingToken: String? = null

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
                // A token GitHub already granted is not thrown away by a flaky hand-off: if setup
                // failed last time, "Try again" resumes from here instead of asking for a new code.
                val token = pendingToken ?: run {
                    signInPhase = SignInPhase.Preparing("Asking GitHub for a sign-in code...")
                    val codes = DeviceFlow.requestCodes(clientId)
                    signInPhase = SignInPhase.AwaitingApproval(codes)
                    DeviceFlow.awaitToken(clientId, codes).also { pendingToken = it }
                }

                signInPhase = SignInPhase.Finishing("Setting up your storage...")
                val probe = GitHubClient(token)
                val user = whileNetworkReturns { probe.viewerLogin() }
                probe.owner = user

                // Storage is named after the account: <login>-storage, created if it is not there.
                if (!prefs.hasRepoName) prefs.repoName = Prefs.defaultRepoFor(user)
                probe.repo = prefs.repoName
                whileNetworkReturns { probe.ensureRepo(private = true) }

                // Generating the Keystore key can take a few hundred milliseconds.
                withContext(Dispatchers.IO) {
                    prefs.token = token
                    prefs.login = user
                    prefs.repoOwner = user
                }
                cachedClient = null
                cachedToken = null

                pendingToken = null
                login = user
                repoName = prefs.repoName
                signedIn = true
                AutoUpload.sync(getApplication())
                signInPhase = SignInPhase.Idle
                refreshArchives()
            } catch (e: Exception) {
                signInPhase = SignInPhase.Failed(friendly(e))
            }
        }
    }

    fun cancelSignIn() {
        signInJob?.cancel()
        signInJob = null
        pendingToken = null
        signInPhase = SignInPhase.Idle
    }

    /**
     * Retries through dropped connections.
     *
     * These calls run in the seconds right after the browser hands control back, which is exactly
     * when Samsung and Xiaomi still have the app frozen and its network blocked. GitHub has already
     * granted the token by then, so an IOException here means "wait for the network to come back",
     * not "sign-in failed". HTTP errors are [GitHubException], not [java.io.IOException], so a 401
     * or a 403 still fails immediately.
     */
    private suspend fun <T> whileNetworkReturns(attempts: Int = 6, block: suspend () -> T): T {
        var last: java.io.IOException? = null
        repeat(attempts) { attempt ->
            try {
                return block()
            } catch (e: java.io.IOException) {
                last = e
                delay(2000L * (attempt + 1))
            }
        }
        throw last ?: java.io.IOException("The network did not come back.")
    }

    fun signOut() {
        signInJob?.cancel()
        prefs.autoUpload = false
        autoUpload = false
        AutoUpload.sync(getApplication(), force = true)
        // Before the token goes, while there is still a client to build a repo from.
        repo()?.clearThumbnailCache()
        prefs.clear()
        cachedClient = null
        cachedToken = null
        covers.clear()
        thumbs.clear()
        details.clear()
        storedBytes = 0L
        archives = emptyList()
        detail = null
        signedIn = false
        login = null
        repoName = prefs.repoName
        signInPhase = SignInPhase.Idle
    }

    // ------------------------------------------------------------------ archives

    fun refreshArchives() {
        val repo = repo() ?: return
        details.clear()
        storedBytes = prefs.storedBytes
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
        val coverId = summary.coverAsset?.id
        viewModelScope.launch {
            val bytes = withContext(Dispatchers.IO) {
                // The listing already told us the asset id; only a summary that arrived without
                // its assets has to go and look it up.
                if (coverId != null) repo.assetBytes(coverId) else repo.coverBytes(summary.releaseId)
            }
            if (bytes != null) covers[summary.releaseId] = bytes
        }
    }

    /**
     * Turns the gallery backup on or off.
     *
     * Switching it on means "from now on", never "upload everything I have ever taken": the
     * watermark is reset to this moment, so nobody hands their whole camera roll to a phone
     * connection by flipping a switch. The caller is responsible for holding the media permission.
     */
    fun backUpGallery(enabled: Boolean) {
        if (enabled) {
            prefs.autoUploadSince = System.currentTimeMillis() / 1000L
            prefs.autoUploadLastId = 0L
        }
        prefs.autoUpload = enabled
        autoUpload = enabled
        val context = getApplication<Application>()
        AutoUpload.sync(context, force = true)
        if (enabled) AutoUpload.runNow(context)
        banner = if (enabled) {
            "New photos back up overnight."
        } else {
            "Photo backup is off."
        }
    }

    fun backUpOnWifiOnly(wifiOnly: Boolean) {
        prefs.autoUploadWifiOnly = wifiOnly
        autoUploadWifiOnly = wifiOnly
        AutoUpload.sync(getApplication(), force = true)
    }

    fun startSelecting() {
        selectionMode = true
    }

    fun toggleSelected(entry: ArchiveEntry) {
        selectionMode = true
        if (!selected.remove(entry.relativePath)) selected.add(entry.relativePath)
    }

    fun clearSelection() {
        selected.clear()
        selectionMode = false
    }

    fun selectAll(entries: List<ArchiveEntry>) {
        selectionMode = true
        selected.clear()
        selected.addAll(entries.map { it.relativePath })
    }

    private fun selectedEntries(): List<ArchiveEntry> {
        val chosen = selected.toSet()
        return detail?.entries.orEmpty().filter { it.relativePath in chosen }
    }

    /**
     * The actual files behind a selection.
     *
     * A folder is only the shape of the paths under it, so picking one means picking everything
     * it contains.
     */
    private fun selectedFiles(): List<ArchiveEntry> {
        val entries = selectedEntries()
        val files = detail?.entries.orEmpty().filterNot { it.isFolder }
        val out = LinkedHashMap<String, ArchiveEntry>()
        for (entry in entries) {
            if (entry.isFolder) {
                val prefix = entry.relativePath + "/"
                files.filter { it.relativePath.startsWith(prefix) }
                    .forEach { out[it.relativePath] = it }
            } else {
                out[entry.relativePath] = entry
            }
        }
        return out.values.toList()
    }

    /** Saves every ticked file into a folder the person picked. */
    fun downloadSelected(treeUri: Uri) {
        val repo = repo() ?: return
        val context = getApplication<Application>()
        val entries = selectedFiles()
        clearSelection()
        if (entries.isEmpty()) return

        viewModelScope.launch {
            val tree = withContext(Dispatchers.IO) { DocumentFile.fromTreeUri(context, treeUri) }
            if (tree == null) {
                banner = "Could not open that folder."
                return@launch
            }
            var started = 0
            for (entry in entries) {
                val target = withContext(Dispatchers.IO) {
                    tree.createFile(entry.contentType.ifEmpty { "application/octet-stream" }, entry.name)
                }
                if (target == null) continue
                TransferManager.startDownload(context, repo, entry, target.uri)
                started++
            }
            banner = if (started == 0) {
                "Could not save into that folder."
            } else {
                "Saving $started file${if (started == 1) "" else "s"}"
            }
        }
    }

    /** Every folder that exists inside the open archive, for picking a destination. */
    fun foldersInArchive(): List<String> {
        val current = detail ?: return emptyList()
        val out = sortedSetOf<String>()
        out.addAll(current.virtualFolders)
        for (entry in current.entries) {
            if (entry.isFolder) out.add(entry.relativePath)
            val parent = entry.relativePath.substringBeforeLast('/', "")
            if (parent.isNotEmpty()) out.add(parent)
        }
        return out.toList()
    }

    /**
     * Moves the ticked files into [folder] - empty means the top of the archive.
     *
     * Nothing is transferred: the archive records where a file lives, so moving a two-gigabyte
     * video is the same amount of work as moving a note.
     */
    fun moveSelected(folder: String) {
        val picked = selectedEntries()
        val target = folder.trim().trim('/')
        val mapping = HashMap<String, String>()
        val files = detail?.entries.orEmpty().filterNot { it.isFolder }

        for (entry in picked) {
            if (entry.isFolder) {
                // A folder keeps its shape: it and everything under it land inside the target.
                val prefix = entry.relativePath + "/"
                val name = entry.relativePath.substringAfterLast('/')
                val moved = if (target.isEmpty()) name else "$target/$name"
                files.filter { it.relativePath.startsWith(prefix) }.forEach { child ->
                    mapping[child.relativePath] = moved + "/" + child.relativePath.removePrefix(prefix)
                }
            } else {
                val name = entry.relativePath.substringAfterLast('/')
                mapping[entry.relativePath] = if (target.isEmpty()) name else "$target/$name"
            }
        }
        val count = mapping.size
        clearSelection()

        val changes = mapping.filter { (from, to) -> from != to }
        if (changes.isEmpty()) {
            banner = "Already there."
            return
        }
        applyRewrite(changes, "Moved $count file${if (count == 1) "" else "s"}")
    }

    /** Renames one file, or one folder and everything under it. */
    fun renameSelected(newName: String) {
        val current = detail ?: return
        val entry = selectedEntries().firstOrNull() ?: return
        val clean = newName.trim().trim('/')
        clearSelection()
        if (clean.isEmpty() || clean.contains('/')) {
            banner = "That name cannot be used."
            return
        }

        val parent = entry.relativePath.substringBeforeLast('/', "")
        val renamed = if (parent.isEmpty()) clean else "$parent/$clean"

        val mapping = if (entry.isFolder) {
            // A folder is only ever the shape of the paths under it, so renaming one means
            // rewriting every path that starts with it.
            val prefix = entry.relativePath + "/"
            current.entries
                .filterNot { it.isFolder }
                .filter { it.relativePath.startsWith(prefix) }
                .associate { it.relativePath to renamed + "/" + it.relativePath.removePrefix(prefix) }
        } else {
            mapOf(entry.relativePath to renamed)
        }
        applyRewrite(mapping, "Renamed to $clean")
    }

    private fun applyRewrite(mapping: Map<String, String>, done: String) {
        val repo = repo() ?: return
        val current = detail ?: return
        if (mapping.isEmpty()) return

        viewModelScope.launch {
            try {
                repo.rewritePaths(current, mapping)
                details.remove(current.summary.releaseId)
                val reloaded = repo.loadDetail(current.summary)
                details[current.summary.releaseId] = reloaded
                detail = reloaded
                banner = done
                refreshArchives()
            } catch (e: Exception) {
                banner = friendly(e)
            }
        }
    }

    /** Removes every ticked file from the open archive. */
    fun deleteSelected() {
        val repo = repo() ?: return
        val current = detail ?: return
        val entries = selectedFiles()
        clearSelection()
        if (entries.isEmpty()) return

        viewModelScope.launch {
            try {
                val archiveGone = repo.deleteEntries(current, entries)
                val count = entries.size
                banner = "Deleted $count file${if (count == 1) "" else "s"}"
                details.remove(current.summary.releaseId)
                usageDirty()
                if (archiveGone) {
                    detail = null
                    refreshArchives()
                } else {
                    detail = repo.loadDetail(current.summary)
                    details[current.summary.releaseId] = detail!!
                    refreshArchives()
                }
            } catch (e: Exception) {
                banner = friendly(e)
            }
        }
    }

    private fun usageDirty() {
        prefs.storageCheckedAt = 0L
        storedBytes = prefs.storedBytes
    }

    fun toggleBrowseView() {
        browseView = if (browseView == BrowseView.LIST) BrowseView.TILE else BrowseView.LIST
    }

    /** Fetches and caches the thumbnail for one image inside an archive, once. */
    fun loadThumb(entry: ArchiveEntry) {
        val key = entry.thumbKey ?: return
        if (thumbs.containsKey(key)) return
        val repo = repo() ?: return
        thumbs[key] = null
        viewModelScope.launch {
            val bytes = withContext(Dispatchers.IO) { thumbGate.withPermit { repo.thumbnail(entry) } }
            if (bytes != null) thumbs[key] = bytes
        }
    }

    fun thumbFor(entry: ArchiveEntry): ByteArray? = entry.thumbKey?.let { thumbs[it] }

    /**
     * Shows the running total straight away, then quietly checks it against what is really stored.
     *
     * The counter cannot see an upload made from the web app or another phone, so left alone it
     * would drift low forever. The walk costs one request and no waiting - nothing on screen is
     * blocked on it, the number simply corrects itself if it was wrong.
     */
    fun refreshStorageUsed(force: Boolean = false) {
        storedBytes = prefs.storedBytes

        // The counter is already right for anything this phone did. The walk only exists to catch
        // what it cannot see - an upload from the web app or another phone - so checking once a
        // day is plenty, and Settings costs nothing to open the rest of the time.
        val age = System.currentTimeMillis() - prefs.storageCheckedAt
        if (!force && age < STORAGE_RECHECK_MILLIS) return

        val repo = repo() ?: return
        viewModelScope.launch {
            runCatching { repo.totalBytes() }.onSuccess { actual ->
                prefs.storedBytes = actual
                prefs.storageCheckedAt = System.currentTimeMillis()
                storedBytes = actual
            }
        }
    }

    /**
     * Opening an archive shows whatever was loaded last time straight away, so going back and
     * forth between the grid and a folder costs nothing after the first visit. [refreshArchives]
     * and any upload or delete clear it.
     */
    fun openArchive(summary: ArchiveSummary) {
        val repo = repo() ?: return
        clearSelection()
        val remembered = details[summary.releaseId]
        detail = remembered
        currentPath = ""
        detailError = null
        detailLoading = remembered == null
        viewModelScope.launch {
            try {
                val loaded = repo.loadDetail(summary)
                details[summary.releaseId] = loaded
                detail = loaded
            } catch (e: Exception) {
                if (remembered == null) detailError = friendly(e)
            } finally {
                detailLoading = false
            }
        }
    }

    fun enterFolder(path: String) {
        clearSelection()
        currentPath = path
    }

    /** Returns false when already at the archive root, so the caller can pop the back stack. */
    fun goUp(): Boolean {
        if (selectionMode) {
            clearSelection()
            return true
        }
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
                details.remove(summary.releaseId)
                prefs.addStoredBytes(-summary.totalAssetBytes)
                storedBytes = prefs.storedBytes
                banner = "Deleted ${summary.sourceName}"
            } catch (e: Exception) {
                banner = friendly(e)
            }
        }
    }

    // ------------------------------------------------------------------ transfers

    /**
     * Picked files become one archive each.
     *
     * Bundling a multi-select into a single release made the drive show a folder the person never
     * created - `source_type` came out as `directory` and the card drew a folder thumbnail. A
     * folder should only appear when they actually picked one, so a five-file selection is five
     * archives. [TransferManager] runs them one at a time.
     */
    fun uploadFiles(uris: List<Uri>) {
        val uploader = uploader() ?: return
        if (uris.isEmpty()) return
        viewModelScope.launch {
            val items = withContext(Dispatchers.IO) { Picking.fromFiles(getApplication(), uris) }
            if (items.isEmpty()) {
                banner = "Nothing to upload."
                return@launch
            }
            for (item in items) {
                startUpload(uploader, item.relativePath, listOf(item), emptyList())
            }
            if (items.size > 1) banner = "Uploading ${items.size} files"
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
        // A dropped connection surfaces as an OkHttp/DNS exception whose message is a bare hostname.
        if (e is java.io.IOException) return "No internet connection. Check your Wi-Fi and try again."
        val message = e.message ?: "Something went wrong."
        return when {
            message.contains("401") -> "GitHub rejected the sign-in. Sign out and sign in again."
            message.contains("404") -> "That storage is gone. Check Settings."
            message.contains("rate limit", ignoreCase = true) ->
                "GitHub is rate limiting this account. Try again in a few minutes."
            else -> message
        }
    }

    private companion object {
        /** How stale the checked total may get before Settings verifies it again. */
        const val STORAGE_RECHECK_MILLIS = 24L * 60L * 60L * 1000L
    }
}
