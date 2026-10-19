package com.panelyra.tablet.receive

import android.app.*
import android.content.*
import android.content.pm.ServiceInfo
import android.os.*
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import androidx.core.app.NotificationCompat
import androidx.core.content.ContextCompat
import com.panelyra.tablet.MainActivity
import com.panelyra.tablet.R
import com.panelyra.tablet.data.LibraryRepository
import com.panelyra.tablet.data.PanelyraDatabase
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import java.io.File
import java.math.BigInteger
import java.net.Inet4Address
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.util.Date
import javax.net.ssl.*
import javax.security.auth.x500.X500Principal

data class ReceiveState(val running: Boolean = false, val paired: Boolean = false, val address: String = "", val code: String = "", val message: String = "Receive mode is off")

class TabletReceiveService : Service() {
    private var server: ReceiveServer? = null
    private var discovery: java.net.DatagramSocket? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val prefs by lazy { getSharedPreferences("panelyra_receive", MODE_PRIVATE) }
    override fun onBind(intent: Intent?) = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == "stop" || intent?.action == "forget") {
            if (intent.action == "forget") {
                prefs.edit().remove("token").commit()
                state.value = state.value.copy(paired = false, code = "")
            }
            prefs.edit().putBoolean("enabled", false).apply()
            stopSelf(); return START_NOT_STICKY
        }
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel(CHANNEL, "Receive from Mac", NotificationManager.IMPORTANCE_LOW))
        val notification = notification("Ready for your Mac")
        if (Build.VERSION.SDK_INT >= 29) startForeground(ID, notification, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        else startForeground(ID, notification)
        if (server != null || state.value.message == "Starting Receive mode…") return START_STICKY
        state.value = ReceiveState(paired = prefs.getString("token", "").orEmpty().isNotEmpty(), message = "Starting Receive mode…")
        prefs.edit().putBoolean("enabled", true).apply()
        scope.launch {
            try {
                val (tls, pin) = identity()
                val dao = PanelyraDatabase.get(this@TabletReceiveService).dao()
                val library = LibraryRepository(this@TabletReceiveService, dao)
                val updates = com.panelyra.tablet.update.AppUpdates(this@TabletReceiveService)
                val receiver = ReceiveServer(tls, pin, File(filesDir, "receive-staging"), Build.MODEL,
                    prefs.getString("token", "").orEmpty(),
                    saveToken = { token ->
                        check(prefs.edit().putString("token", token).commit()) { "Could not save pairing. Try again." }
                        state.value = state.value.copy(paired = true, code = "")
                    },
                    verified = { meta -> runBlocking {
                        if (meta.getString("kind") == "apk") updates.alreadyReceived(meta)
                        else if (meta.getString("kind") == "chapter") library.verified(meta.getString("series_key"), meta.getString("chapter"), meta.getString("sha256"), meta.getLong("size"))
                        else dao.series(meta.getString("series_key"))?.let { series ->
                            val cover = series.coverPath?.let(::File)
                            cover != null && cover.isFile && cover.length() == meta.getLong("size") && ReceiveServer.fileDigest(cover) == meta.getString("sha256")
                        } ?: false
                    } },
                    install = { file, meta -> runBlocking {
                        if (meta.getString("kind") == "apk") updates.receive(file, meta)
                        else if (meta.getString("kind") == "chapter") library.installCbz(file, meta.getString("series_title"), meta.getString("chapter"), meta.getString("title"), meta.optString("source_url", "mac-push"), meta.getString("sha256"), meta.getString("series_key"),
                            meta.optJSONArray("legacy_series_keys")?.let { list -> (0 until list.length()).map { list.getString(it) } }.orEmpty())
                        else {
                            check(dao.series(meta.getString("series_key")) != null) { "Send the chapter before its cover" }
                            library.installCover(meta.getString("series_key"), file, meta.getString("sha256"), meta.optString("cover_provenance", "supplied"))
                        }
                    } },
                    status = { message ->
                        state.value = state.value.copy(message = message)
                        manager.notify(ID, notification(message))
                    }, appInfo = { org.json.JSONObject().put("app_version_code", com.panelyra.tablet.BuildConfig.VERSION_CODE)
                        .put("app_version_name", com.panelyra.tablet.BuildConfig.VERSION_NAME).put("apk_updates", updates.enabled).put("update_received_code", updates.pendingCode) })
                ensureActive()
                receiver.start()
                server = receiver
                scope.launch { discoverable(pin, receiver.port) }
                state.value = ReceiveState(true, receiver.token.isNotEmpty(), address(receiver.port), if (receiver.token.isEmpty()) receiver.code else "",
                    if (receiver.token.isEmpty()) "Enter this address and code on your Mac" else "Mac pairing saved · Ready to receive")
                while (isActive) {
                    delay(3000)
                    state.value = state.value.copy(address = address(receiver.port))
                }
            } catch (error: CancellationException) { throw error }
            catch (error: Exception) {
                state.value = state.value.copy(running = false, message = "Receive mode could not start: ${error.message}")
                stopSelf()
            }
        }
        return START_STICKY
    }

    private suspend fun discoverable(pin: String, port: Int) = withContext(Dispatchers.IO) {
        try {
            java.net.DatagramSocket(8591).use { socket ->
                discovery = socket
                socket.soTimeout = 1000
                while (isActive) {
                    try {
                        val packet = java.net.DatagramPacket(ByteArray(1024), 1024)
                        socket.receive(packet)
                        val token = prefs.getString("token", "").orEmpty()
                        if (token.isEmpty()) continue
                        val body = org.json.JSONObject(String(packet.data, 0, packet.length, Charsets.UTF_8))
                        val nonce = body.optString("nonce")
                        if (body.optString("protocol") != "panelyra-discovery-v1" || !Regex("[a-f0-9]{64}").matches(nonce)) continue
                        val mac = javax.crypto.Mac.getInstance("HmacSHA256")
                        mac.init(javax.crypto.spec.SecretKeySpec(token.toByteArray(), "HmacSHA256"))
                        val proof = mac.doFinal("discover|$nonce|$pin|$port".toByteArray()).joinToString("") { "%02x".format(it) }
                        val response = org.json.JSONObject().put("nonce",nonce).put("pin",pin).put("port",port).put("proof",proof).toString().toByteArray()
                        socket.send(java.net.DatagramPacket(response, response.size, packet.address, packet.port))
                    } catch (_: java.net.SocketTimeoutException) { }
                    catch (_: org.json.JSONException) { }
                }
            }
        } catch (error: java.net.SocketException) {
            // Discovery is optional; manual address and pinned HTTPS remain usable.
        } finally { discovery = null }
    }

    override fun onDestroy() {
        discovery?.close();discovery=null
        scope.cancel(); server?.close(); server = null
        state.value = state.value.copy(running = false, address = "", code = "", message = if (state.value.message.startsWith("Receive mode could not start")) state.value.message else "Receive mode is off · Pairing is remembered")
        super.onDestroy()
    }

    private fun notification(text: String): Notification {
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 1, Intent(this, TabletReceiveService::class.java).setAction("stop"), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return NotificationCompat.Builder(this, CHANNEL).setSmallIcon(R.drawable.ic_panelyra)
            .setContentTitle("Panelyra · Receive from Mac").setContentText(text).setContentIntent(open)
            .setOngoing(true).setOnlyAlertOnce(true).addAction(0, "Stop receiving", stop).build()
    }

    private fun address(port: Int): String {
        val connectivity = getSystemService(ConnectivityManager::class.java)
        val wifi = connectivity.allNetworks.firstOrNull { connectivity.getNetworkCapabilities(it)?.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) == true }
        val ip = wifi?.let { connectivity.getLinkProperties(it)?.linkAddresses?.firstOrNull { it.address is Inet4Address }?.address?.hostAddress }
        return ip?.let { "$it:$port" } ?: "Connect this tablet to Wi-Fi"
    }

    private fun identity(): Pair<SSLContext, String> {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (!store.containsAlias(ALIAS)) {
            KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_RSA, "AndroidKeyStore").apply {
                initialize(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY)
                    // Conscrypt hashes/pads TLS signatures before invoking the hardware key.
                    .setKeySize(2048).setDigests(KeyProperties.DIGEST_NONE, KeyProperties.DIGEST_SHA256, KeyProperties.DIGEST_SHA384, KeyProperties.DIGEST_SHA512)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE, KeyProperties.ENCRYPTION_PADDING_RSA_PKCS1)
                    .setSignaturePaddings(KeyProperties.SIGNATURE_PADDING_RSA_PKCS1, KeyProperties.SIGNATURE_PADDING_RSA_PSS)
                    .setCertificateSubject(X500Principal("CN=Panelyra Tablet"))
                    .setCertificateSerialNumber(BigInteger.ONE).setCertificateNotBefore(Date(0))
                    .setCertificateNotAfter(Date(4_102_444_800_000L)).build())
                generateKeyPair()
            }
        }
        val factory = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(store, null) }
        val context = SSLContext.getInstance("TLS").apply { init(factory.keyManagers, null, null) }
        return context to ReceiveServer.digest(store.getCertificate(ALIAS).encoded)
    }

    companion object {
        private const val ALIAS = "panelyra-tablet-receive-v1"
        private const val CHANNEL = "panelyra-receive"
        private const val ID = 141
        val state = MutableStateFlow(ReceiveState())
        fun start(context: Context) = ContextCompat.startForegroundService(context, Intent(context, TabletReceiveService::class.java))
        fun stop(context: Context, forget: Boolean = false) {
            if (forget) context.getSharedPreferences("panelyra_receive", MODE_PRIVATE).edit().remove("token").commit()
            context.getSharedPreferences("panelyra_receive", MODE_PRIVATE).edit().putBoolean("enabled", false).apply()
            context.stopService(Intent(context, TabletReceiveService::class.java))
            state.value = ReceiveState(paired = !forget && context.getSharedPreferences("panelyra_receive", MODE_PRIVATE).getString("token", "").orEmpty().isNotEmpty(), message = if (forget) "Mac forgotten. Start Receive mode to pair again." else "Receive mode is off · Pairing is remembered")
        }
    }
}
