package com.panelyra.tablet.ui

import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.panelyra.tablet.BuildConfig

@Composable
fun TabletLibraryCleanupCard(viewModel: MainViewModel) {
    val state by viewModel.cleanupState.collectAsStateWithLifecycle()
    var confirm by remember { mutableStateOf(false) }
    OutlinedCard(Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp)) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("Current library", style = MaterialTheme.typography.titleLarge)
            Text("Keep Rebirth: Monarch of the Dead and Return of the Grim Reaper Assassin, with their current chapters and covers.")
            Text(state.message, color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (state.busy) LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            if (!state.done) Button(onClick = { confirm = true }, enabled = state.ready && !state.busy, modifier = Modifier.heightIn(min = 48.dp)) { Text("Remove other books & old versions") }
            if (state.restoreAvailable) TextButton(onClick = viewModel::undoLibraryCleanup) { Text("Undo cleanup / restore Trash") }
            Text("Tablet only. Your Mac library and the two current books are kept. Removed files stay in recoverable Trash.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
    if (confirm) AlertDialog(onDismissRequest = { confirm = false }, title = { Text("Keep only the two current books?") },
        text = { Text("All 58 replacement chapters and both covers must pass verification first. Other tablet books, duplicate editions and unused old chapter files will leave Reader. The Mac is not changed. You can undo this cleanup in Settings.") },
        confirmButton = { Button(onClick = { confirm = false; viewModel.finishLibraryCleanup() }) { Text("Clean tablet library") } },
        dismissButton = { TextButton(onClick = { confirm = false }) { Text("Cancel") } })
}

@Composable
fun AppUpdateCard(viewModel: MainViewModel) {
    val state by viewModel.appUpdateState.collectAsStateWithLifecycle()
    OutlinedCard(Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp)) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("App updates", style = MaterialTheme.typography.titleLarge)
            Text("Installed: Panelyra ${BuildConfig.VERSION_NAME}", color = MaterialTheme.colorScheme.onSurfaceVariant)
            Row(Modifier.fillMaxWidth(), verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                Text("Receive new releases from my Mac", Modifier.weight(1f))
                Switch(checked = state.enabled, onCheckedChange = viewModel::setAutomaticAppUpdates)
            }
            Text(state.message, style = MaterialTheme.typography.bodyMedium)
            Text("Only newer Panelyra APKs with the same signing identity are accepted. Books, covers and reading progress stay saved.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (state.available) {
                Text("Version ${state.version} is downloaded and ready.", color = MaterialTheme.colorScheme.primary)
                Button(onClick = viewModel::installAppUpdate) { Text("Install update") }
                Text("Android may first ask you to allow app updates from Panelyra. Return here and tap Install update again after allowing it.", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}
