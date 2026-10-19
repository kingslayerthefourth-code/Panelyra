package com.panelyra.tablet.download

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.work.CoroutineWorker
import androidx.work.ForegroundInfo
import androidx.work.WorkerParameters
import com.panelyra.tablet.MainActivity
import com.panelyra.tablet.R
import com.panelyra.tablet.data.*
import com.panelyra.tablet.network.CompanionClient
import com.panelyra.tablet.network.MacDiscovery
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

private class DeliveryPaused : Exception()
private class DeliveryCancelled : Exception()

class LibrarySyncWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    private val dao = PanelyraDatabase.get(context).dao()
    private val library = LibraryRepository(context, dao)
    private val client = CompanionClient(context)
    private val prefs = context.getSharedPreferences("panelyra_sync", Context.MODE_PRIVATE)
    private var activeTransfer: String? = null

    private fun status(text: String) { prefs.edit().putString("status", text).apply() }

    override suspend fun doWork(): Result = withContext(Dispatchers.IO) {
        try {
            importUsb()
            if (!client.isPaired) { status("Offline library ready · Pair your Mac for delivery"); return@withContext Result.success() }
            val network = applicationContext.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
            if (network.getNetworkCapabilities(network.activeNetwork)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) != true) {
                status("Waiting for Wi-Fi · Downloaded books remain available"); return@withContext Result.success()
            }
            try { client.heartbeat() } catch (error: Exception) {
                val found = CompletableDeferred<Boolean>()
                val discovery = MacDiscovery(applicationContext) { address ->
                    CoroutineScope(Dispatchers.IO).launch { if (client.reconnect(address)) found.complete(true) }
                }
                try { discovery.start(); withTimeoutOrNull(6_000) { found.await() } ?: throw error }
                finally { discovery.stop() }
            }
            publishInventory()
            if (prefs.getBoolean("paused", false)) { status("Delivery paused · Resume in Connections & transfers"); return@withContext Result.success() }
            var received = 0
            while (true) {
                val pending = client.pendingTransfers()
                for (manifest in pending) {
                    val id = manifest.getString("transfer_id")
                    val old = dao.transfer(id)
                    if (old?.status in setOf("cancelled", "failed") && manifest.optString("status") != "queued") continue
                    val chapters = manifest.getJSONArray("chapters")
                    val transfer = (old ?: TransferEntity(id, manifest.toString(), totalChapters = chapters.length(), totalBytes = manifest.getLong("total_bytes")))
                        .copy(manifest = manifest.toString(), status = manifest.getString("status"))
                    dao.putTransfer(transfer)
                }
                val next = pending.firstOrNull { item ->
                    item.optString("status") !in setOf("paused", "cancelled", "ready", "failed") &&
                        dao.transfer(item.getString("transfer_id"))?.status !in setOf("cancelled", "failed")
                } ?: break
                activeTransfer = next.getString("transfer_id")
                try { receive(next); received += next.getJSONArray("chapters").length() }
                catch (_: DeliveryPaused) { status("Delivery paused · Finished chapters are ready"); break }
                catch (_: DeliveryCancelled) {
                    dao.transferStatus(activeTransfer!!, "cancelled")
                    status("Delivery cancelled · Finished chapters are kept")
                }
                activeTransfer = null
            }
            publishInventory()
            if (!prefs.getBoolean("paused", false)) status(if (received > 0) "Ready · $received selected chapters processed" else "Ready for selected Mac deliveries · Offline library available")
            Result.success()
        } catch (error: CancellationException) {
            withContext(NonCancellable + Dispatchers.IO) {
                activeTransfer?.let { id ->
                    val paused = prefs.getBoolean("paused", false)
                    dao.transferStatus(id, if (paused) "paused" else "waiting")
                    if (paused) runCatching { client.transferAction(id, "pause") }
                }
                status(if (prefs.getBoolean("paused", false)) "Delivery paused · Tap Resume" else "Delivery interrupted · Reopen to resume")
            }
            throw error
        } catch (error: Exception) {
            activeTransfer?.let { id ->
                val terminal = error is IllegalArgumentException || error.message?.contains("storage", true) == true
                dao.transfer(id)?.let { dao.putTransfer(it.copy(status = if (terminal) "failed" else "waiting", error = error.message.orEmpty())) }
                runCatching { client.transferAction(id, "progress", JSONObject().put("status", if (terminal) "failed" else "waiting").put("error", error.message)) }
            }
            status("Delivery waiting: ${error.message ?: "Reconnect to your Mac"}. Offline books stay available.")
            Result.retry()
        }
    }

    private suspend fun receive(manifest: JSONObject) = coroutineScope {
        val id = manifest.getString("transfer_id")
        val array = manifest.getJSONArray("chapters")
        val chapters = (0 until array.length()).map { client.parseChapter(array.getJSONObject(it)) }
        val staging = File(applicationContext.filesDir, "delivery-staging/$id").apply { mkdirs() }
        val mutex = Mutex()
        val bytes = mutableMapOf<String, Long>()
        var done = 0
        var lastUpdate = 0L
        var remoteCheck = 0L
        var remoteReport = 0L
        val started = System.nanoTime()
        var networkBytes = 0L
        val covers = mutableSetOf<String>()
        for (chapter in chapters) {
            val name = "${chapter.seriesKey}-${chapter.chapter}-${chapter.sha256}.cbz"
            val partial = File(staging, "$name.partial")
            bytes["${chapter.seriesKey}/${chapter.chapter}"] = partial.length().coerceAtMost(chapter.size)
        }
        suspend fun update(stage: String, force: Boolean = false) = mutex.withLock {
            coroutineContext.ensureActive()
            if (prefs.getBoolean("paused", false)) throw DeliveryPaused()
            val current = dao.transfer(id) ?: error("Delivery manifest missing")
            if (current.status == "cancelled") throw DeliveryCancelled()
            if (current.status == "paused") throw DeliveryPaused()
            val now = System.currentTimeMillis()
            if (now - remoteCheck > 3_000) {
                val remote = client.transfer(id).getString("status")
                remoteCheck = now
                if (remote == "cancelled") throw DeliveryCancelled()
                if (remote == "paused") { dao.transferStatus(id, "paused"); throw DeliveryPaused() }
            }
            if (force || now - lastUpdate > 750) {
                lastUpdate = now
                val received = bytes.values.sum()
                val speed = (networkBytes / ((System.nanoTime() - started) / 1_000_000_000.0).coerceAtLeast(.1)).toLong()
                val eta = if (speed > 0) ((manifest.getLong("total_bytes") - received).coerceAtLeast(0) / speed) else 0
                dao.putTransfer(current.copy(status = stage, completedChapters = done, receivedBytes = received,
                    bytesPerSecond = speed, etaSeconds = eta, error = "", updatedAt = now))
                val label = "${stage.replaceFirstChar(Char::uppercase)} · $done/${chapters.size} chapters · ${received / 1048576} MB · ${speed / 1048576.0} MB/s · ~${eta}s"
                status(label)
                setForeground(notification(label, (received * 100 / manifest.getLong("total_bytes").coerceAtLeast(1)).toInt()))
                if (now - remoteReport > 2_000) {
                    remoteReport = now
                    client.transferAction(id, "progress", JSONObject().put("status", stage).put("received_bytes", received)
                        .put("bytes_per_second", speed).put("eta_seconds", eta))
                }
            }
        }
        update("receiving", true)
        // One global worker; no more than two chapter streams at any time.
        for (batch in chapters.chunked(2)) {
            batch.map { chapter -> async(Dispatchers.IO) {
                val key = "${chapter.seriesKey}/${chapter.chapter}"
                val file = File(staging, "${chapter.seriesKey}-${chapter.chapter}-${chapter.sha256}.cbz")
                if (!library.verified(chapter.seriesKey, chapter.chapter, chapter.sha256, chapter.size)) {
                    require(staging.usableSpace > chapter.size * 2 + 20_000_000) { "Not enough tablet storage for ${chapter.seriesTitle} chapter ${chapter.chapter}" }
                    if (!file.isFile || file.length() != chapter.size || CbzValidator.sha256(file) != chapter.sha256) {
                        client.downloadChapter(chapter, file) { count ->
                            mutex.withLock {
                                networkBytes += (count - (bytes[key] ?: 0)).coerceAtLeast(0)
                                bytes[key] = count
                            }
                            update("receiving")
                        }
                    }
                    update("verifying", true)
                    library.installCbz(file, chapter.seriesTitle, chapter.chapter, chapter.title,
                        chapter.sourceUrl, chapter.sha256, chapter.seriesKey, chapter.legacySeriesKeys)
                }
                // A generated thumbnail never blocks a supplied/updated cover.
                mutex.withLock {
                    if (covers.add(chapter.seriesKey)) {
                        val current = dao.series(chapter.seriesKey)
                        if (current?.coverVersion != chapter.coverVersion || current.coverPath?.let { File(it).isFile } != true) {
                            val cover = File(staging, "${chapter.seriesKey}.cover")
                            client.downloadCover(chapter.coverUrl, cover)
                            library.installCover(chapter.seriesKey, cover, chapter.coverVersion, chapter.coverProvenance)
                            cover.delete()
                        }
                    }
                    bytes[key] = chapter.size
                }
                update("verifying", true)
                client.transferAction(id, "ack", JSONObject().put("series_key", chapter.seriesKey).put("chapter", chapter.chapter).put("sha256", chapter.sha256))
                mutex.withLock { done++ }
                file.delete()
                update("receiving", true)
            } }.awaitAll()
        }
        dao.transfer(id)?.let { dao.putTransfer(it.copy(status = "ready", completedChapters = chapters.size,
            receivedBytes = it.totalBytes, etaSeconds = 0, error = "")) }
        staging.deleteRecursively()
        status("Ready · ${chapters.size} chapters in Reader")
    }

    private suspend fun publishInventory() {
        val inventory = JSONObject()
        dao.allChapters().forEach { chapter -> if (library.verified(chapter.seriesId, chapter.chapter, chapter.sha256, chapter.byteSize))
            inventory.put("${chapter.seriesId}/${chapter.chapter}", chapter.sha256) }
        dao.allSeries().forEach { series ->
            if (series.coverPath?.let { File(it).isFile } == true && series.coverVersion.isNotBlank())
                inventory.put("cover/${series.id}", series.coverVersion)
        }
        client.heartbeat(inventory)
    }

    private suspend fun importUsb() {
        val inbox = File(library.libraryRoot(), "inbox")
        val manifest = File(inbox, "usb-manifest.json")
        if (manifest.isFile) {
            val items = JSONArray(manifest.readText())
            var pending = false
            for (index in 0 until items.length()) {
                val item = items.getJSONObject(index)
                val name = item.getString("filename")
                require(Regex("[a-zA-Z0-9._-]+\\.cbz").matches(name))
                val file = File(inbox, name)
                if (!file.isFile) { pending = true; continue }
                library.installCbz(file, item.getString("series_title"), item.getString("chapter"), item.getString("title"),
                    "usb", item.getString("sha256"), item.getString("series_key"))
                file.delete()
            }
            if (!pending) manifest.delete()
        }
        val index = JSONObject()
        dao.allChapters().forEach { index.put("${it.seriesId}/${it.chapter}", it.sha256) }
        File(library.libraryRoot(), "sync-index.json").writeText(index.toString())
    }

    private fun notification(text: String, percent: Int): ForegroundInfo {
        val manager = applicationContext.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(NotificationChannel("panelyra-delivery", "Chapter delivery", NotificationManager.IMPORTANCE_LOW))
        fun action(name: String) = PendingIntent.getBroadcast(applicationContext, name.hashCode(),
            Intent(applicationContext, DeliveryControlReceiver::class.java).setAction("com.panelyra.tablet.$name"), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val open = PendingIntent.getActivity(applicationContext, 0, Intent(applicationContext, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
        val notification = NotificationCompat.Builder(applicationContext, "panelyra-delivery")
            .setSmallIcon(R.drawable.ic_panelyra).setContentTitle("Panelyra · Receiving Originals").setContentText(text)
            .setContentIntent(open).setOngoing(true).setProgress(100, percent, false)
            .addAction(0, "Pause", action("PAUSE")).addAction(0, "Resume", action("RESUME")).build()
        return if (Build.VERSION.SDK_INT >= 29) ForegroundInfo(140, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            else ForegroundInfo(140, notification)
    }
}
