@file:OptIn(androidx.compose.material3.ExperimentalMaterial3Api::class)

package com.harshiv.githubdrive.ui

import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.clickable
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.CloudUpload
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Folder
import androidx.compose.material.icons.filled.GridView
import androidx.compose.material.icons.filled.Inbox
import androidx.compose.material.icons.automirrored.filled.Logout
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.OpenInNew
import androidx.compose.material.icons.filled.PhotoLibrary
import androidx.compose.material.icons.filled.PieChart
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material.icons.filled.SwapVert
import androidx.compose.material.icons.automirrored.filled.ViewList
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.ListItem
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.harshiv.githubdrive.drive.ArchiveEntry
import com.harshiv.githubdrive.drive.ArchiveSummary
import com.harshiv.githubdrive.transfer.AutoUpload
import com.harshiv.githubdrive.transfer.Transfer
import com.harshiv.githubdrive.transfer.TransferKind
import com.harshiv.githubdrive.transfer.TransferState

// ---------------------------------------------------------------------- archives

@Composable
fun ArchivesScreen(
    vm: AppViewModel,
    onOpen: (ArchiveSummary) -> Unit,
    onTransfers: () -> Unit,
    onSettings: () -> Unit,
    onPickFiles: () -> Unit,
    onPickFolder: () -> Unit
) {
    var menuOpen by remember { mutableStateOf(false) }
    var pendingDelete by remember { mutableStateOf<ArchiveSummary?>(null) }

    LaunchedEffect(Unit) {
        if (vm.archives.isEmpty()) vm.refreshArchives()
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Your files") },
                actions = {
                    IconButton(onClick = { vm.refreshArchives() }) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Refresh")
                    }
                    IconButton(onClick = onTransfers) {
                        Icon(Icons.Filled.SwapVert, contentDescription = "Transfers")
                    }
                    IconButton(onClick = onSettings) {
                        Icon(Icons.Filled.Settings, contentDescription = "Settings")
                    }
                }
            )
        },
        floatingActionButton = {
            Box {
                ExtendedFloatingActionButton(
                    onClick = { menuOpen = true },
                    icon = { Icon(Icons.Filled.Add, contentDescription = null) },
                    text = { Text("Upload") }
                )
                DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                    DropdownMenuItem(
                        text = { Text("Upload files") },
                        onClick = { menuOpen = false; onPickFiles() }
                    )
                    DropdownMenuItem(
                        text = { Text("Upload a folder") },
                        onClick = { menuOpen = false; onPickFolder() }
                    )
                }
            }
        }
    ) { padding ->
        Column(modifier = Modifier.fillMaxSize().padding(padding)) {
            vm.archivesError?.let { error ->
                Text(
                    error,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                    modifier = Modifier.padding(horizontal = 16.dp, vertical = 8.dp)
                )
            }

            if (vm.archivesLoading && vm.archives.isEmpty()) {
                Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }
            } else if (vm.archives.isEmpty()) {
                EmptyState(
                    icon = Icons.Filled.Inbox,
                    title = "Nothing stored yet",
                    subtitle = "Tap Upload to put your first files into your storage."
                )
            } else {
                LazyVerticalGrid(
                    columns = GridCells.Fixed(2),
                    contentPadding = PaddingValues(12.dp),
                    horizontalArrangement = Arrangement.spacedBy(12.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                    modifier = Modifier.fillMaxSize()
                ) {
                    items(vm.archives, key = { it.releaseId }) { archive ->
                        ArchiveCard(
                            archive = archive,
                            cover = vm.covers[archive.releaseId],
                            onLoadCover = { vm.loadCover(archive) },
                            onOpen = { onOpen(archive) },
                            onDelete = { pendingDelete = archive }
                        )
                    }
                    if (vm.hasMore) {
                        item {
                            OutlinedButton(
                                onClick = { vm.loadMoreArchives() },
                                modifier = Modifier.fillMaxWidth()
                            ) { Text("Load more") }
                        }
                    }
                }
            }
        }
    }

    pendingDelete?.let { target ->
        AlertDialog(
            onDismissRequest = { pendingDelete = null },
            title = { Text("Delete this archive?") },
            text = {
                Text("\"${target.sourceName}\" and all ${target.totalItems} files in it will be removed from your storage. This cannot be undone.")
            },
            confirmButton = {
                TextButton(onClick = {
                    vm.deleteArchive(target)
                    pendingDelete = null
                }) { Text("Delete") }
            },
            dismissButton = {
                TextButton(onClick = { pendingDelete = null }) { Text("Keep") }
            }
        )
    }
}

@Composable
private fun ArchiveCard(
    archive: ArchiveSummary,
    cover: ByteArray?,
    onLoadCover: () -> Unit,
    onOpen: () -> Unit,
    onDelete: () -> Unit
) {
    LaunchedEffect(archive.releaseId) { onLoadCover() }

    var menuOpen by remember { mutableStateOf(false) }
    val dominantKind = archive.kinds.maxByOrNull { it.value }?.takeIf { it.value > 0 }?.key ?: "other"

    Card(
        modifier = Modifier.fillMaxWidth().clickable { onOpen() },
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
    ) {
        CoverThumb(
            bytes = cover,
            fallbackKind = if (archive.sourceType == "directory") "folder" else dominantKind,
            modifier = Modifier.fillMaxWidth().aspectRatio(1f)
        )
        Row(
            modifier = Modifier.fillMaxWidth().padding(start = 12.dp, end = 4.dp, top = 8.dp, bottom = 8.dp),
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    archive.sourceName,
                    style = MaterialTheme.typography.titleSmall,
                    maxLines = 1,
                    overflow = TextOverflow.Ellipsis
                )
                Text(
                    "${archive.totalItems} item${if (archive.totalItems == 1) "" else "s"} - ${formatBytes(archive.totalAssetBytes)}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    maxLines = 1
                )
                Text(
                    formatDate(archive.createdAt),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
            Box {
                IconButton(onClick = { menuOpen = true }) {
                    Icon(Icons.Filled.MoreVert, contentDescription = "More")
                }
                DropdownMenu(expanded = menuOpen, onDismissRequest = { menuOpen = false }) {
                    DropdownMenuItem(
                        text = { Text("Delete") },
                        leadingIcon = { Icon(Icons.Filled.Delete, contentDescription = null) },
                        onClick = { menuOpen = false; onDelete() }
                    )
                }
            }
        }
    }
}

// ---------------------------------------------------------------------- browse

@Composable
fun BrowseScreen(
    vm: AppViewModel,
    onBack: () -> Unit,
    onSave: (ArchiveEntry) -> Unit
) {
    val detail = vm.detail
    val title = detail?.summary?.sourceName ?: "Opening..."

    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Column {
                        Text(title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                        if (vm.currentPath.isNotEmpty()) {
                            Text(
                                vm.currentPath,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis
                            )
                        }
                    }
                },
                navigationIcon = {
                    IconButton(onClick = { if (!vm.goUp()) onBack() }) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
                actions = {
                    val tiled = vm.browseView == BrowseView.TILE
                    IconButton(onClick = { vm.toggleBrowseView() }) {
                        Icon(
                            if (tiled) Icons.AutoMirrored.Filled.ViewList else Icons.Filled.GridView,
                            contentDescription = if (tiled) "Show as a list" else "Show as tiles"
                        )
                    }
                }
            )
        }
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when {
                vm.detailLoading -> Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                    CircularProgressIndicator()
                }

                vm.detailError != null -> EmptyState(
                    icon = Icons.Filled.Inbox,
                    title = "Could not open this archive",
                    subtitle = vm.detailError ?: ""
                )

                detail == null -> Unit

                else -> {
                    val children = detail.childrenOf(vm.currentPath)
                    if (children.isEmpty()) {
                        EmptyState(
                            icon = Icons.Filled.Folder,
                            title = "Empty folder",
                            subtitle = "There is nothing stored here."
                        )
                    } else {
                        val open: (ArchiveEntry) -> Unit = { entry ->
                            if (entry.isFolder) vm.enterFolder(entry.relativePath) else onSave(entry)
                        }
                        when (vm.browseView) {
                            BrowseView.LIST -> LazyColumn(Modifier.fillMaxSize()) {
                                if (detail.encrypted) {
                                    item { EncryptedNotice() }
                                }
                                items(children, key = { it.relativePath }) { entry ->
                                    EntryRow(entry, vm, onClick = { open(entry) })
                                    HorizontalDivider()
                                }
                            }

                            BrowseView.TILE -> LazyVerticalGrid(
                                columns = GridCells.Adaptive(112.dp),
                                modifier = Modifier.fillMaxSize(),
                                contentPadding = PaddingValues(12.dp),
                                horizontalArrangement = Arrangement.spacedBy(8.dp),
                                verticalArrangement = Arrangement.spacedBy(8.dp)
                            ) {
                                if (detail.encrypted) {
                                    item(span = { GridItemSpan(maxLineSpan) }) { EncryptedNotice() }
                                }
                                items(children, key = { it.relativePath }) { entry ->
                                    EntryTile(entry, vm, onClick = { open(entry) })
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun EncryptedNotice() {
    Text(
        "This archive is encrypted. You can see what is in it, but saving files needs the web app.",
        style = MaterialTheme.typography.bodySmall,
        color = MaterialTheme.colorScheme.error,
        modifier = Modifier.padding(16.dp)
    )
}

/**
 * Asks for a thumbnail for one entry.
 *
 * Lazy layouts only compose what is on screen, so this fetches pictures the person is actually
 * looking at rather than every image in the folder.
 */
@Composable
private fun rememberThumb(entry: ArchiveEntry, vm: AppViewModel): ByteArray? {
    LaunchedEffect(entry.relativePath) { vm.loadThumb(entry) }
    return vm.thumbFor(entry)
}

@Composable
private fun EntryTile(entry: ArchiveEntry, vm: AppViewModel, onClick: () -> Unit) {
    val thumb = rememberThumb(entry, vm)
    Column(
        modifier = Modifier.clip(RoundedCornerShape(12.dp)).clickable { onClick() }
    ) {
        CoverThumb(
            bytes = thumb,
            fallbackKind = entry.kind,
            modifier = Modifier.fillMaxWidth().aspectRatio(1f).clip(RoundedCornerShape(12.dp))
        )
        Spacer(Modifier.height(6.dp))
        Text(
            entry.name,
            style = MaterialTheme.typography.bodySmall,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(horizontal = 4.dp)
        )
        Text(
            if (entry.isFolder) "Folder" else formatBytes(entry.originalSize),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            maxLines = 1,
            modifier = Modifier.padding(horizontal = 4.dp, vertical = 2.dp)
        )
    }
}

@Composable
private fun EntryRow(entry: ArchiveEntry, vm: AppViewModel, onClick: () -> Unit) {
    val thumb = rememberThumb(entry, vm)
    ListItem(
        headlineContent = {
            Text(entry.name, maxLines = 1, overflow = TextOverflow.Ellipsis)
        },
        supportingContent = {
            Text(
                if (entry.isFolder) "Folder" else formatBytes(entry.originalSize),
                style = MaterialTheme.typography.bodySmall
            )
        },
        leadingContent = {
            if (thumb != null) {
                CoverThumb(
                    bytes = thumb,
                    fallbackKind = entry.kind,
                    modifier = Modifier.size(40.dp).clip(RoundedCornerShape(8.dp))
                )
            } else {
                Icon(
                    iconForKind(entry.kind),
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.onSurfaceVariant
                )
            }
        },
        trailingContent = {
            if (!entry.isFolder) {
                Icon(
                    Icons.Filled.Download,
                    contentDescription = "Save",
                    tint = MaterialTheme.colorScheme.primary
                )
            }
        },
        modifier = Modifier.clickable { onClick() }
    )
}

// ---------------------------------------------------------------------- transfers

@Composable
fun TransfersScreen(
    transfers: List<Transfer>,
    onCancel: (Long) -> Unit,
    onClear: () -> Unit,
    onBack: () -> Unit
) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Transfers") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                },
                actions = {
                    TextButton(onClick = onClear) { Text("Clear finished") }
                }
            )
        }
    ) { padding ->
        if (transfers.isEmpty()) {
            Box(Modifier.fillMaxSize().padding(padding), contentAlignment = Alignment.Center) {
                EmptyState(
                    icon = Icons.Filled.SwapVert,
                    title = "Nothing in flight",
                    subtitle = "Uploads and downloads show their progress here."
                )
            }
        } else {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding),
                contentPadding = PaddingValues(vertical = 8.dp)
            ) {
                items(transfers, key = { it.id }) { transfer ->
                    TransferRow(transfer, onCancel)
                    HorizontalDivider()
                }
            }
        }
    }
}

@Composable
private fun TransferRow(transfer: Transfer, onCancel: (Long) -> Unit) {
    Column(Modifier.fillMaxWidth().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(
                if (transfer.kind == TransferKind.UPLOAD) Icons.Filled.CloudUpload else Icons.Filled.Download,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.onSurfaceVariant
            )
            Spacer(Modifier.size(12.dp))
            Column(Modifier.weight(1f)) {
                Text(transfer.title, maxLines = 1, overflow = TextOverflow.Ellipsis)
                Text(
                    transfer.error ?: transfer.detail,
                    style = MaterialTheme.typography.bodySmall,
                    color = if (transfer.state == TransferState.FAILED) {
                        MaterialTheme.colorScheme.error
                    } else {
                        MaterialTheme.colorScheme.onSurfaceVariant
                    },
                    maxLines = 2,
                    overflow = TextOverflow.Ellipsis
                )
            }
            if (transfer.state == TransferState.RUNNING) {
                TextButton(onClick = { onCancel(transfer.id) }) { Text("Stop") }
            }
        }
        if (transfer.state == TransferState.RUNNING) {
            Spacer(Modifier.height(10.dp))
            LinearProgressIndicator(
                progress = { transfer.fraction },
                modifier = Modifier.fillMaxWidth()
            )
            Spacer(Modifier.height(6.dp))
            Text(
                "${formatBytes(transfer.bytesDone)} of ${formatBytes(transfer.bytesTotal)}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant
            )
        }
    }
}

// ---------------------------------------------------------------------- settings

@Composable
fun SettingsScreen(
    login: String?,
    repoName: String,
    storedBytes: Long,
    onRefreshStorageUsed: () -> Unit,
    autoUpload: Boolean,
    autoUploadWifiOnly: Boolean,
    onAutoUpload: (Boolean) -> Unit,
    onAutoUploadWifiOnly: (Boolean) -> Unit,
    onOpenRepo: () -> Unit,
    onSignOut: () -> Unit,
    onBack: () -> Unit
) {
    var confirmSignOut by remember { mutableStateOf(false) }
    val context = LocalContext.current

    LaunchedEffect(Unit) { onRefreshStorageUsed() }

    // Reading the camera roll is only asked for at the moment someone switches the backup on.
    val askForGallery = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { _ ->
        if (AutoUpload.canReadGallery(context)) {
            onAutoUpload(true)
        } else {
            Toast.makeText(
                context,
                "Photo backup needs permission to read your gallery.",
                Toast.LENGTH_LONG
            ).show()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("Settings") },
                navigationIcon = {
                    IconButton(onClick = onBack) {
                        Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back")
                    }
                }
            )
        }
    ) { padding ->
        Column(Modifier.fillMaxSize().padding(padding)) {
            ListItem(
                headlineContent = { Text("Signed in as") },
                supportingContent = { Text(login ?: "unknown") }
            )
            HorizontalDivider()
            ListItem(
                headlineContent = { Text("Your storage") },
                supportingContent = { Text("$repoName - private, only you can see it") },
                trailingContent = {
                    Icon(Icons.Filled.OpenInNew, contentDescription = null)
                },
                modifier = Modifier.clickable { onOpenRepo() }
            )
            ListItem(
                headlineContent = { Text("Space used") },
                supportingContent = { Text(formatBytes(storedBytes)) },
                leadingContent = { Icon(Icons.Filled.PieChart, contentDescription = null) }
            )
            HorizontalDivider()
            ListItem(
                headlineContent = { Text("Back up my photos") },
                supportingContent = {
                    Text(
                        if (autoUpload) {
                            "Photos and videos you take go up on their own, overnight."
                        } else {
                            "Photos and videos you take from now on will go up overnight."
                        }
                    )
                },
                leadingContent = { Icon(Icons.Filled.PhotoLibrary, contentDescription = null) },
                trailingContent = {
                    Switch(
                        checked = autoUpload,
                        onCheckedChange = { wanted ->
                            if (!wanted) {
                                onAutoUpload(false)
                            } else if (AutoUpload.canReadGallery(context)) {
                                onAutoUpload(true)
                            } else {
                                askForGallery.launch(AutoUpload.mediaPermissions())
                            }
                        }
                    )
                }
            )
            if (autoUpload) {
                ListItem(
                    headlineContent = { Text("Only on Wi-Fi") },
                    supportingContent = { Text("Leave this on to keep backups off your mobile data.") },
                    trailingContent = {
                        Switch(checked = autoUploadWifiOnly, onCheckedChange = onAutoUploadWifiOnly)
                    }
                )
            }
            HorizontalDivider()
            ListItem(
                headlineContent = { Text("Sign out") },
                leadingContent = { Icon(Icons.AutoMirrored.Filled.Logout, contentDescription = null) },
                modifier = Modifier.clickable { confirmSignOut = true }
            )
            HorizontalDivider()
            Text(
                "Your files live in private storage that belongs to you. This app talks to it " +
                    "directly - nothing is uploaded to any other server, and nobody else can see it.",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.padding(16.dp)
            )
            Spacer(Modifier.weight(1f))
            Text(
                "Made by Harshiv Shah",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
                modifier = Modifier.fillMaxWidth().padding(16.dp)
            )
        }
    }

    if (confirmSignOut) {
        AlertDialog(
            onDismissRequest = { confirmSignOut = false },
            title = { Text("Sign out?") },
            text = { Text("Your files stay where they are - nothing is deleted. You will need to approve a new sign-in code to get back in.") },
            confirmButton = {
                TextButton(onClick = { confirmSignOut = false; onSignOut() }) { Text("Sign out") }
            },
            dismissButton = {
                TextButton(onClick = { confirmSignOut = false }) { Text("Cancel") }
            }
        )
    }
}
