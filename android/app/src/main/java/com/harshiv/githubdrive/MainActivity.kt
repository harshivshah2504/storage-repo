package com.harshiv.githubdrive

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.harshiv.githubdrive.drive.ArchiveEntry
import com.harshiv.githubdrive.drive.ArchiveSummary
import com.harshiv.githubdrive.transfer.TransferManager
import com.harshiv.githubdrive.ui.ArchivesScreen
import com.harshiv.githubdrive.ui.BrowseScreen
import com.harshiv.githubdrive.ui.AppViewModel
import com.harshiv.githubdrive.ui.GitHubDriveTheme
import com.harshiv.githubdrive.ui.SettingsScreen
import com.harshiv.githubdrive.ui.SignInScreen
import com.harshiv.githubdrive.ui.TransfersScreen
import androidx.activity.compose.BackHandler

private enum class Screen { ARCHIVES, BROWSE, TRANSFERS, SETTINGS }

class MainActivity : ComponentActivity() {

    /** Files shared in from another app, handed to the view model once it is signed in. */
    private val sharedUris = androidx.compose.runtime.mutableStateListOf<Uri>()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        sharedUris.addAll(collectSharedUris(intent))
        requestNotificationPermission()

        setContent {
            GitHubDriveTheme {
                AppRoot(
                    pendingShareCount = sharedUris.size,
                    takeSharedUris = {
                        val out = sharedUris.toList()
                        sharedUris.clear()
                        out
                    }
                )
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        sharedUris.addAll(collectSharedUris(intent))
    }

    private fun collectSharedUris(intent: Intent?): List<Uri> {
        if (intent == null) return emptyList()
        return when (intent.action) {
            Intent.ACTION_SEND -> {
                val uri = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM)
                }
                listOfNotNull(uri)
            }
            Intent.ACTION_SEND_MULTIPLE -> {
                val list = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
                    intent.getParcelableArrayListExtra(Intent.EXTRA_STREAM, Uri::class.java)
                } else {
                    @Suppress("DEPRECATION")
                    intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM)
                }
                list?.filterNotNull() ?: emptyList()
            }
            else -> emptyList()
        }
    }

    private fun requestNotificationPermission() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        val granted = ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted) {
            // Only used for the transfer progress notification; the app works without it.
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }
}

@Composable
private fun AppRoot(pendingShareCount: Int, takeSharedUris: () -> List<Uri>) {
    val vm: AppViewModel = viewModel()
    val context = LocalContext.current
    val transfers by TransferManager.transfers.collectAsState()

    var screen by remember { mutableStateOf(Screen.ARCHIVES) }
    var pendingSave by remember { mutableStateOf<ArchiveEntry?>(null) }

    val pickFiles = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        if (!uris.isNullOrEmpty()) vm.uploadFiles(uris)
    }

    val pickFolder = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { uri ->
        if (uri != null) vm.uploadFolder(uri)
    }

    val saveFile = rememberLauncherForActivityResult(
        ActivityResultContracts.CreateDocument("*/*")
    ) { uri ->
        val entry = pendingSave
        pendingSave = null
        if (uri != null && entry != null) vm.download(entry, uri)
    }

    // Anything shared in from another app queues as soon as we have a signed-in session.
    LaunchedEffect(vm.signedIn, pendingShareCount) {
        if (vm.signedIn && pendingShareCount > 0) {
            val shared = takeSharedUris()
            if (shared.isNotEmpty()) vm.uploadFiles(shared)
        }
    }

    LaunchedEffect(vm.banner) {
        vm.banner?.let { message ->
            android.widget.Toast.makeText(context, message, android.widget.Toast.LENGTH_SHORT).show()
            vm.banner = null
        }
    }

    if (!vm.signedIn) {
        SignInScreen(
            phase = vm.signInPhase,
            onStart = { vm.startSignIn() },
            onCancel = { vm.cancelSignIn() }
        )
        return
    }

    BackHandler(enabled = screen != Screen.ARCHIVES) {
        if (screen == Screen.BROWSE && vm.goUp()) return@BackHandler
        screen = Screen.ARCHIVES
    }

    when (screen) {
        Screen.ARCHIVES -> ArchivesScreen(
            vm = vm,
            onOpen = { summary: ArchiveSummary ->
                vm.openArchive(summary)
                screen = Screen.BROWSE
            },
            onTransfers = { screen = Screen.TRANSFERS },
            onSettings = { screen = Screen.SETTINGS },
            onPickFiles = { pickFiles.launch(arrayOf("*/*")) },
            onPickFolder = { pickFolder.launch(null) }
        )

        Screen.BROWSE -> BrowseScreen(
            vm = vm,
            onBack = { screen = Screen.ARCHIVES },
            onSave = { entry ->
                pendingSave = entry
                saveFile.launch(entry.name)
            }
        )

        Screen.TRANSFERS -> TransfersScreen(
            transfers = transfers,
            onCancel = { TransferManager.cancel(it) },
            onClear = { TransferManager.clearFinished() },
            onBack = { screen = Screen.ARCHIVES }
        )

        Screen.SETTINGS -> SettingsScreen(
            login = vm.login,
            repoName = vm.repoName,
            onOpenRepo = {
                val url = "https://github.com/${vm.login}/${vm.repoName}"
                runCatching {
                    context.startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url)))
                }
            },
            onSignOut = {
                vm.signOut()
                screen = Screen.ARCHIVES
            },
            onBack = { screen = Screen.ARCHIVES }
        )
    }
}
