package com.panelyra.tablet.ui

import android.app.Application
import android.content.Intent
import android.net.Uri
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.provider.OpenableColumns
import android.webkit.CookieManager
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import androidx.work.ExistingWorkPolicy
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.workDataOf
import com.panelyra.tablet.data.ChapterEntity
import com.panelyra.tablet.data.DownloadJobEntity
import com.panelyra.tablet.data.DownloadCheckpoint
import com.panelyra.tablet.data.LibraryRepository
import com.panelyra.tablet.data.PanelyraDatabase
import com.panelyra.tablet.download.DirectCaptureEngine
import com.panelyra.tablet.download.DownloadWorker
import com.panelyra.tablet.download.LibrarySyncWorker
import com.panelyra.tablet.download.DeliveryScheduler
import com.panelyra.tablet.download.DeliveryPollWorker
import com.panelyra.tablet.data.ChapterIdentity
import kotlinx.coroutines.channels.Channel
import org.json.JSONObject
import com.panelyra.tablet.network.CompanionClient
import com.panelyra.tablet.network.MacDiscovery
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.collect
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.UUID
import java.io.File
import java.util.concurrent.TimeUnit
import java.util.zip.ZipInputStream
import android.graphics.BitmapFactory
import kotlinx.coroutines.delay
import kotlinx.coroutines.Job
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.ensureActive

@OptIn(ExperimentalCoroutinesApi::class)
class MainViewModel(application: Application) : AndroidViewModel(application) {
    private val dao = PanelyraDatabase.get(application).dao()
    private val library = LibraryRepository(application, dao)
    private val companion = CompanionClient(application)
    private val work = WorkManager.getInstance(application)
    private val cleanup = com.panelyra.tablet.data.LibraryCleanup(application, library)
    private val updater = com.panelyra.tablet.update.AppUpdates(application)
    val cleanupState = MutableStateFlow(com.panelyra.tablet.data.CleanupState())
    val appUpdateState = MutableStateFlow(updater.state())

    fun finishLibraryCleanup() {
        if (cleanupState.value.busy) return
        cleanupState.value = cleanupState.value.copy(busy = true, message = "Checking replacement files before cleanup…")
        viewModelScope.launch(Dispatchers.IO) {
            runCatching { cleanup.execute() }.onSuccess { cleanupState.value = it; message.value = "Tablet library cleaned. Mac books were not changed." }
                .onFailure { cleanupState.value = cleanupState.value.copy(busy = false, message = it.message ?: "Cleanup could not finish. Old files were retained.") }
        }
    }
    fun undoLibraryCleanup() = viewModelScope.launch(Dispatchers.IO) {
        runCatching { cleanup.undo() }.onSuccess { cleanupState.value = cleanup.inspect(); message.value = "Previous books restored from Trash." }
            .onFailure { message.value = "Could not restore Trash: ${it.message}" }
    }
    fun setAutomaticAppUpdates(enabled: Boolean) { updater.setEnabled(enabled); appUpdateState.value = updater.state() }
    fun installAppUpdate() = viewModelScope.launch(Dispatchers.IO) {
        runCatching { updater.installIntent() }.onSuccess { intent -> withContext(Dispatchers.Main) { getApplication<Application>().startActivity(intent) } }
            .onFailure { message.value = it.message ?: "Could not open the Android installer." }
    }
    val series = dao.observeSeries().stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())
    val jobs = dao.observeJobs().stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())
    val downloadActivity = work.getWorkInfosByTagFlow("panelyra-capture").map { infos ->
        infos.filter { !it.state.isFinished }.mapNotNull { info ->
            info.progress.getString(DownloadWorker.KEY_JOB_ID)?.let { id ->
                id to com.panelyra.tablet.download.DownloadActivity.from(info.progress)
            }
        }.toMap()
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyMap())
    val continueReading = dao.observeContinueReading().stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), null)
    val selectedSeries = MutableStateFlow<String?>(null)
    val chapters = selectedSeries.flatMapLatest { id -> if (id == null) flowOf(emptyList()) else dao.observeChapters(id) }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())
    val openedChapter = MutableStateFlow<ChapterEntity?>(null)
    val readerPages = MutableStateFlow<List<java.io.File>>(emptyList())
    val readerChapters = MutableStateFlow<List<ChapterEntity>>(emptyList())
    val readerError = MutableStateFlow<String?>(null)
    val transfers = dao.observeTransfers().stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), emptyList())
    private data class Position(val chapter: ChapterEntity, val panel: Int, val offset: Int, val mode: String)
    private val positions = Channel<Position>(Channel.UNLIMITED)
    private val latestPosition = mutableMapOf<String, Position>()
    private var readerLoadJob: Job? = null
    val message = MutableStateFlow<String?>(null)
    val receiveState = com.panelyra.tablet.receive.TabletReceiveService.state
    fun startReceiving() {
        runCatching { com.panelyra.tablet.receive.TabletReceiveService.start(getApplication()) }
            .onFailure { message.value = "Could not start Receive mode: ${it.message}" }
    }
    fun stopReceiving() { com.panelyra.tablet.receive.TabletReceiveService.stop(getApplication()) }
    fun forgetSendingMac() { com.panelyra.tablet.receive.TabletReceiveService.stop(getApplication(), true) }
    val paired = MutableStateFlow(companion.isPaired)
    val discoveredMac = MutableStateFlow("")
    private val discovery = MacDiscovery(application) { address ->
        discoveredMac.value = address
        viewModelScope.launch(Dispatchers.IO) { if (companion.reconnect(address)) syncNow() }
    }
    val syncStatus = MutableStateFlow("Checking local library…")
    @Volatile private var appVisible = false
    private val connectivity = application.getSystemService(Application.CONNECTIVITY_SERVICE) as ConnectivityManager
    private val wifiCallback = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) {
            viewModelScope.launch(Dispatchers.Main) {
                if (appVisible) { discovery.stop(); discovery.start(); syncNow() }
            }
        }
    }
    private var wifiCallbackRegistered = false

    init {
        wifiCallbackRegistered = runCatching {
            connectivity.registerNetworkCallback(NetworkRequest.Builder()
                .addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build(), wifiCallback)
            true
        }.getOrDefault(false)
        viewModelScope.launch(Dispatchers.IO) {
            val errors = library.recoverOrphans()
            library.repairMissingCovers()
            if (errors.isNotEmpty()) message.value = "Damaged chapters: ${errors.joinToString("; ")}"
            dao.observeSeries().collect {
                if (!cleanupState.value.busy) cleanupState.value = runCatching { cleanup.inspect() }.getOrElse {
                    com.panelyra.tablet.data.CleanupState(message = "Cleanup is not available: ${it.message}")
                }
            }
        }
        viewModelScope.launch(Dispatchers.IO) {
            for (position in positions) dao.savePosition(position.chapter.seriesId, position.chapter.chapter,
                position.panel, position.offset, position.mode)
        }
        work.cancelUniqueWork("library-sync-now")
        work.cancelUniqueWork("library-sync-periodic")
        work.enqueueUniquePeriodicWork(
            "delivery-poll-periodic", androidx.work.ExistingPeriodicWorkPolicy.KEEP,
            PeriodicWorkRequestBuilder<DeliveryPollWorker>(15, TimeUnit.MINUTES).build(),
        )
        syncNow()
        viewModelScope.launch {
            val prefs = application.getSharedPreferences("panelyra_sync", Application.MODE_PRIVATE)
            while (true) {
                syncStatus.value = prefs.getString("status", "Ready to sync").orEmpty()
                appUpdateState.value = updater.state()
                delay(1_500)
            }
        }
        viewModelScope.launch(Dispatchers.IO) {
            val prefs = application.getSharedPreferences("panelyra_sync", Application.MODE_PRIVATE)
            while (true) {
                if (appVisible && companion.isPaired) {
                    runCatching { companion.heartbeat(); companion.pendingTransfers() }.onSuccess { pending ->
                        if (pending.any { it.optString("status") !in setOf("paused", "cancelled", "failed") }) syncNow()
                    }.onFailure {
                        // WorkManager owns the reconnect attempt and coordinated delivery queue.
                        // Never clear the saved token/pin merely because the Mac is asleep.
                        syncNow()
                    }
                }
                delay(15_000)
            }
        }
    }

    override fun onCleared() {
        discovery.stop()
        if (wifiCallbackRegistered) runCatching { connectivity.unregisterNetworkCallback(wifiCallback) }
        super.onCleared()
    }

    fun syncNow() {
        DeliveryScheduler.request(getApplication())
    }

    fun controlTransfer(id: String, action: String) = viewModelScope.launch(Dispatchers.IO) {
        val status = when (action) { "pause" -> "paused"; "cancel" -> "cancelled"; else -> "queued" }
        dao.transferStatus(id, status)
        if (action == "resume") getApplication<Application>().getSharedPreferences("panelyra_sync", Application.MODE_PRIVATE)
            .edit().putBoolean("paused", false).apply()
        runCatching { companion.transferAction(id, action) }.onFailure {
            message.value = "Saved locally. Mac will reconnect when available: ${it.message}"
        }
        if (action == "resume") syncNow()
    }

    fun resumeDeliveries() {
        getApplication<Application>().sendBroadcast(Intent(getApplication(), com.panelyra.tablet.download.DeliveryControlReceiver::class.java)
            .setAction("com.panelyra.tablet.RESUME"))
    }

    fun appResumed() {
        appVisible = true; discovery.start(); syncNow()
        if (getApplication<Application>().getSharedPreferences("panelyra_receive", Application.MODE_PRIVATE).getBoolean("enabled", false)) startReceiving()
    }
    fun appPaused() { appVisible = false; discovery.stop() }

    fun createDownload(url: String, count: Int, performance: String, authorized: Boolean) {
        if (!authorized) { message.value = "Confirm that you are authorized to archive this content."; return }
        if (!url.startsWith("https://")) { message.value = "Enter a secure HTTPS chapter URL."; return }
        if (count !in 1..10000) { message.value = "Choose between 1 and 10,000 chapters."; return }
        val id = UUID.randomUUID().toString()
        message.value = "Added to Downloads. Progress will appear below."
        viewModelScope.launch {
            dao.putJob(DownloadJobEntity(id, url.trim(), count, performance, "queued"))
            enqueue(id)
        }
    }

    fun retry(job: DownloadJobEntity) = viewModelScope.launch {
        dao.putJob(job.copy(status = "queued", error = "", updatedAt = System.currentTimeMillis()))
        enqueue(job.id)
    }

    fun cancel(job: DownloadJobEntity) {
        work.cancelUniqueWork("capture-${job.id}")
        viewModelScope.launch(Dispatchers.IO) {
            dao.updateJob(job.id, "cancelled", job.engine, job.currentChapter, job.remoteJobId, "")
            job.remoteJobId?.let { runCatching { companion.cancel(it) } }
        }
    }

    private fun enqueue(id: String) {
        val request = OneTimeWorkRequestBuilder<DownloadWorker>()
            .setInputData(workDataOf(DownloadWorker.KEY_JOB_ID to id))
            .addTag("panelyra-capture")
            .build()
        work.enqueueUniqueWork("capture-$id", ExistingWorkPolicy.REPLACE, request)
    }

    fun pair(raw: String) = viewModelScope.launch(Dispatchers.IO) {
        runCatching { companion.pair(raw) }.onSuccess {
            paired.value = true
            message.value = "Mac paired securely."
            syncNow()
        }.onFailure { message.value = it.message ?: "Pairing failed." }
    }

    fun pairWithCode(address: String, code: String) = viewModelScope.launch(Dispatchers.IO) {
        runCatching { companion.pairWithCode(address, code) }.onSuccess {
            paired.value = true
            message.value = "Mac paired securely. Select books on your Mac and tap Send to Huawei."
            syncNow()
        }.onFailure { message.value = it.message ?: "Pairing failed." }
    }

    fun unpair() = viewModelScope.launch(Dispatchers.IO) {
        companion.unpair(); paired.value = false; message.value = "Mac unpaired."
    }

    fun importCbzFiles(uris: List<Uri>) = viewModelScope.launch(Dispatchers.IO) {
        val resolver = getApplication<Application>().contentResolver
        var imported = 0
        var skipped = 0
        var covers = 0
        var firstError: String? = null
        message.value = "Importing chapters…"
        for (uri in uris) {
            val result = runCatching {
                val filename = resolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
                    if (cursor.moveToFirst()) cursor.getString(0) else null
                } ?: uri.lastPathSegment.orEmpty()
                val isBundle = filename.endsWith(".zip", ignoreCase = true) && resolver.openInputStream(uri)?.use { stream ->
                    ZipInputStream(stream).use { zip ->
                        val first = zip.nextEntry?.name.orEmpty()
                        first.endsWith(".cbz", true) || first.startsWith("Covers/") || first == "HOW_TO_IMPORT.txt"
                    }
                } == true
                if (isBundle) {
                    val counts = importLibraryZip(uri)
                    imported += counts.first
                    skipped += counts.second
                    covers += counts.third
                    return@runCatching
                }
                require(filename.endsWith(".cbz", true) || filename.endsWith(".zip", true)) { "Share a ZIP or CBZ with Panelyra" }
                val match = Regex("^(.+?)\\s+-\\s+Chapter\\s+(\\d+(?:\\.\\d+)?)\\.(?:cbz|zip)$", RegexOption.IGNORE_CASE).matchEntire(filename)
                val seriesTitle = match?.groupValues?.get(1)?.trim() ?: filename.substringBeforeLast('.').ifBlank { "Imported book" }
                val chapter = match?.groupValues?.get(2) ?: "1"
                val key = if (match != null) LibraryRepository.slug(seriesTitle) else "external-" +
                    java.security.MessageDigest.getInstance("SHA-256").digest(uri.toString().toByteArray()).take(8).joinToString("") { "%02x".format(it) }
                val existing = dao.chapter(key, ChapterIdentity.chapter(chapter))
                if (existing != null && library.verified(key, chapter, existing.sha256, existing.byteSize)) {
                    skipped++
                } else {
                    val temporary = File.createTempFile("panelyra-import-", ".cbz", getApplication<Application>().cacheDir)
                    try {
                        resolver.openInputStream(uri)?.use { input ->
                            temporary.outputStream().use { output ->
                                val buffer = ByteArray(256 * 1024)
                                var copied = 0L
                                while (true) {
                                    kotlinx.coroutines.currentCoroutineContext().ensureActive()
                                    val count = input.read(buffer)
                                    if (count < 0) break
                                    copied += count
                                    require(copied <= 1_000_000_000L) { "Chapter exceeds the 1 GB import limit" }
                                    check(temporary.parentFile!!.usableSpace > count + 10_000_000L) { "Not enough tablet storage" }
                                    output.write(buffer, 0, count)
                                }
                            }
                        } ?: error("Could not open $filename.")
                        library.installCbz(temporary, seriesTitle, chapter, "Chapter $chapter", "", seriesKey = key)
                        imported++
                    } finally {
                        temporary.delete()
                    }
                }
            }
            result.exceptionOrNull()?.let { if (firstError == null) firstError = it.message ?: "Invalid CBZ." }
        }
        message.value = when {
            firstError != null -> "Imported $imported chapters and $covers covers, skipped $skipped. ${firstError}"
            else -> "Imported $imported chapters and $covers covers; skipped $skipped already in your library."
        }
    }

    private suspend fun importLibraryZip(uri: Uri): Triple<Int, Int, Int> {
        val app = getApplication<Application>()
        val chapterName = Regex("^(.+?)\\s+-\\s+Chapter\\s+(\\d+(?:\\.\\d+)?)\\.cbz$", RegexOption.IGNORE_CASE)
        val coverName = Regex("^Covers/([^/]+)\\.(jpg|jpeg|png|webp)$", RegexOption.IGNORE_CASE)
        var imported = 0
        var skipped = 0
        var covers = 0
        var totalBytes = 0L
        var entries = 0
        val pendingCovers = mutableListOf<Triple<String, String, File>>()
        try {
            val input = app.contentResolver.openInputStream(uri) ?: error("Cannot open the library ZIP.")
            ZipInputStream(input.buffered()).use { zip ->
                while (true) {
                    val entry = zip.nextEntry ?: break
                    entries++
                    require(entries <= 2_000) { "Too many files in the library ZIP." }
                    val name = entry.name.replace('\\', '/')
                    require(!name.startsWith('/') && name.split('/').none { it == ".." }) { "Unsafe ZIP filename." }
                    if (entry.isDirectory || name == "HOW_TO_IMPORT.txt") {
                        zip.closeEntry()
                        continue
                    }
                    val match = chapterName.matchEntire(name)
                    val coverMatch = coverName.matchEntire(name)
                    require(match != null || coverMatch != null) { "Unexpected file in library ZIP: $name" }
                    val temporary = File.createTempFile("panelyra-bundle-", if (match != null) ".cbz" else ".cover", app.cacheDir)
                    try {
                        temporary.outputStream().use { output ->
                            val buffer = ByteArray(256 * 1024)
                            var entryBytes = 0L
                            while (true) {
                                val read = zip.read(buffer)
                                if (read < 0) break
                                entryBytes += read
                                totalBytes += read
                                require(entryBytes <= 750_000_000L && totalBytes <= 5_000_000_000L) { "Library ZIP is too large." }
                                output.write(buffer, 0, read)
                            }
                        }
                        if (match != null) {
                            val title = match.groupValues[1].trim()
                            val chapter = match.groupValues[2]
                            if (dao.chapter(LibraryRepository.slug(title), ChapterIdentity.chapter(chapter)) != null) {
                                skipped++
                            } else {
                                library.installCbz(temporary, title, chapter, "Chapter $chapter", "")
                                imported++
                            }
                        } else if (coverMatch != null) {
                            val title = coverMatch.groupValues[1]
                            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                            BitmapFactory.decodeFile(temporary.absolutePath, bounds)
                            require(bounds.outWidth in 1..10_000 && bounds.outHeight in 1..10_000) { "Invalid cover: $name" }
                            pendingCovers += Triple(title, coverMatch.groupValues[2].lowercase(), temporary)
                            zip.closeEntry()
                            continue
                        }
                    } finally {
                        if (coverMatch == null) temporary.delete()
                    }
                    zip.closeEntry()
                }
            }
            require(imported + skipped > 0) { "The library ZIP contains no chapters." }
            for ((title, extension, temporary) in pendingCovers) {
                val seriesId = LibraryRepository.slug(title)
                if (dao.series(seriesId) != null) {
                    library.installCover(seriesId, temporary, "")
                    covers++
                }
            }
        } finally {
            pendingCovers.forEach { it.third.delete() }
        }
        return Triple(imported, skipped, covers)
    }

    fun open(chapter: ChapterEntity) {
        readerLoadJob?.cancel()
        readerPages.value = emptyList()
        readerError.value = null
        readerChapters.value = emptyList()
        openedChapter.value = chapter
        readerLoadJob = viewModelScope.launch {
            val fresh = dao.chapter(chapter.seriesId, chapter.chapter) ?: chapter
            openedChapter.value = readerPosition(fresh)
            readerChapters.value = dao.chaptersForSeries(chapter.seriesId)
            try { readerPages.value = library.pages(fresh) }
            catch (error: CancellationException) { throw error }
            catch (error: Exception) { readerError.value = error.message ?: "Could not open this chapter." }
            dao.observeChapters(chapter.seriesId).collect { readerChapters.value = it }
        }
    }

    fun navigateChapter(direction: Int) {
        val current = openedChapter.value ?: return
        val list = readerChapters.value
        val index = list.indexOfFirst { it.seriesId == current.seriesId && it.chapter == current.chapter }
        if (index < 0) return
        val next = list.getOrNull(index + direction) ?: return
        open(next)
    }

    fun showChapterList() {
        val current = openedChapter.value ?: return
        selectedSeries.value = current.seriesId
        closeReader()
    }

    fun closeReader() {
        readerLoadJob?.cancel()
        readerLoadJob = null
        openedChapter.value = null
        readerPages.value = emptyList()
        readerChapters.value = emptyList()
    }

    fun saveProgress(chapter: ChapterEntity, panel: Int, offset: Int, mode: String) {
        val position = Position(chapter, panel.coerceAtLeast(0), offset.coerceAtLeast(0), mode)
        latestPosition["${chapter.seriesId}/${chapter.chapter}"] = position
        positions.trySend(position)
    }

    fun changeMode(chapter: ChapterEntity, mode: String) {
        val position = latestPosition["${chapter.seriesId}/${chapter.chapter}"]
        saveProgress(chapter, position?.panel ?: chapter.progressPanel, position?.offset ?: chapter.scrollOffset, mode)
    }

    fun readerPosition(chapter: ChapterEntity): ChapterEntity = latestPosition["${chapter.seriesId}/${chapter.chapter}"]?.let {
        chapter.copy(progressPanel = it.panel, scrollOffset = it.offset, readingMode = it.mode)
    } ?: chapter

    fun markComplete(chapter: ChapterEntity) = viewModelScope.launch { dao.setCompleted(chapter.seriesId, chapter.chapter, true) }

    fun delete(chapter: ChapterEntity) = viewModelScope.launch {
        library.delete(chapter); closeReader(); syncNow(); message.value = "Chapter deleted."
    }

    fun share(chapter: ChapterEntity): Intent = Intent.createChooser(library.shareIntent(chapter), "Share chapter CBZ")

    fun assistedCapture(job: DownloadJobEntity, imageUrls: List<String>, cookie: String?) {
        viewModelScope.launch {
            val saved = dao.job(job.id) ?: job
            if (saved.status == "cancelled" || saved.currentChapter >= saved.chapterCount) return@launch
            runCatching {
                val startUrl = DownloadCheckpoint.resumeUrl(saved)
                dao.putJob(DownloadCheckpoint.beginAssisted(saved))
                DirectCaptureEngine(getApplication()).capture(
                    startUrl, saved.chapterCount - saved.currentChapter, saved.performance, cookie, imageUrls,
                    onChapter = { chapter, next ->
                        library.installCbz(chapter.cbz, chapter.seriesTitle, chapter.chapterLabel, chapter.title, chapter.sourceUrl)
                        val current = dao.job(job.id) ?: job
                        dao.putJob(current.copy(currentChapter = current.currentChapter + 1, resumeUrl = next.orEmpty()))
                        chapter.cbz.parentFile?.deleteRecursively()
                    },
                )
            }.onSuccess {
                val latest = dao.job(job.id) ?: saved
                if (latest.status != "cancelled") dao.updateJob(job.id, "completed", "webview", latest.currentChapter, null, "")
            }.onFailure { error ->
                if (error is CancellationException) throw error
                val latest = dao.job(job.id) ?: saved
                if (latest.status != "cancelled") dao.updateJob(job.id, "failed", "webview", latest.currentChapter, null, error.message ?: "WebView capture failed.")
            }
        }
    }
}
