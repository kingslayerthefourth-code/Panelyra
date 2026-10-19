package com.panelyra.tablet.receive

import android.content.Context
import android.graphics.Bitmap
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.panelyra.tablet.data.LibraryRepository
import com.panelyra.tablet.data.PanelyraDatabase
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.*
import java.security.KeyStore
import java.security.cert.X509Certificate
import java.util.concurrent.TimeUnit
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream
import javax.net.ssl.*

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], application = android.app.Application::class)
class ReceiveServerTest {
    private lateinit var root: File
    private lateinit var tls: SSLContext
    private lateinit var pin: String
    private var server: ReceiveServer? = null
    private var token = ""
    private var now = 1_000L
    @Before fun setup() {
        root = kotlin.io.path.createTempDirectory("receive-test-").toFile()
        val store = KeyStore.getInstance("PKCS12")
        javaClass.getResourceAsStream("/receive-test.p12")!!.use { store.load(it, "test-only".toCharArray()) }
        val factory = KeyManagerFactory.getInstance(KeyManagerFactory.getDefaultAlgorithm()).apply { init(store, "test-only".toCharArray()) }
        tls = SSLContext.getInstance("TLS").apply { init(factory.keyManagers, null, null) }
        pin = ReceiveServer.digest(store.getCertificate("test").encoded)
    }
    @After fun cleanup() { server?.close(); root.deleteRecursively() }
    private fun start(saved: String = token, install: (File, JSONObject) -> Unit = { _, _ -> }, verified: (JSONObject) -> Boolean = { false }): ReceiveServer {
        return ReceiveServer(tls, pin, File(root, "staging"), "Test tablet", saved, { token = it }, verified, install, {}, { now }).also { it.start(0); server = it }
    }
    private fun request(method: String, path: String, body: String = "", auth: String = token): Pair<Int, JSONObject> {
        val context = SSLContext.getInstance("TLS").apply { init(null, arrayOf<TrustManager>(object : X509TrustManager {
            override fun getAcceptedIssuers() = emptyArray<X509Certificate>()
            override fun checkClientTrusted(chain: Array<X509Certificate>, type: String) { error("Not a client") }
            override fun checkServerTrusted(chain: Array<X509Certificate>, type: String) { check(ReceiveServer.digest(chain[0].encoded) == pin) }
        }), null) }
        return (context.socketFactory.createSocket("127.0.0.1", server!!.port) as SSLSocket).use { socket ->
            socket.soTimeout = 5000
            val data = body.toByteArray()
            val headers = "$method $path HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer $auth\r\nContent-Length: ${data.size}\r\nConnection: close\r\n\r\n"
            socket.outputStream.write(headers.toByteArray() + data); socket.outputStream.flush()
            val raw = socket.inputStream.readBytes().toString(Charsets.UTF_8)
            raw.substringBefore("\r\n").split(' ')[1].toInt() to JSONObject(raw.substringAfter("\r\n\r\n"))
        }
    }
    private fun pair(receiver: ReceiveServer): String {
        val client = "a".repeat(64)
        val challenge = request("POST", "/v1/pair/start", JSONObject().put("client_nonce", client).toString()).second
        assertEquals(ReceiveServer.proof(receiver.code, "server", client, challenge.getString("server_nonce"), pin), challenge.getString("proof"))
        val finish = JSONObject().put("client_nonce", client).put("server_nonce", challenge.getString("server_nonce"))
            .put("proof", ReceiveServer.proof(receiver.code, "client", client, challenge.getString("server_nonce"), pin))
        val response = request("POST", "/v1/pair/finish", finish.toString())
        assertEquals(200, response.first)
        return response.second.getString("token")
    }
    @Test fun pairingPersistsAcrossRestartAndDoesNotExpire() {
        val first = start(); val paired = pair(first)
        assertEquals(paired, token)
        now += 365L * 86_400_000
        assertEquals(200, request("GET", "/v1/status").first)
        first.close(); start(paired)
        assertEquals(200, request("GET", "/v1/status").first)
        assertEquals(401, request("GET", "/v1/status", auth = "bad").first)
        assertEquals(400, request("POST", "/v1/pair/start", "{\"client_nonce\":\"${"b".repeat(64)}\"}").first)
    }
    @Test fun unusedCodeExpiresAndGuessedProofCannotPair() {
        start()
        val nonce = "b".repeat(64)
        val first = request("POST", "/v1/pair/start", "{\"client_nonce\":\"$nonce\"}").second
        val bad = JSONObject().put("client_nonce", nonce).put("server_nonce", first.getString("server_nonce")).put("proof", "0".repeat(64))
        assertEquals(400, request("POST", "/v1/pair/finish", bad.toString()).first)
        assertEquals("", token)
        now += 300_001
        assertEquals(400, request("POST", "/v1/pair/start", "{\"client_nonce\":\"$nonce\"}").first)
    }
    @Test fun invalidManifestAndIncompleteCommitNeverInstall() {
        var installed = false
        val receiver = start(install = { _, _ -> installed = true }); pair(receiver)
        val meta = JSONObject().put("kind", "chapter").put("series_key", "book").put("chapter", "1.5")
            .put("series_title", "Book").put("title", "Chapter 1.5").put("size", 100).put("sha256", "a".repeat(64))
        val offered = request("POST", "/v1/offer", meta.toString())
        assertEquals(200, offered.first)
        assertEquals(400, request("POST", "/v1/commit/${offered.second.getString("id")}").first)
        assertEquals(400, request("POST", "/v1/offer", meta.put("series_key", "../escape").toString()).first)
        assertFalse(installed)
    }
    @Test fun actualPythonClientResumesImportsAndSkipsDuplicates() = runBlocking {
        val python = System.getenv("PANELYRA_TEST_PYTHON")
        val sourceRoot = System.getenv("PANELYRA_PUSH_ROOT")
        Assume.assumeTrue("Set PANELYRA_TEST_PYTHON and PANELYRA_PUSH_ROOT for cross-language test", python != null && sourceRoot != null)
        val context = ApplicationProvider.getApplicationContext<Context>()
        File(context.getExternalFilesDir(null), "Panelyra").deleteRecursively()
        val db = Room.inMemoryDatabaseBuilder(context, PanelyraDatabase::class.java).allowMainThreadQueries().build()
        try {
            val library = LibraryRepository(context, db.dao())
            val image = ByteArrayOutputStream()
            Bitmap.createBitmap(8, 8, Bitmap.Config.ARGB_8888).apply { eraseColor(android.graphics.Color.CYAN); compress(Bitmap.CompressFormat.PNG, 100, image); recycle() }
            val cbz = File(root, "fixture.cbz")
            ZipOutputStream(cbz.outputStream()).use { zip -> zip.putNextEntry(ZipEntry("01.png")); zip.write(image.toByteArray()); zip.closeEntry() }
            var installs = 0
            val receiver = start(install = { file, meta -> runBlocking {
                library.installCbz(file, meta.getString("series_title"), meta.getString("chapter"), meta.getString("title"), "mac-push", meta.getString("sha256"), meta.getString("series_key"))
                db.dao().savePosition("push-book", "1.5", 0, 37, "continuous")
                installs++
            } }, verified = { meta -> runBlocking { library.verified(meta.getString("series_key"), meta.getString("chapter"), meta.getString("sha256"), meta.getLong("size")) } })
            val output = File(root, "python-output.txt")
            val process = ProcessBuilder(python!!, "$sourceRoot/tests/push_protocol_client.py", "127.0.0.1:${receiver.port}", receiver.code, cbz.path)
                .redirectErrorStream(true).redirectOutput(output).start()
            assertTrue("Python timed out", process.waitFor(60, TimeUnit.SECONDS))
            assertEquals(output.readText(), 0, process.exitValue())
            assertEquals(1, installs)
            val saved = db.dao().chapter("push-book", "1.5")!!
            assertEquals(37, saved.scrollOffset)
            assertEquals(1, saved.pageCount)
            assertEquals(1, db.dao().allChapters().size)
        } finally { db.close() }
    }
}
