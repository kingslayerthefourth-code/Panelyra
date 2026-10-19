package com.panelyra.tablet.network

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.URL
import java.net.InetSocketAddress
import java.net.Socket
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.X509TrustManager
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.ensureActive
import kotlin.coroutines.coroutineContext

data class PairingPayload(
    val baseUrl: String,
    val pairingSecret: String,
    val certificateSha256: String,
)

data class RemoteArtifact(
    val id: String,
    val series: String,
    val chapter: String,
    val title: String,
    val sha256: String,
    val downloadUrl: String,
    val size: Long = 0,
    val sourceUrl: String = "",
)

data class RemoteJob(
    val id: String,
    val status: String,
    val error: String,
    val artifacts: List<RemoteArtifact>,
)

data class CatalogChapter(
    val seriesKey: String, val seriesTitle: String, val chapter: String, val title: String,
    val size: Long, val sha256: String, val downloadUrl: String, val coverUrl: String,
    val coverVersion: String = "", val coverProvenance: String = "supplied", val sourceUrl: String = "",
    val legacySeriesKeys: List<String> = emptyList(),
)

data class LibraryCatalog(val chapters: List<CatalogChapter>, val errors: List<String>)

class CompanionClient(private val context: Context) {
    private val preferences = context.getSharedPreferences("panelyra_companion", Context.MODE_PRIVATE)

    val isPaired: Boolean get() = token().isNotBlank() && baseUrl().isNotBlank() && fingerprint().isNotBlank()

    @Synchronized fun reconnect(address: String): Boolean {
        if (!isPaired) return false
        val candidate = "https://${address.trim().removePrefix("https://").trimEnd('/')}"
        return runCatching {
            val identity = JSONObject(request("GET", "$candidate/api/v3/identity", base = candidate))
            require(identity.getString("identity").equals(fingerprint(), true))
            preferences.edit().putString("base_url", candidate).putString("mac_name", identity.getString("name")).commit()
        }.getOrDefault(false)
    }

    fun pendingTransfers(): List<JSONObject> {
        val array = JSONObject(request("GET", "${baseUrl()}/api/v3/transfers/pending")).getJSONArray("transfers")
        return (0 until array.length()).map(array::getJSONObject)
    }

    fun transfer(id: String): JSONObject = JSONObject(request("GET", "${baseUrl()}/api/v3/transfers/$id"))

    fun transferAction(id: String, action: String, body: JSONObject = JSONObject()): JSONObject =
        JSONObject(request("POST", "${baseUrl()}/api/v3/transfers/$id/$action", body.toString()))

    fun heartbeat(inventory: JSONObject? = null) {
        val body = JSONObject().put("device", android.os.Build.MODEL).put("version", com.panelyra.tablet.BuildConfig.VERSION_NAME)
        if (inventory != null) body.put("inventory", inventory)
        request("POST", "${baseUrl()}/api/v3/tablet", body.toString())
    }

    fun parseChapter(item: JSONObject): CatalogChapter = CatalogChapter(
        item.getString("series_key"), item.getString("series_title"), item.getString("chapter"),
        item.getString("title"), item.getLong("size"), item.getString("sha256"), item.getString("download_url"),
        item.getString("cover_url"), item.optString("cover_version"), item.optString("cover_provenance", "supplied"), item.optString("source_url"),
        item.optJSONArray("legacy_series_keys")?.let { list -> (0 until list.length()).map { list.getString(it) } }.orEmpty())

    fun parsePairing(raw: String): PairingPayload {
        val json = JSONObject(raw)
        require(json.optString("version") == "1") { "Unsupported pairing code." }
        val base = json.getString("base_url").trimEnd('/')
        require(base.startsWith("https://")) { "Pairing must use HTTPS." }
        return PairingPayload(base, json.getString("pairing_secret"), json.getString("certificate_sha256").lowercase())
    }

    fun pair(raw: String, deviceName: String = android.os.Build.MODEL): Boolean {
        val payload = parsePairing(raw)
        val response = request(
            "POST",
            "${payload.baseUrl}/api/v1/pair",
            JSONObject().put("pairing_secret", payload.pairingSecret).put("device_name", deviceName).toString(),
            base = payload.baseUrl,
            pin = payload.certificateSha256,
            bearer = "",
        )
        val token = JSONObject(response).getString("token")
        preferences.edit()
            .putString("base_url", payload.baseUrl)
            .putString("certificate_sha256", payload.certificateSha256)
            .putString("token", token)
            .apply()
        return token.isNotBlank()
    }

    fun pairWithCode(address: String, enteredCode: String, deviceName: String = android.os.Build.MODEL): Boolean {
        val base = if (address.trim().startsWith("https://")) address.trim().trimEnd('/')
            else "https://${address.trim().trimEnd('/')}"
        val url = URL(base)
        require(url.protocol == "https" && url.host.isNotBlank() && url.path.isEmpty()) { "Enter the Mac address shown in Panelyra." }
        val code = enteredCode.uppercase().filter { it.isLetterOrDigit() }
        require(code.length == 12) { "Enter the 12-character code from your Mac." }
        val clientNonce = ByteArray(24).also { SecureRandom().nextBytes(it) }.joinToString("") { "%02x".format(it) }
        val start = JSONObject(challenge(url.host, (if (url.port > 0) url.port else 8588) + 1,
            JSONObject().put("client_nonce", clientNonce).put("device_name", deviceName).toString()))
        val serverNonce = start.getString("server_nonce")
        val pin = start.getString("certificate_sha256").lowercase()
        require(Regex("[a-f0-9]{64}").matches(pin)) { "Invalid Mac certificate identity." }
        val expected = proof(code, "server", clientNonce, serverNonce, pin)
        require(MessageDigest.isEqual(expected.toByteArray(), start.getString("proof").toByteArray())) {
            "That Mac address and code do not match. Check both and try again."
        }
        val finish = JSONObject(request("POST", "$base/api/v2/pair/finish",
            JSONObject().put("client_nonce", clientNonce).put("server_nonce", serverNonce)
                .put("proof", proof(code, "client", clientNonce, serverNonce, pin)).toString(),
            base = base, pin = pin, bearer = ""))
        val newToken = finish.getString("token")
        preferences.edit().putString("base_url", base).putString("certificate_sha256", pin)
            .putString("token", newToken).apply()
        return newToken.isNotBlank()
    }

    private fun proof(code: String, role: String, clientNonce: String, serverNonce: String, pin: String): String {
        val mac = Mac.getInstance("HmacSHA256")
        mac.init(SecretKeySpec(code.toByteArray(), "HmacSHA256"))
        return mac.doFinal("panelyra-pair-v2|$role|$clientNonce|$serverNonce|$pin".toByteArray())
            .joinToString("") { "%02x".format(it) }
    }

    private fun challenge(host: String, port: Int, body: String): String {
        // No code, token, or private data crosses this transport; its reply is authenticated by HMAC.
        Socket().use { socket ->
            socket.connect(InetSocketAddress(host, port), 10_000)
            socket.soTimeout = 15_000
            val bytes = body.toByteArray(Charsets.UTF_8)
            val header = "POST /api/v2/pair/start HTTP/1.1\r\nHost: $host\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\n\r\n"
            socket.getOutputStream().write(header.toByteArray(Charsets.US_ASCII))
            socket.getOutputStream().write(bytes)
            socket.getOutputStream().flush()
            val input = socket.getInputStream().bufferedReader()
            val status = input.readLine().orEmpty()
            check(status.contains(" 200 ")) { "Mac pairing code expired or unavailable. Refresh it on the Mac." }
            var length = -1
            while (true) {
                val line = input.readLine() ?: error("Incomplete Mac response.")
                if (line.isEmpty()) break
                if (line.startsWith("Content-Length:", true)) length = line.substringAfter(':').trim().toIntOrNull() ?: -1
            }
            require(length in 1..4096) { "Invalid Mac response size." }
            val chars = CharArray(length)
            var offset = 0
            while (offset < length) {
                val count = input.read(chars, offset, length - offset)
                check(count > 0) { "Incomplete Mac response." }
                offset += count
            }
            return String(chars)
        }
    }

    fun unpair() {
        if (isPaired) runCatching { request("POST", "${baseUrl()}/api/v1/unpair", "{}") }
        preferences.edit().clear().apply()
    }

    fun submit(startUrl: String, chapterCount: Int, performance: String): RemoteJob {
        val body = JSONObject()
            .put("start_url", startUrl)
            .put("chapter_count", chapterCount)
            .put("performance", performance)
        return parseJob(request("POST", "${baseUrl()}/api/v1/jobs", body.toString()))
    }

    fun job(id: String): RemoteJob = parseJob(request("GET", "${baseUrl()}/api/v1/jobs/$id"))

    fun cancel(id: String) {
        request("POST", "${baseUrl()}/api/v1/jobs/$id/cancel", "{}")
    }

    fun catalog(): LibraryCatalog {
        val json = JSONObject(request("GET", "${baseUrl()}/api/v2/library"))
        val array = json.getJSONArray("chapters")
        val errors = json.optJSONArray("errors") ?: JSONArray()
        return LibraryCatalog((0 until array.length()).map { index ->
            val item = array.getJSONObject(index)
            parseChapter(item)
        }, (0 until errors.length()).map { index ->
            val item = errors.getJSONObject(index)
            "${item.optString("series")} chapter ${item.optString("chapter")}: ${item.optString("error")}"
        })
    }

    fun syncRequestGeneration(): Long = JSONObject(
        request("GET", "${baseUrl()}/api/v2/library/sync-request")
    ).optLong("generation", 0L)

    fun reportSyncStatus(message: String) {
        request("POST", "${baseUrl()}/api/v2/library/sync-status",
            JSONObject().put("message", message.take(300)).toString())
    }

    suspend fun downloadChapter(chapter: CatalogChapter, destination: File, progress: suspend (Long) -> Unit = {}) = withContext(Dispatchers.IO) {
        require(Regex("[a-f0-9]{64}").matches(chapter.sha256) && chapter.size in 1..1_000_000_000L) { "Invalid chapter manifest" }
        destination.parentFile?.mkdirs()
        val partial = File(destination.parentFile, destination.name + ".partial")
        val versionFile = File(destination.parentFile, destination.name + ".version")
        if (!versionFile.isFile || versionFile.readText() != "${chapter.sha256}|${chapter.size}") {
            partial.delete()
            versionFile.writeText("${chapter.sha256}|${chapter.size}")
        }
        var offset = partial.length()
        if (offset > chapter.size) { partial.delete(); offset = 0 }
        if (offset < chapter.size) {
            val connection = connection(URL(baseUrl() + chapter.downloadUrl), fingerprint())
            connection.requestMethod = "GET"
            connection.connectTimeout = 10_000
            connection.readTimeout = 120_000
            connection.setRequestProperty("Authorization", "Bearer ${token()}")
            if (offset > 0) { connection.setRequestProperty("Range", "bytes=$offset-"); connection.setRequestProperty("If-Range", "\"${chapter.sha256}\"") }
            try {
            connection.connect()
            check(connection.responseCode == if (offset > 0) 206 else 200) { errorBody(connection) }
            if (offset > 0) check(connection.getHeaderField("Content-Range")?.startsWith("bytes $offset-") == true)
            connection.inputStream.use { input -> java.io.FileOutputStream(partial, offset > 0).use { output ->
                val buffer = ByteArray(256 * 1024)
                var received = offset
                while (true) {
                    coroutineContext.ensureActive()
                    progress(received)
                    val n = input.read(buffer)
                    if (n < 0) break
                    received += n
                    require(received <= chapter.size) { "Transfer exceeded its expected size" }
                    output.write(buffer, 0, n)
                }
                output.fd.sync()
                progress(received)
            } }
            } finally { connection.disconnect() }
        }
        if (partial.length() != chapter.size || !sha256(partial).equals(chapter.sha256, true)) {
            partial.delete()
            error("The Mac transfer checksum did not match; retrying from the beginning.")
        }
        check(partial.renameTo(destination) || run { partial.copyTo(destination, overwrite = true); partial.delete() })
        versionFile.delete()
    }

    fun downloadCover(url: String, destination: File) {
        val connection = connection(URL(baseUrl() + url), fingerprint())
        connection.requestMethod = "GET"
        connection.connectTimeout = 10_000
        connection.readTimeout = 45_000
        connection.setRequestProperty("Authorization", "Bearer ${token()}")
        try {
        connection.connect()
        check(connection.responseCode == 200) { errorBody(connection) }
        require(connection.contentLengthLong in 64..5_000_000) { "Invalid cover size." }
        destination.parentFile?.mkdirs()
        val partial = File(destination.parentFile, destination.name + ".partial")
        connection.inputStream.use { input -> partial.outputStream().use { output ->
            var received = 0L
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                received += count
                require(received <= connection.contentLengthLong && received <= 5_000_000) { "Cover exceeded its expected size." }
                output.write(buffer, 0, count)
            }
        } }
        check(partial.length() == connection.contentLengthLong)
        check(partial.renameTo(destination) || run { partial.copyTo(destination, overwrite = true); partial.delete() })
        } finally { connection.disconnect() }
    }

    suspend fun download(artifact: RemoteArtifact, destination: File) {
        downloadChapter(CatalogChapter(artifact.series, artifact.series, artifact.chapter, artifact.title,
            artifact.size, artifact.sha256, artifact.downloadUrl, ""), destination)
    }

    private fun parseJob(raw: String): RemoteJob {
        val json = JSONObject(raw)
        val artifactsJson = json.optJSONArray("artifacts") ?: JSONArray()
        val artifacts = (0 until artifactsJson.length()).map { index ->
            val item = artifactsJson.getJSONObject(index)
            RemoteArtifact(
                item.getString("artifact_id"), item.getString("series"), item.getString("chapter"),
                item.getString("title"), item.getString("sha256"), item.getString("download_url"),
                item.optLong("size"), item.optString("source_url"),
            )
        }
        return RemoteJob(json.getString("job_id"), json.getString("status"), json.optString("error"), artifacts)
    }

    private fun request(
        method: String,
        url: String,
        body: String? = null,
        base: String = baseUrl(),
        pin: String = fingerprint(),
        bearer: String = token(),
    ): String {
        check(base.isNotBlank() && pin.isNotBlank()) { "Pair the Mac first." }
        val connection = connection(URL(url), pin)
        try {
        connection.requestMethod = method
        connection.connectTimeout = 10_000
        connection.readTimeout = 45_000
        connection.setRequestProperty("Accept", "application/json")
        if (bearer.isNotBlank()) connection.setRequestProperty("Authorization", "Bearer $bearer")
        if (body != null) {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json")
            connection.outputStream.use { it.write(body.toByteArray()) }
        }
        check(connection.responseCode in 200..299) { errorBody(connection) }
        return connection.inputStream.use { readBounded(it, 32_000_000) }
        } finally { connection.disconnect() }
    }

    private fun connection(url: URL, pin: String): HttpsURLConnection {
        val factory = socketFactories.getOrPut(pin) {
        val manager = object : X509TrustManager {
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
            override fun checkClientTrusted(chain: Array<X509Certificate>, authType: String) = Unit
            override fun checkServerTrusted(chain: Array<X509Certificate>, authType: String) {
                require(chain.isNotEmpty()) { "The Mac did not provide a certificate." }
                val actual = MessageDigest.getInstance("SHA-256").digest(chain.first().encoded)
                    .joinToString("") { "%02x".format(it) }
                require(actual.equals(pin, true)) { "The Mac certificate changed; pair again." }
                chain.first().checkValidity()
            }
        }
        val ssl = SSLContext.getInstance("TLS")
        ssl.init(null, arrayOf(manager), SecureRandom())
        ssl.socketFactory
        }
        return (url.openConnection() as HttpsURLConnection).apply {
            sslSocketFactory = factory
            // The exact saved certificate is the identity; DHCP addresses need not be in its SAN.
            hostnameVerifier = javax.net.ssl.HostnameVerifier { _, _ -> true }
            instanceFollowRedirects = false // Never leak the token to a redirected host.
        }
    }

    private fun errorBody(connection: HttpsURLConnection): String =
        runCatching { connection.errorStream?.use { readBounded(it, 65_536) } }.getOrNull()
            ?.let { runCatching { JSONObject(it).optString("error") }.getOrNull() }
            ?.takeIf(String::isNotBlank)
            ?: "The Mac companion returned HTTP ${connection.responseCode}."

    private fun baseUrl() = preferences.getString("base_url", "").orEmpty()
    private fun fingerprint() = preferences.getString("certificate_sha256", "").orEmpty()
    private fun token() = preferences.getString("token", "").orEmpty()

    companion object {
        internal fun readBounded(input: java.io.InputStream, limit: Int): String {
            val output = java.io.ByteArrayOutputStream()
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                require(output.size().toLong() + count <= limit) { "Mac response exceeded the size limit." }
                output.write(buffer, 0, count)
            }
            return output.toString(Charsets.UTF_8.name())
        }
        private val socketFactories = java.util.concurrent.ConcurrentHashMap<String, javax.net.ssl.SSLSocketFactory>()
        fun sha256(file: File): String {
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { input ->
                val buffer = ByteArray(256 * 1024)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    digest.update(buffer, 0, count)
                }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }
    }
}
