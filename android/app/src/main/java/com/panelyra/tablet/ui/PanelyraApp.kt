package com.panelyra.tablet.ui

import android.app.Activity
import androidx.compose.foundation.gestures.detectTapGestures
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.material.icons.filled.Settings
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import android.webkit.CookieManager
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.GridItemSpan
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items as gridItems
import androidx.compose.foundation.pager.HorizontalPager
import androidx.compose.foundation.pager.rememberPagerState
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.automirrored.filled.MenuBook
import androidx.compose.material.icons.filled.Computer
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Download
import androidx.compose.material.icons.filled.Fullscreen
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.NavigationRail
import androidx.compose.material3.NavigationRailItem
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Switch
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.foundation.lazy.grid.rememberLazyGridState
import androidx.compose.runtime.produceState
import com.panelyra.tablet.download.DownloadActivity
import com.panelyra.tablet.download.DOWNLOAD_NOTES
import com.panelyra.tablet.download.waitDuration
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.platform.LocalConfiguration
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import coil3.compose.AsyncImage
import com.panelyra.tablet.data.ChapterEntity
import com.panelyra.tablet.data.DownloadJobEntity
import com.panelyra.tablet.data.SeriesSummary
import kotlinx.coroutines.flow.distinctUntilChanged
import org.json.JSONArray
import org.json.JSONTokener

private val PanelyraColors = androidx.compose.material3.darkColorScheme(
    primary = androidx.compose.ui.graphics.Color(0xFF57D5C5),
    onPrimary = androidx.compose.ui.graphics.Color(0xFF09241E),
    secondary = androidx.compose.ui.graphics.Color(0xFF9FBDB7),
    background = androidx.compose.ui.graphics.Color(0xFF0E1417),
    surface = androidx.compose.ui.graphics.Color(0xFF151E22),
    surfaceVariant = androidx.compose.ui.graphics.Color(0xFF182227),
    onBackground = androidx.compose.ui.graphics.Color(0xFFE9EEEF),
    onSurface = androidx.compose.ui.graphics.Color(0xFFE9EEEF),
    onSurfaceVariant = androidx.compose.ui.graphics.Color(0xFFAEBcBE),
    surfaceContainerLowest = androidx.compose.ui.graphics.Color(0xFF0C0D0F),
    surfaceContainerLow = androidx.compose.ui.graphics.Color(0xFF151E22),
    surfaceContainer = androidx.compose.ui.graphics.Color(0xFF182227),
    surfaceContainerHigh = androidx.compose.ui.graphics.Color(0xFF233338),
    surfaceContainerHighest = androidx.compose.ui.graphics.Color(0xFF2B3C40),
    primaryContainer = androidx.compose.ui.graphics.Color(0xFF1A2A2B),
    onPrimaryContainer = androidx.compose.ui.graphics.Color(0xFFBFEFE5),
    secondaryContainer = androidx.compose.ui.graphics.Color(0xFF233338),
    onSecondaryContainer = androidx.compose.ui.graphics.Color(0xFFD8E8E5),
    outline = androidx.compose.ui.graphics.Color(0xFF718A88),
    outlineVariant = androidx.compose.ui.graphics.Color(0xFF334247),
)
private val PanelyraLightColors = androidx.compose.material3.lightColorScheme(
    primary = androidx.compose.ui.graphics.Color(0xFF176B5D),
    onPrimary = androidx.compose.ui.graphics.Color.White,
    background = androidx.compose.ui.graphics.Color(0xFFF5F9F8),
    surface = androidx.compose.ui.graphics.Color.White,
    surfaceVariant = androidx.compose.ui.graphics.Color(0xFFE7EFED),
    onBackground = androidx.compose.ui.graphics.Color(0xFF182624),
    onSurface = androidx.compose.ui.graphics.Color(0xFF182624),
    onSurfaceVariant = androidx.compose.ui.graphics.Color(0xFF5D6D6A),
    surfaceContainerLowest = androidx.compose.ui.graphics.Color.White,
    surfaceContainerLow = androidx.compose.ui.graphics.Color(0xFFF0F6F4),
    surfaceContainer = androidx.compose.ui.graphics.Color(0xFFEAF2EF),
    surfaceContainerHigh = androidx.compose.ui.graphics.Color(0xFFE2ECE8),
    surfaceContainerHighest = androidx.compose.ui.graphics.Color(0xFFD8E6E0),
    primaryContainer = androidx.compose.ui.graphics.Color(0xFFD7EFE8),
    onPrimaryContainer = androidx.compose.ui.graphics.Color(0xFF12392F),
    secondaryContainer = androidx.compose.ui.graphics.Color(0xFFE2ECE8),
    onSecondaryContainer = androidx.compose.ui.graphics.Color(0xFF263B35),
    outline = androidx.compose.ui.graphics.Color(0xFF778B84),
    outlineVariant = androidx.compose.ui.graphics.Color(0xFFCDDDD6),
)

private enum class RootTab { Reader, Downloader, Settings }

@Composable
private fun tabletLayout(): Boolean = LocalConfiguration.current.screenWidthDp >= 720

@Composable
fun PanelyraApp(viewModel: MainViewModel, chooseCbzFiles: () -> Unit, scanPairingCode: () -> Unit) {
    val context = LocalContext.current
    val preferences = remember { context.getSharedPreferences("panelyra_reader", android.content.Context.MODE_PRIVATE) }
    var dark by remember { mutableStateOf(preferences.getBoolean("dark", true)) }
    MaterialTheme(colorScheme = if (dark) PanelyraColors else PanelyraLightColors,
        shapes = androidx.compose.material3.Shapes(extraSmall = RoundedCornerShape(4.dp), small = RoundedCornerShape(6.dp),
            medium = RoundedCornerShape(8.dp), large = RoundedCornerShape(12.dp), extraLarge = RoundedCornerShape(16.dp))) {
        val opened by viewModel.openedChapter.collectAsStateWithLifecycle()
        val pages by viewModel.readerPages.collectAsStateWithLifecycle()
        if (opened != null) {
            ImmersiveReader(opened!!, pages, viewModel)
        } else {
            Root(viewModel, chooseCbzFiles, scanPairingCode, dark) {
                dark = it; preferences.edit().putBoolean("dark", it).apply()
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Root(viewModel: MainViewModel, chooseCbzFiles: () -> Unit, scanPairingCode: () -> Unit, dark: Boolean, setDark: (Boolean) -> Unit) {
    var tab by remember { mutableStateOf(RootTab.Reader) }
    val tablet = tabletLayout()
    val message by viewModel.message.collectAsStateWithLifecycle()
    val snackbar = remember { SnackbarHostState() }
    LaunchedEffect(message) {
        message?.let { snackbar.showSnackbar(it); viewModel.message.value = null }
    }
    Scaffold(
        topBar = { if (!tablet) TopAppBar(title = { Column {
            Text("Panelyra", style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold)
            Text(if (tab == RootTab.Reader) "Your stories, offline" else "Download your next read", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        } },
            actions = { IconButton(onClick = { tab = RootTab.Settings }, modifier = Modifier.size(56.dp)) { Icon(Icons.Default.Settings, "Settings & connections") } }) },
        snackbarHost = { SnackbarHost(snackbar) },
        bottomBar = {
            if (!tablet) NavigationBar {
                NavigationBarItem(
                    selected = tab == RootTab.Reader, onClick = { tab = RootTab.Reader },
                    icon = { Icon(Icons.AutoMirrored.Filled.MenuBook, null) }, label = { Text("Reader") },
                )
                NavigationBarItem(
                    selected = tab == RootTab.Downloader, onClick = { tab = RootTab.Downloader },
                    icon = { Icon(Icons.Default.Download, null) }, label = { Text("Downloader") },
                )
                NavigationBarItem(selected = tab == RootTab.Settings, onClick = { tab = RootTab.Settings },
                    icon = { Icon(Icons.Default.Settings, null) }, label = { Text("Settings") })
            }
        },
    ) { padding ->
        Row(Modifier.padding(padding).fillMaxSize()) {
            if (tablet) MacStyleSidebar(tab.name, { tab = RootTab.valueOf(it) })
            Box(Modifier.weight(1f).fillMaxHeight()) {
                when (tab) {
                    RootTab.Reader -> ReaderLibrary(viewModel, chooseCbzFiles)
                    RootTab.Downloader -> Downloader(viewModel, scanPairingCode)
                    RootTab.Settings -> SettingsPage(viewModel, dark, setDark, chooseCbzFiles)
                }
            }
        }
    }
}

@Composable
private fun SettingsPage(viewModel: MainViewModel, dark: Boolean, setDark: (Boolean) -> Unit, chooseCbzFiles: () -> Unit) {
        val paired by viewModel.paired.collectAsStateWithLifecycle()
        val discovered by viewModel.discoveredMac.collectAsStateWithLifecycle()
        val status by viewModel.syncStatus.collectAsStateWithLifecycle()
        val transfers by viewModel.transfers.collectAsStateWithLifecycle()
        LazyColumn(Modifier.fillMaxSize().padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            item {
                Text("Settings", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                Text("Connections, appearance, and your offline library.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            item { TabletLibraryCleanupCard(viewModel) }
            item { AppUpdateCard(viewModel) }
            item { ReceiveFromMacCard(viewModel) }
            item {
                var legacy by remember { mutableStateOf(false) }
                TextButton(onClick = { legacy = !legacy }) { Text(if (legacy) "Hide older Mac pairing" else "Other connection: tablet connects to Mac") }
                if (legacy) MacPairingCard(paired, discovered, viewModel::pairWithCode, viewModel::unpair)
            }
            if (paired) item {
                Text(status)
                Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                    Button(onClick = viewModel::syncNow) { Text("Check deliveries") }
                    OutlinedButton(onClick = viewModel::resumeDeliveries) { Text("Resume") }
                }
            }
            items(transfers, key = { it.id }) { transfer ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text("${transfer.status.replaceFirstChar(Char::uppercase)} · ${transfer.completedChapters}/${transfer.totalChapters} chapters")
                        LinearProgressIndicator(progress = { (transfer.receivedBytes.toFloat() / transfer.totalBytes.coerceAtLeast(1)).coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth())
                        val moving = transfer.status in setOf("receiving", "running", "downloading")
                        Text("${formatBytes(transfer.receivedBytes)}/${formatBytes(transfer.totalBytes)}" +
                            if (moving && transfer.bytesPerSecond > 0) " · ${formatBytes(transfer.bytesPerSecond)}/s" else "")
                        Text(if (moving && transfer.etaSeconds > 0) "About ${waitDuration(transfer.etaSeconds)} left"
                            else if (moving) "Time remaining: estimating…" else "Completed chapters stay in Reader.", style = MaterialTheme.typography.bodySmall)
                        if (transfer.error.isNotBlank()) Text(transfer.error, color = MaterialTheme.colorScheme.error)
                        if (transfer.status !in setOf("ready", "cancelled")) Row {
                            val action = if (transfer.status in setOf("paused", "failed", "waiting")) "resume" else "pause"
                            TextButton(onClick = { viewModel.controlTransfer(transfer.id, action) }) { Text(if (action == "resume") "Resume / Retry" else "Pause") }
                            TextButton(onClick = { viewModel.controlTransfer(transfer.id, "cancel") }) { Text("Cancel") }
                        }
                    }
                }
            }
            item {
                Row(verticalAlignment = Alignment.CenterVertically) { Text("Dark appearance", Modifier.weight(1f)); Switch(dark, setDark) }
                Text("Optional tools", style = MaterialTheme.typography.titleMedium)
                Text("LocalSend: open or share the received ZIP/CBZ with Panelyra. It imports directly. USB and manual files are optional.")
                OutlinedButton(onClick = chooseCbzFiles, modifier = Modifier.heightIn(min = 56.dp)) { Text("Manual ZIP / CBZ import") }
            }
        }
}

@Composable
private fun ReaderLibrary(viewModel: MainViewModel, chooseCbzFiles: () -> Unit) {
    var query by rememberSaveable { mutableStateOf("") }
    var unreadOnly by rememberSaveable { mutableStateOf(false) }
    val gridState = rememberLazyGridState()
    val tablet = tabletLayout()
    val series by viewModel.series.collectAsStateWithLifecycle()
    val chapters by viewModel.chapters.collectAsStateWithLifecycle()
    val selected by viewModel.selectedSeries.collectAsStateWithLifecycle()
    val continuing by viewModel.continueReading.collectAsStateWithLifecycle()
    val cleanup by viewModel.cleanupState.collectAsStateWithLifecycle()
    val appUpdate by viewModel.appUpdateState.collectAsStateWithLifecycle()
    val legacyStatus by viewModel.syncStatus.collectAsStateWithLifecycle()
    val receive by viewModel.receiveState.collectAsStateWithLifecycle()
    val legacyPaired by viewModel.paired.collectAsStateWithLifecycle()
    val useReceive = receive.paired || !legacyPaired
    val syncStatus = if (receive.running || receive.paired) receive.message else legacyStatus
    val connectionAction = { if (useReceive) viewModel.startReceiving() else viewModel.syncNow() }
    val connectionLabel = if (useReceive) { if (receive.running) "Receive mode on" else "Receive from Mac" } else "Sync now"
    if (selected != null) {
        BackHandler { viewModel.selectedSeries.value = null }
        val book = series.firstOrNull { it.id == selected }
        if (book != null) MacStyleBookDetails(book, chapters.filter { it.seriesId == selected }, tablet,
            { viewModel.selectedSeries.value = null }, viewModel::open)
        return
    }
    if (series.isEmpty()) {
        Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(horizontalAlignment = Alignment.CenterHorizontally) {
                Icon(Icons.AutoMirrored.Filled.MenuBook, null, Modifier.size(72.dp), tint = MaterialTheme.colorScheme.primary)
                Spacer(Modifier.height(12.dp))
                Text("Your offline library is empty", style = MaterialTheme.typography.titleLarge)
                Text("Select chapters on your Mac → Settings → Connections.\nThey appear here automatically, with covers.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(16.dp))
                Text("Pair once using Connections & transfers above.")
                Spacer(Modifier.height(12.dp))
                Text(syncStatus, color = MaterialTheme.colorScheme.onSurfaceVariant)
                TextButton(onClick = connectionAction, enabled = !useReceive || !receive.running) { Text(connectionLabel) }
            }
        }
        return
    }
    LazyVerticalGrid(
        columns = GridCells.Adaptive(if (tablet) 185.dp else 150.dp),
        state = gridState,
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(if (tablet) 24.dp else 16.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
        horizontalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item(span = { GridItemSpan(maxLineSpan) }) {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("Library", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                        Text("Your saved chapters, ready when you are.", style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    FilterChip(selected = unreadOnly, onClick = { unreadOnly = !unreadOnly }, label = { Text("Unread") })
                }
                OutlinedTextField(value = query, onValueChange = { query = it }, singleLine = true,
                    placeholder = { Text("Search by series title") }, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(8.dp))
                Text("${LibraryPresentation.books(series, query, unreadOnly).size} of ${series.size} series · Saved on this tablet", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
        }
        if (cleanup.ready && !cleanup.done) item(span = { GridItemSpan(maxLineSpan) }) { TabletLibraryCleanupCard(viewModel) }
        if (appUpdate.available) item(span = { GridItemSpan(maxLineSpan) }) { AppUpdateCard(viewModel) }
        continuing?.takeUnless { it.completed }?.let { chapter ->
            item(span = { GridItemSpan(maxLineSpan) }) {
                MacStyleContinue(series.firstOrNull { it.id == chapter.seriesId }?.title ?: chapter.title, chapter) { viewModel.open(chapter) }
            }
        }
        val filtered = LibraryPresentation.books(series, query, unreadOnly)
        if (filtered.isEmpty()) item(span = { GridItemSpan(maxLineSpan) }) {
            Text("No books match. Try another search or turn off Unread.", modifier = Modifier.padding(vertical = 24.dp), color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        gridItems(filtered, key = { it.id }) { item -> MacStyleSeriesCard(item) { viewModel.selectedSeries.value = item.id } }
    }
}

@Composable
private fun SeriesCard(series: SeriesSummary, open: () -> Unit) {
    var coverFailed by remember(series.coverPath) { mutableStateOf(false) }
    val coverFile = series.coverPath?.let { java.io.File(it) }
    Card(onClick = open, modifier = Modifier.fillMaxWidth().heightIn(min = 164.dp),
        shape = RoundedCornerShape(20.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)) {
        Column(Modifier.fillMaxWidth().padding(10.dp)) {
            if (coverFile?.isFile == true && !coverFailed) {
                AsyncImage(model = coverFile, contentDescription = "${series.title} cover",
                    modifier = Modifier.fillMaxWidth().height(250.dp).clip(RoundedCornerShape(14.dp)), contentScale = ContentScale.Crop,
                    onError = { coverFailed = true })
            } else Surface(Modifier.fillMaxWidth().height(250.dp), color = MaterialTheme.colorScheme.primary.copy(alpha = .16f)) {
                Box(Modifier.padding(16.dp), contentAlignment = Alignment.Center) { Text(series.title, style = MaterialTheme.typography.titleLarge, fontWeight = FontWeight.Bold, maxLines = 4, overflow = TextOverflow.Ellipsis) }
            }
            Spacer(Modifier.height(14.dp))
            Text(series.title, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold, maxLines = 2, overflow = TextOverflow.Ellipsis)
            Spacer(Modifier.height(6.dp))
            Text("${series.chapterCount} chapters", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
            if (series.unreadCount > 0) Text("${series.unreadCount} unread", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.primary)
        }
    }
}

@Composable
private fun ChapterRow(chapter: ChapterEntity, viewModel: MainViewModel) {
    Card(onClick = { viewModel.open(chapter) }, modifier = Modifier.fillMaxWidth().heightIn(min = 92.dp)) {
        Row(Modifier.fillMaxWidth().padding(16.dp), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text("Chapter ${chapter.chapter}", style = MaterialTheme.typography.titleMedium)
                Text("${chapter.pageCount} pages · ${formatBytes(chapter.byteSize)}")
            }
            Text(if (chapter.completed) "Read" else "Unread", color = if (chapter.completed) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.primary)
        }
    }
}

@Composable
private fun Downloader(viewModel: MainViewModel, scanPairingCode: () -> Unit) {
    val tablet = LocalConfiguration.current.screenWidthDp >= 1100
    val jobs by viewModel.jobs.collectAsStateWithLifecycle()
    val activity by viewModel.downloadActivity.collectAsStateWithLifecycle()
    val paired by viewModel.paired.collectAsStateWithLifecycle()
    val discoveredMac by viewModel.discoveredMac.collectAsStateWithLifecycle()
    var url by remember { mutableStateOf("") }
    var count by remember { mutableIntStateOf(1) }
    var performance by remember { mutableStateOf("reliable") }
    var authorized by remember { mutableStateOf(false) }
    var assisted by remember { mutableStateOf<DownloadJobEntity?>(null) }
    val padding = if (tablet) 24.dp else 16.dp
    if (tablet) {
        Row(
            Modifier.fillMaxSize().padding(padding),
            horizontalArrangement = Arrangement.spacedBy(18.dp),
        ) {
            LazyColumn(
                Modifier.weight(.43f).fillMaxHeight(),
                verticalArrangement = Arrangement.spacedBy(14.dp),
            ) {
                item {
                    Text("Downloads", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                    Text("Choose chapters. Follow their progress. Read offline.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Spacer(Modifier.height(16.dp))
                    DownloadForm(
                        url, { url = it }, count, { count = it }, performance, { performance = it },
                        authorized, { authorized = it },
                    ) { viewModel.createDownload(url, count, performance, authorized) }
                }
                item { Text("Mac fallback and deliveries are in Connections & transfers.", color = MaterialTheme.colorScheme.onSurfaceVariant) }
            }
            Column(Modifier.weight(.57f).fillMaxHeight()) {
                Text("Downloads", style = MaterialTheme.typography.headlineSmall)
                Spacer(Modifier.height(12.dp))
                if (jobs.isEmpty()) {
                    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) {
                        Text("No downloads yet", color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                } else LazyColumn(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                    items(jobs, key = { it.id }) { job ->
                        JobCard(job, activity[job.id], { viewModel.retry(job) }, { viewModel.cancel(job) }, { assisted = job })
                    }
                }
            }
        }
    } else {
        LazyColumn(Modifier.fillMaxSize().padding(padding), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            item {
                Text("Downloads", style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold)
                Text("Choose chapters. Follow their progress. Read offline.", color = MaterialTheme.colorScheme.onSurfaceVariant)
                Spacer(Modifier.height(16.dp))
                DownloadForm(
                    url, { url = it }, count, { count = it }, performance, { performance = it },
                    authorized, { authorized = it },
                ) { viewModel.createDownload(url, count, performance, authorized) }
            }
            item { Text("Mac fallback and deliveries are in Connections & transfers.", color = MaterialTheme.colorScheme.onSurfaceVariant) }
            if (jobs.isNotEmpty()) item { Text("Downloads", style = MaterialTheme.typography.titleLarge) }
            items(jobs, key = { it.id }) { job ->
                JobCard(job, activity[job.id], { viewModel.retry(job) }, { viewModel.cancel(job) }, { assisted = job })
            }
        }
    }
    assisted?.let { job ->
        AssistedBrowser(job, onDismiss = { assisted = null }) { urls, cookie ->
            viewModel.assistedCapture(job, urls, cookie); assisted = null
        }
    }
}

@Composable
private fun DownloadForm(
    url: String,
    setUrl: (String) -> Unit,
    count: Int,
    setCount: (Int) -> Unit,
    performance: String,
    setPerformance: (String) -> Unit,
    authorized: Boolean,
    setAuthorized: (Boolean) -> Unit,
    download: () -> Unit,
) {
    androidx.compose.material3.OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(20.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
            Text("New download", style = MaterialTheme.typography.titleLarge)
            Text("1 · Choose a chapter   2 · Download   3 · Read offline", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.primary)
            OutlinedTextField(
                url, setUrl, Modifier.fillMaxWidth().heightIn(min = 56.dp),
                label = { Text("Starting chapter URL") }, singleLine = true,
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                OutlinedTextField(count.toString(), { value -> value.toIntOrNull()?.takeIf { it in 1..10000 }?.let(setCount) },
                    Modifier.weight(1f), label = { Text("Chapters") }, singleLine = true,
                    keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Number))
                Spacer(Modifier.width(8.dp))
                OutlinedButton(
                    onClick = { if (count > 1) setCount(count - 1) },
                    modifier = Modifier.heightIn(min = 52.dp),
                ) { Text("−") }
                Spacer(Modifier.width(8.dp))
                OutlinedButton(
                    onClick = { if (count < 10000) setCount(count + 1) },
                    modifier = Modifier.heightIn(min = 52.dp),
                ) { Text("+") }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                RadioButton(performance == "reliable", { setPerformance("reliable") }); Text("Reliable")
                Spacer(Modifier.width(20.dp))
                RadioButton(performance == "rapid", { setPerformance("rapid") }); Text("Rapid")
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(authorized, setAuthorized)
                Text("I am authorized to archive this content", Modifier.weight(1f))
            }
            Button(onClick = download, enabled = authorized && url.trim().startsWith("https://"), modifier = Modifier.fillMaxWidth().heightIn(min = 56.dp)) {
                Icon(Icons.Default.Download, null); Spacer(Modifier.width(8.dp)); Text("Download")
            }
            Text("Each finished chapter goes straight to Reader. No second import.", style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun MacPairingCard(
    paired: Boolean,
    discoveredMac: String,
    pair: (String, String) -> Unit,
    unpair: () -> Unit,
) {
    var showPasteDialog by remember { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.fillMaxWidth().padding(18.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Computer, null, Modifier.size(32.dp))
                Spacer(Modifier.width(14.dp))
                Column(Modifier.weight(1f)) {
                    Text(if (paired) "Mac paired" else "Mac fallback not paired", style = MaterialTheme.typography.titleMedium)
                    Text(
                        if (paired) "Protected downloads can fall back automatically. Your local library remains available without the Mac."
                        else "Pair once on the same Wi-Fi. Then select books on Mac → Send to Huawei. Chapters and covers appear automatically.",
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            Spacer(Modifier.height(12.dp))
            if (paired) {
                TextButton(onClick = unpair, modifier = Modifier.heightIn(min = 52.dp)) { Text("Unpair") }
            } else {
                Button(onClick = { showPasteDialog = true }, modifier = Modifier.heightIn(min = 56.dp)) {
                    Text("Pair with short code")
                }
            }
        }
    }
    if (showPasteDialog) PairingCodeDialog(
        discoveredMac = discoveredMac,
        onDismiss = { showPasteDialog = false },
        onPair = { address, code -> showPasteDialog = false; pair(address, code) },
    )
}

@Composable
private fun PairingCodeDialog(discoveredMac: String, onDismiss: () -> Unit, onPair: (String, String) -> Unit) {
    var address by remember(discoveredMac) { mutableStateOf(discoveredMac) }
    var code by remember { mutableStateOf("") }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Pair with Mac") },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(12.dp)) {
                Text("Enter the address and 12-character code shown under Huawei tablet on your Mac.")
                OutlinedTextField(value = address, onValueChange = { address = it },
                    modifier = Modifier.fillMaxWidth(), label = { Text("Mac address, e.g. 192.168.1.188:8588") },
                    singleLine = true)
                OutlinedTextField(
                    value = code,
                    onValueChange = { code = it },
                    modifier = Modifier.fillMaxWidth(),
                    label = { Text("12-character code") },
                    singleLine = true,
                )
            }
        },
        confirmButton = {
            Button(onClick = { onPair(address.trim(), code.trim()) }, enabled = address.isNotBlank() && code.isNotBlank()) { Text("Pair") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun JobCard(job: DownloadJobEntity, activity: DownloadActivity?, retry: () -> Unit, cancel: () -> Unit, assisted: () -> Unit) {
    val active = job.status in setOf("queued", "running")
    val now by produceState(System.currentTimeMillis(), active, job.id) {
        while (active) { value = System.currentTimeMillis(); kotlinx.coroutines.delay(1000) }
    }
    val elapsed = activity?.elapsed(now) ?: ((now - job.createdAt) / 1000).coerceAtLeast(0)
    val stage = when (job.status) {
        "completed" -> "Ready in Reader"
        "queued" -> "Queued · starting shortly"
        "running" -> activity?.stage ?: "Preparing download"
        "needs_webview" -> "Browser access needed"
        "queued_for_mac" -> "Waiting for your Mac"
        "interrupted" -> "Interrupted · resume the unfinished chapters"
        "failed" -> "Couldn’t finish · saved chapters are kept"
        "cancelled" -> "Cancelled · saved chapters are kept"
        else -> job.status.replace('_', ' ')
    }
    androidx.compose.material3.OutlinedCard(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(18.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text(job.startUrl, maxLines = 2, style = MaterialTheme.typography.titleSmall)
            Text(stage, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
            Text("${job.currentChapter} / ${job.chapterCount} chapters saved · ${if (job.engine == "mac") "via Mac" else "on this tablet"}")
            LinearProgressIndicator(progress = { (job.currentChapter.toFloat() / job.chapterCount.coerceAtLeast(1)).coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth())
            if (active) {
                Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
                    Text("Receive", style = MaterialTheme.typography.labelSmall)
                    Text("Check", style = MaterialTheme.typography.labelSmall)
                    Text("Save to Reader", style = MaterialTheme.typography.labelSmall)
                }
                if (activity?.pages != null && activity.pages > 0) {
                    Text("${activity.page} / ${activity.pages} pages received · ${formatBytes(activity.bytes)}", style = MaterialTheme.typography.bodySmall)
                    LinearProgressIndicator(progress = { activity.pageFraction() ?: 0f }, modifier = Modifier.fillMaxWidth())
                } else LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                val eta = activity?.eta(job.status, job.currentChapter, job.chapterCount, now)
                Text("${waitDuration(elapsed)} elapsed · " + (eta?.let { "about ${waitDuration(it)} left" } ?: "time remaining: estimating…"), style = MaterialTheme.typography.bodySmall)
                Text(DOWNLOAD_NOTES[((elapsed / 8) % DOWNLOAD_NOTES.size).toInt()], style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            } else if (job.status == "completed") {
                Text("Open Reader to start. These chapters work offline.", style = MaterialTheme.typography.bodySmall)
            }
            if (job.error.isNotBlank()) Text(job.error, color = MaterialTheme.colorScheme.error)
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                if (job.status in setOf("failed", "queued_for_mac", "cancelled", "interrupted")) TextButton(onClick = retry) { Icon(Icons.Default.Refresh, null); Text("Retry / Resume") }
                if (job.status == "needs_webview") TextButton(onClick = assisted) { Text("Open browser") }
                if (job.status in setOf("queued", "running")) TextButton(onClick = cancel) { Text("Cancel") }
            }
        }
    }
}

@Composable
private fun ChapterLoadingCard(chapter: ChapterEntity) {
    val started = remember(chapter.cbzPath) { System.currentTimeMillis() }
    val seconds by produceState(0L, chapter.cbzPath) {
        while (true) { value = (System.currentTimeMillis() - started) / 1000; kotlinx.coroutines.delay(1000) }
    }
    Card(Modifier.padding(24.dp).fillMaxWidth()) {
        Column(Modifier.padding(24.dp), verticalArrangement = Arrangement.spacedBy(14.dp)) {
            Text("Opening chapter ${chapter.chapter}", style = MaterialTheme.typography.titleLarge)
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            Text("Preparing ${chapter.pageCount} pages · ${waitDuration(seconds)} elapsed")
            Text(if (seconds < 8) "Saved pages are reused when available. No download needed."
                else "Large chapters can take a moment to unpack the first time.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun AssistedBrowser(job: DownloadJobEntity, onDismiss: () -> Unit, capture: (List<String>, String?) -> Unit) {
    var webView by remember { mutableStateOf<WebView?>(null) }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text("Assisted browser capture") },
        text = {
            Column(Modifier.fillMaxWidth().fillMaxHeight(.8f)) {
                Text("Complete any login or verification, then capture the visible chapter.")
                AndroidView(
                    modifier = Modifier.fillMaxSize(),
                    factory = { context ->
                        WebView(context).apply {
                            settings.javaScriptEnabled = true
                            settings.domStorageEnabled = true
                            webViewClient = WebViewClient()
                            loadUrl(job.startUrl)
                            webView = this
                        }
                    },
                )
            }
        },
        confirmButton = {
            Button(onClick = {
                webView?.evaluateJavascript(WEBVIEW_IMAGE_SCRIPT) { raw ->
                    val decoded = JSONTokener(raw).nextValue() as? String ?: "[]"
                    val array = JSONArray(decoded)
                    val urls = (0 until array.length()).map(array::getString).distinct()
                    capture(urls, CookieManager.getInstance().getCookie(webView?.url ?: job.startUrl))
                }
            }) { Text("Capture chapter") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@OptIn(ExperimentalFoundationApi::class, ExperimentalMaterial3Api::class)
@Composable
private fun ImmersiveReader(chapter: ChapterEntity, pages: List<java.io.File>, viewModel: MainViewModel) {
    val context = LocalContext.current
    val chapterList by viewModel.readerChapters.collectAsStateWithLifecycle()
    val error by viewModel.readerError.collectAsStateWithLifecycle()
    val chapterIndex = chapterList.indexOfFirst { it.seriesId == chapter.seriesId && it.chapter == chapter.chapter }
    var mode by remember(chapter) { mutableStateOf(chapter.readingMode) }
    var position by remember(chapter) { mutableStateOf(chapter) }
    var fullscreen by remember { mutableStateOf(true) }
    var readerSettings by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }
    val activity = context as? Activity
    DisposableEffect(Unit) { onDispose {
        activity?.window?.let { WindowCompat.setDecorFitsSystemWindows(it, true); WindowCompat.getInsetsController(it, it.decorView).show(WindowInsetsCompat.Type.systemBars()) }
    } }
    fun setFullscreen(value: Boolean) {
        fullscreen = value
        activity?.window?.let { window ->
            WindowCompat.setDecorFitsSystemWindows(window, !value)
            val controller = WindowCompat.getInsetsController(window, window.decorView)
            if (value) controller.hide(WindowInsetsCompat.Type.systemBars()) else controller.show(WindowInsetsCompat.Type.systemBars())
        }
    }
    LaunchedEffect(Unit) { setFullscreen(true) }
    BackHandler {
        if (fullscreen) setFullscreen(false) else viewModel.closeReader()
    }
    Scaffold(
        topBar = {
            if (!fullscreen) TopAppBar(
                title = { Column { Text(chapter.title, maxLines = 1); Text("Chapter ${chapter.chapter}", style = MaterialTheme.typography.labelSmall) } },
                navigationIcon = { IconButton(onClick = viewModel::closeReader, modifier = Modifier.size(56.dp)) { Icon(Icons.AutoMirrored.Filled.ArrowBack, "Back") } },
                actions = {
                    IconButton(onClick = { context.startActivity(viewModel.share(chapter)) }, modifier = Modifier.size(56.dp)) { Icon(Icons.Default.Share, "Share CBZ") }
                    IconButton(onClick = { readerSettings = true }, modifier = Modifier.size(56.dp)) { Icon(Icons.Default.Settings, "Reader settings") }
                },
            )
        },
        bottomBar = {
            if (!fullscreen) Column(Modifier.fillMaxWidth().background(MaterialTheme.colorScheme.surface)) {
                Row(
                    Modifier.fillMaxWidth().padding(horizontal = 8.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    TextButton(onClick = { viewModel.navigateChapter(-1) }, enabled = chapterIndex > 0) { Text("← Previous") }
                    TextButton(onClick = viewModel::showChapterList) {
                        Text("Chapter ${chapter.chapter} · Chapters")
                    }
                    TextButton(onClick = { viewModel.navigateChapter(1) }, enabled = chapterIndex >= 0 && chapterIndex < chapterList.lastIndex) { Text("Next →") }
                }
            }
        },
    ) { padding ->
        if (error != null) Box(Modifier.padding(padding).fillMaxSize(), contentAlignment = Alignment.Center) {
            Column(Modifier.padding(24.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                Text(error!!, color = MaterialTheme.colorScheme.error)
                Button(onClick = { viewModel.open(chapter) }) { Text("Retry chapter") }
            }
        }
        else if (pages.isEmpty()) Box(Modifier.padding(padding).fillMaxSize(), contentAlignment = Alignment.Center) {
            ChapterLoadingCard(chapter)
        }
        else key(chapter.seriesId, chapter.chapter, mode) {
            ReaderPages(position, pages, mode, Modifier.padding(padding).background(androidx.compose.ui.graphics.Color.Black)
                .pointerInput(fullscreen) { detectTapGestures(onTap = { setFullscreen(!fullscreen) }) }, viewModel)
        }
    }
    if (readerSettings) ModalBottomSheet(onDismissRequest = { readerSettings = false }) {
        Column(Modifier.fillMaxWidth().padding(24.dp), verticalArrangement = Arrangement.spacedBy(16.dp)) {
            Text("Reading preferences", style = MaterialTheme.typography.headlineSmall)
            Text("Tap the page to show or hide controls. Swipe to read.", color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text("Reading mode", style = MaterialTheme.typography.titleMedium)
            LazyRow(
                    modifier = Modifier.fillMaxWidth(),
                    contentPadding = PaddingValues(horizontal = 12.dp, vertical = 8.dp),
                    horizontalArrangement = Arrangement.spacedBy(6.dp),
                ) {
                    items(listOf("fit", "original", "continuous", "single", "double")) { item ->
                        FilterChip(
                            selected = mode == item,
                            onClick = { position = viewModel.readerPosition(chapter); mode = item; viewModel.changeMode(chapter, item) },
                            modifier = Modifier.heightIn(min = 48.dp),
                            label = { Text(when (item) { "fit" -> "Fit width"; "original" -> "Original size"; "continuous" -> "Webtoon"; "single" -> "Single page"; else -> "Two pages" }) },
                        )
                    }
                    item {
                        Button(onClick = { viewModel.markComplete(chapter) }, modifier = Modifier.heightIn(min = 48.dp)) {
                            Text(if (chapterList.firstOrNull { it.chapter == chapter.chapter }?.completed == true) "Completed" else "Mark complete")
                        }
                    }
            }
            TextButton(onClick = { readerSettings = false; confirmDelete = true }) {
                Icon(Icons.Default.Delete, null); Spacer(Modifier.width(8.dp)); Text("Delete downloaded chapter", color = MaterialTheme.colorScheme.error)
            }
            Button(onClick = { readerSettings = false; setFullscreen(true) }, modifier = Modifier.fillMaxWidth().heightIn(min = 52.dp)) { Text("Back to reading") }
        }
    }
    if (confirmDelete) AlertDialog(
        onDismissRequest = { confirmDelete = false },
        title = { Text("Delete this chapter?") },
        text = { Text("The offline CBZ and cached pages will be removed from this tablet.") },
        confirmButton = { Button(onClick = { viewModel.delete(chapter); confirmDelete = false }) { Text("Delete") } },
        dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text("Keep") } },
    )
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun ReaderPages(chapter: ChapterEntity, pages: List<java.io.File>, mode: String, modifier: Modifier, viewModel: MainViewModel) {
    when (mode) {
        "single" -> {
            val state = rememberPagerState(initialPage = chapter.progressPanel.coerceIn(0, pages.lastIndex), pageCount = { pages.size })
            LaunchedEffect(state) { snapshotFlow { state.currentPage }.distinctUntilChanged().collect { viewModel.saveProgress(chapter, it, 0, mode) } }
            HorizontalPager(state, modifier.fillMaxSize()) { index -> PageImage(pages[index], Modifier.fillMaxSize(), ContentScale.Fit) }
        }
        "double" -> {
            val pairs = pages.chunked(2)
            val state = rememberPagerState(initialPage = (chapter.progressPanel / 2).coerceIn(0, pairs.lastIndex), pageCount = { pairs.size })
            LaunchedEffect(state) { snapshotFlow { state.currentPage }.distinctUntilChanged().collect { viewModel.saveProgress(chapter, it * 2, 0, mode) } }
            HorizontalPager(state, modifier.fillMaxSize()) { index ->
                Row(Modifier.fillMaxSize()) { pairs[index].forEach { PageImage(it, Modifier.weight(1f).fillMaxHeight(), ContentScale.Fit) } }
            }
        }
        else -> {
            val list = rememberLazyListState(chapter.progressPanel.coerceIn(0, pages.lastIndex), chapter.scrollOffset)
            LaunchedEffect(list, mode) {
                snapshotFlow { list.firstVisibleItemIndex to list.firstVisibleItemScrollOffset }
                    .distinctUntilChanged().collect { (panel, offset) -> viewModel.saveProgress(chapter, panel, offset, mode) }
            }
            LazyColumn(modifier.fillMaxSize(), state = list, horizontalAlignment = Alignment.CenterHorizontally) {
                items(pages.size) { index ->
                    val pageModifier = when (mode) {
                        "original" -> Modifier
                        "continuous" -> Modifier.fillMaxWidth()
                        else -> Modifier.fillMaxWidth()
                    }
                    PageImage(pages[index], pageModifier, if (mode == "original") ContentScale.None else ContentScale.FillWidth)
                }
            }
        }
    }
}

@Composable
private fun PageImage(file: java.io.File, modifier: Modifier, scale: ContentScale) {
    AsyncImage(model = file, contentDescription = file.name, modifier = modifier, contentScale = scale)
}

private fun numberText(value: Double) = if (value % 1.0 == 0.0) value.toInt().toString() else value.toString()
private fun formatBytes(bytes: Long): String = when {
    bytes >= 1_073_741_824 -> "%.1f GB".format(bytes / 1_073_741_824.0)
    bytes >= 1_048_576 -> "%.1f MB".format(bytes / 1_048_576.0)
    else -> "%.0f KB".format(bytes / 1024.0)
}

private const val WEBVIEW_IMAGE_SCRIPT = """
(() => JSON.stringify([...document.querySelectorAll('.reading-content img,#readerarea img,#chapter-content img,.chapter-content img,.entry-content img,.page-break img,img')]
 .map(x=>x.dataset.src||x.dataset.lazySrc||x.dataset.original||x.currentSrc||x.src)
 .filter((x,i,a)=>x&&/^https?:/.test(x)&&a.indexOf(x)===i&&!/(logo|avatar|discord|telegram|banner|advert|promo|placeholder|loading|spacer)/i.test(x))))()
"""


@Composable
private fun ReceiveFromMacCard(viewModel: MainViewModel) {
    val receive by viewModel.receiveState.collectAsStateWithLifecycle()
    var confirmForget by remember { mutableStateOf(false) }
    Card(Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
            Text("Receive from Mac", style = MaterialTheme.typography.titleLarge)
            Text("Send over Wi-Fi without changing your Mac firewall. Your pairing stays saved until you forget it.")
            Text(receive.message)
            if (receive.running) {
                androidx.compose.foundation.text.selection.SelectionContainer { Text(receive.address, style = MaterialTheme.typography.titleMedium) }
                if (receive.code.isNotBlank()) {
                    androidx.compose.foundation.text.selection.SelectionContainer { Text(receive.code.chunked(4).joinToString(" "), style = MaterialTheme.typography.headlineSmall) }
                    Text("On the Mac: Settings → Connections → Connect to tablet. An unused code lasts five minutes; an established pairing does not expire.")
                }
                Text("Receive mode stays available in the background. Stop it here or from its notification.")
                OutlinedButton(onClick = viewModel::stopReceiving) { Text("Stop receiving") }
            } else Button(onClick = viewModel::startReceiving, enabled = receive.message != "Starting Receive mode…") { Text("Receive from Mac") }
            if (receive.paired) TextButton(onClick = { confirmForget = true }) { Text("Forget Mac") }
        }
    }
    if (confirmForget) AlertDialog(onDismissRequest = { confirmForget = false },
        title = { Text("Forget this Mac?") }, text = { Text("Saved chapters stay on the tablet. You will need a new code to connect again.") },
        confirmButton = { TextButton(onClick = { viewModel.forgetSendingMac(); confirmForget = false }) { Text("Forget Mac") } },
        dismissButton = { TextButton(onClick = { confirmForget = false }) { Text("Cancel") } })
}
