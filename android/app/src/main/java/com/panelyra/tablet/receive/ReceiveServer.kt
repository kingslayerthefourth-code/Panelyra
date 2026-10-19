package com.panelyra.tablet.receive

import org.json.JSONObject
import java.io.*
import java.net.Socket
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLServerSocket
import kotlin.concurrent.thread

/** One authenticated upload at a time. No filesystem path is accepted from the sender. */
class ReceiveServer(
    private val tls: SSLContext,
    val fingerprint: String,
    private val staging: File,
    private val device: String,
    initialToken: String,
    private val saveToken: (String) -> Unit,
    private val verified: (JSONObject) -> Boolean,
    private val install: (File, JSONObject) -> Unit,
    private val status: (String) -> Unit,
    private val clock: () -> Long = System::currentTimeMillis,
    private val appInfo: () -> JSONObject = { JSONObject() },
) : Closeable {
    @Volatile var token = initialToken; private set
    val code = (1..12).map { "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"[random.nextInt(32)] }.joinToString("")
    private val expires = clock() + 5 * 60_000
    private var failures = 0
    private val challenges = linkedMapOf<String, Pair<String, Long>>()
    @Volatile private var listener: SSLServerSocket? = null
    @Volatile private var active: Socket? = null
    @Volatile private var stopped = false
    val port get() = listener?.localPort ?: 0

    fun start(port: Int = 8590) {
        staging.mkdirs()
        // Only our hash-named staging records are eligible for cleanup.
        staging.listFiles().orEmpty().filter { Regex("[a-f0-9]{64}\\.(part|json)").matches(it.name) && clock() - it.lastModified() > 7L * 86_400_000 }
            .forEach { it.delete() }
        val server = tls.serverSocketFactory.createServerSocket(port, 4) as SSLServerSocket
        server.enabledProtocols = server.supportedProtocols.filter { it in setOf("TLSv1.2", "TLSv1.3") }.toTypedArray()
        listener = server
        thread(name = "panelyra-receive", isDaemon = true) {
            while (!stopped) {
                try {
                    val socket = server.accept()
                    active = socket
                    socket.use { handle(it) }
                } catch (error: Exception) {
                    if (!stopped) status("Transfer interrupted. Retry from the Mac; received bytes are kept.")
                } finally { active = null }
            }
        }
    }

    override fun close() {
        stopped = true
        runCatching { listener?.close() }
        runCatching { active?.close() }
    }

    private fun line(input: InputStream): String {
        val bytes = ByteArrayOutputStream()
        while (bytes.size() <= 4096) {
            val b = input.read()
            require(b >= 0) { "Incomplete request" }
            if (b == 10) return bytes.toString("US-ASCII").trimEnd('\r')
            bytes.write(b)
        }
        error("Header too long")
    }

    private fun handle(socket: Socket) {
        socket.soTimeout = 30_000
        val input = BufferedInputStream(socket.getInputStream())
        val output = socket.getOutputStream()
        fun reply(code: Int, body: JSONObject) {
            val bytes = body.toString().toByteArray(Charsets.UTF_8)
            output.write("HTTP/1.1 $code Result\r\nContent-Type: application/json\r\nContent-Length: ${bytes.size}\r\nConnection: close\r\nCache-Control: no-store\r\n\r\n".toByteArray())
            output.write(bytes); output.flush()
        }
        var authenticated = false
        try {
            val request = line(input).split(' ')
            require(request.size == 3 && request[2] == "HTTP/1.1")
            val (method, path) = request
            val headers = mutableMapOf<String, String>()
            var total = 0
            while (true) {
                val header = line(input); total += header.length
                require(total <= 16_384 && headers.size < 40)
                if (header.isEmpty()) break
                val name = header.substringBefore(':').lowercase()
                require(':' in header && name !in headers)
                headers[name] = header.substringAfter(':').trim()
            }
            require("transfer-encoding" !in headers)
            val length = headers["content-length"]?.toLong() ?: 0L
            require(length in 0..1_000_000_000L)
            fun json(): JSONObject {
                require(length <= 16_384)
                val bytes = ByteArray(length.toInt())
                DataInputStream(input).readFully(bytes)
                return JSONObject(String(bytes, Charsets.UTF_8))
            }
            if (method == "POST" && path.startsWith("/v1/pair/")) {
                require(token.isEmpty()) { "Already paired. Forget the Mac on the tablet before pairing another." }
                require(clock() < expires && failures < 5) { "Setup code expired. Stop and start Receive mode for a new code." }
                val body = json()
                val client = body.getString("client_nonce")
                require(Regex("[a-f0-9]{64}").matches(client))
                if (path == "/v1/pair/start") {
                    challenges.entries.removeAll { it.value.second < clock() }
                    require(challenges.size < 8) { "Too many pairing attempts. Try again in a minute." }
                    val server = randomHex()
                    challenges[client] = server to (clock() + 60_000)
                    reply(200, JSONObject().put("server_nonce", server).put("proof", proof(code, "server", client, server, fingerprint)))
                } else if (path == "/v1/pair/finish") {
                    failures++
                    val challenge = challenges.remove(client)
                    require(challenge != null && clock() < challenge.second && body.getString("server_nonce") == challenge.first &&
                        equal(proof(code, "client", client, challenge.first, fingerprint), body.getString("proof"))) { "Pairing code did not match. Check the tablet code." }
                    val newToken = randomHex()
                    saveToken(newToken) // Commit before reporting success; a restarted service keeps this pairing.
                    token = newToken
                    challenges.clear()
                    status("Connected to Mac · Ready to receive chapters")
                    reply(200, JSONObject().put("token", token).put("device", device))
                } else reply(404, JSONObject().put("error", "Unknown endpoint"))
                return
            }
            if (token.isEmpty() || !equal("Bearer $token", headers["authorization"].orEmpty())) {
                reply(401, JSONObject().put("error", "Tablet is not paired with this Mac. Connect in Settings → Connections.")); return
            }
            authenticated = true
            when {
                method == "GET" && path == "/v1/status" -> reply(200, appInfo().put("device", device).put("status", "ready"))
                method == "POST" && path == "/v1/offer" -> {
                    val body = json(); validate(body)
                    val id = digest("${body.getString("kind")}|${body.getString("series_key")}|${body.optString("chapter")}|${body.getString("sha256")}".toByteArray())
                    val file = File(staging, "$id.part")
                    val done = verified(body)
                    if (done) { file.delete(); File(staging, "$id.json").delete() }
                    else {
                        require(file.length() <= body.getLong("size"))
                        require(staging.usableSpace > 2 * body.getLong("size") - file.length() + 10_000_000) { "Not enough tablet storage" }
                        require(staging.listFiles().orEmpty().count { it.extension == "json" } < 200 || File(staging, "$id.json").exists()) { "Too many unfinished transfers" }
                        File(staging, "$id.json").writeText(body.toString())
                    }
                    reply(200, JSONObject().put("id", id).put("ready", done).put("offset", if (done) body.getLong("size") else file.length()))
                }
                method == "PUT" && Regex("/v1/upload/[a-f0-9]{64}").matches(path) -> {
                    val id = path.substringAfterLast('/'); val body = manifest(id)
                    val file = File(staging, "$id.part")
                    val offset = headers["x-offset"]?.toLong() ?: -1
                    require(offset == file.length() && length == body.getLong("size") - offset) { "Transfer position changed. Retry from the Mac." }
                    var received = offset; var last = 0L
                    FileOutputStream(file, true).use { out ->
                        val buffer = ByteArray(256 * 1024)
                        while (received < body.getLong("size")) {
                            val n = input.read(buffer, 0, minOf(buffer.size.toLong(), body.getLong("size") - received).toInt())
                            require(n > 0) { "Transfer interrupted" }
                            out.write(buffer, 0, n); received += n
                            if (clock() - last > 500) { status("Receiving ${body.optString("title", "cover")} · ${received * 100 / body.getLong("size")}%"); last = clock() }
                        }
                        out.fd.sync()
                    }
                    reply(200, JSONObject().put("offset", received))
                }
                method == "POST" && Regex("/v1/commit/[a-f0-9]{64}").matches(path) -> {
                    val id = path.substringAfterLast('/'); val body = manifest(id); val file = File(staging, "$id.part")
                    require(file.length() == body.getLong("size")) { "Incomplete transfer. Retry from the Mac." }
                    status("Verifying ${body.optString("title", "cover")}…")
                    if (fileDigest(file) != body.getString("sha256")) { file.delete(); error("Checksum mismatch. Retry to send a fresh copy.") }
                    install(file, body)
                    file.delete(); File(staging, "$id.json").delete()
                    status("Received ${body.optString("title", "cover")} · Ready for more")
                    reply(200, JSONObject().put("ready", true))
                }
                else -> reply(404, JSONObject().put("error", "Unknown endpoint"))
            }
        } catch (error: Exception) {
            val detail = if (error is IOException) "Transfer interrupted. Retry from the Mac." else error.message?.take(200) ?: "Transfer failed"
            if (authenticated) status("Transfer not complete: $detail")
            runCatching { reply(400, JSONObject().put("error", detail)) }
        }
    }

    private fun manifest(id: String) = JSONObject(File(staging, "$id.json").readText()).also(::validate)

    private fun validate(body: JSONObject) {
        fun identity(value: String) = value.length in 1..180 && value !in setOf(".", "..") && Regex("[a-zA-Z0-9._-]+").matches(value)
        val kind = body.getString("kind")
        require(kind in setOf("chapter", "cover", "apk") && identity(body.getString("series_key"))) { "Invalid chapter identity" }
        require(Regex("[a-f0-9]{64}").matches(body.getString("sha256")))
        require(body.getLong("size") in 1..when (kind) { "cover" -> 10_000_000L; "apk" -> 200_000_000L; else -> 1_000_000_000L })
        if (kind == "apk") require(body.getLong("version_code") > 0 && body.getString("series_key") == "panelyra-tablet-app") { "Invalid app update" }
        if (kind == "chapter") {
            require(identity(body.getString("chapter")))
            require(body.getString("series_title").length in 1..500 && body.getString("title").length in 1..500)
        }
    }

    companion object {
        private val random = SecureRandom()
        fun randomHex() = ByteArray(32).also(random::nextBytes).joinToString("") { "%02x".format(it) }
        fun digest(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        fun fileDigest(file: File): String {
            val md = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { input -> val buffer = ByteArray(256 * 1024); while (true) { val n = input.read(buffer); if (n < 0) break; md.update(buffer, 0, n) } }
            return md.digest().joinToString("") { "%02x".format(it) }
        }
        fun equal(a: String, b: String) = MessageDigest.isEqual(a.toByteArray(), b.toByteArray())
        fun proof(code: String, role: String, client: String, server: String, pin: String): String {
            val mac = Mac.getInstance("HmacSHA256")
            mac.init(SecretKeySpec(code.toByteArray(), "HmacSHA256"))
            return mac.doFinal("panelyra-push-v1|$role|$client|$server|$pin".toByteArray()).joinToString("") { "%02x".format(it) }
        }
    }
}
