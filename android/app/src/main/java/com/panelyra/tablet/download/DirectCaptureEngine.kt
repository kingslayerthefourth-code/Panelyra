package com.panelyra.tablet.download

import android.content.Context
import android.graphics.BitmapFactory
import com.panelyra.tablet.data.LibraryRepository
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.delay
import kotlinx.coroutines.CancellationException
import kotlin.coroutines.coroutineContext
import org.jsoup.Jsoup
import org.jsoup.nodes.Document
import java.io.File
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.security.MessageDigest
import java.util.UUID
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

data class CapturedChapter(
    val cbz: File,
    val seriesTitle: String,
    val chapterLabel: String,
    val title: String,
    val sourceUrl: String,
)

class CaptureFailure(message: String, val browserMayHelp: Boolean = false) : Exception(message)

object ChapterRouting {
    private val pattern = Regex(
        "(?:chapter|chap|ch|episode|ep)[-_/.:\\s]*(\\d+(?:\\.\\d+)?)",
        RegexOption.IGNORE_CASE,
    )

    fun number(value: String): Double? =
        pattern.findAll(value).lastOrNull()?.groupValues?.getOrNull(1)?.toDoubleOrNull()

    fun increment(url: String): String? {
        val match = pattern.findAll(url.substringBefore('?').substringBefore('#')).lastOrNull() ?: return null
        val number = match.groupValues[1]
        val integer = number.toIntOrNull() ?: return null
        val start = match.groups[1]?.range?.first ?: return null
        val end = match.groups[1]?.range?.last?.plus(1) ?: return null
        return url.substring(0, start) + (integer + 1) + url.substring(end)
    }
}

class DirectCaptureEngine(private val context: Context) {
    private val validator = CbzValidator()
    private val blockedMarkers = listOf(
        "checking your browser", "verify you are human", "cf-chl-", "login to continue",
        "sign in to continue", "cloudflare ray id",
    )
    private val promoMarkers = listOf(
        "logo", "avatar", "discord", "telegram", "banner", "advert", "promo", "recommend",
        "favicon", "icon", "placeholder", "loading", "spacer",
    )

    suspend fun capture(
        startUrl: String,
        chapterCount: Int,
        performance: String,
        cookieHeader: String? = null,
        firstPageImages: List<String>? = null,
        onChapter: suspend (CapturedChapter, String?) -> Unit = { _, _ -> },
        onProgress: suspend (Int) -> Unit = {},
        onActivity: suspend (CaptureActivity) -> Unit = {},
    ): List<CapturedChapter> = withContext(Dispatchers.IO) {
        require(chapterCount in 1..10000)
        val attempts = if (performance == "rapid") 1 else 3
        var currentUrl = startUrl
        val completed = mutableListOf<CapturedChapter>()
        val seen = mutableSetOf<String>()
        repeat(chapterCount) { index ->
            coroutineContext.ensureActive()
            val canonical = canonical(currentUrl)
            if (!seen.add(canonical)) throw CaptureFailure("The chapter route repeated instead of advancing.")
            onActivity(CaptureActivity("Finding chapter pages"))
            val html = requestText(currentUrl, attempts, cookieHeader)
            if (blockedMarkers.any { html.contains(it, ignoreCase = true) }) {
                throw CaptureFailure("This source requires a browser or the paired Mac.", true)
            }
            val document = Jsoup.parse(html, currentUrl)
            val title = document.title().ifBlank { "Chapter ${index + 1}" }
            val currentNumber = ChapterRouting.number("$title $currentUrl")
            val images = if (index == 0 && !firstPageImages.isNullOrEmpty()) {
                firstPageImages
            } else {
                imageUrls(document)
            }
            if (images.isEmpty()) throw CaptureFailure("No chapter pages were found in the static page.", true)
            val seriesTitle = seriesTitle(title, currentUrl)
            val chapterLabel = currentNumber?.let(::numberLabel) ?: "%02d".format(index + 1)
            onActivity(CaptureActivity("Receiving pages", pages = images.size))
            val cbz = buildCbz(images, currentUrl, attempts, cookieHeader, onActivity)
            onActivity(CaptureActivity("Checking archive", images.size, images.size, cbz.length()))
            validator.validate(cbz)
            val captured = CapturedChapter(cbz, seriesTitle, chapterLabel, title, currentUrl)
            completed += captured
            val nextUrl = nextChapter(document, currentUrl, currentNumber)
            onChapter(captured, nextUrl)
            onProgress(index + 1)
            if (index + 1 < chapterCount) {
                currentUrl = nextUrl
                    ?: throw CaptureFailure("A strictly later chapter could not be found.", true)
                val nextNumber = ChapterRouting.number(currentUrl)
                if (currentNumber != null && nextNumber != null && nextNumber <= currentNumber) {
                    throw CaptureFailure("The source attempted to navigate backward.")
                }
            }
        }
        completed
    }

    fun imageUrls(document: Document): List<String> {
        val focused = document.select(
            ".reading-content img, #readerarea img, #chapter-content img, .chapter-content img, " +
                ".entry-content img, .page-break img, .chapter-image img"
        )
        val nodes = if (focused.isNotEmpty()) focused else document.select("img")
        return nodes.mapNotNull { image ->
            val raw = listOf("data-src", "data-lazy-src", "data-original", "src")
                .firstNotNullOfOrNull { key -> image.attr(key).trim().takeIf(String::isNotEmpty) }
                ?: image.attr("srcset").split(',').lastOrNull()?.trim()?.substringBefore(' ')
            raw?.let { document.baseUri().resolveUrl(it) }
        }.filter { url ->
            url.startsWith("http") && promoMarkers.none { url.contains(it, true) }
        }.distinct()
    }

    fun nextChapter(document: Document, currentUrl: String, currentNumber: Double?): String? {
        val currentHost = URI(currentUrl).host
        val numbered = document.select("a[href]").mapNotNull { anchor ->
            val absolute = anchor.absUrl("href")
            if (absolute.isBlank() || runCatching { URI(absolute).host }.getOrNull() != currentHost) return@mapNotNull null
            val number = ChapterRouting.number("${anchor.text()} $absolute") ?: return@mapNotNull null
            if (currentNumber != null && (number <= currentNumber || (currentNumber % 1.0 == 0.0 && number != currentNumber + 1))) return@mapNotNull null
            number to absolute
        }.sortedBy { it.first }
        if (numbered.isNotEmpty()) return numbered.first().second
        return ChapterRouting.increment(currentUrl)
    }

    private suspend fun buildCbz(urls: List<String>, referer: String, attempts: Int, cookie: String?, onActivity: suspend (CaptureActivity) -> Unit): File {
        require(urls.size in 1..2000) { "Chapter exceeds safe page limits" }
        val work = File(context.cacheDir, "capture/${UUID.randomUUID()}").apply { mkdirs() }
        val pages = mutableListOf<File>()
        val hashes = mutableSetOf<String>()
        var totalBytes = 0L
        urls.forEach { url ->
            coroutineContext.ensureActive()
            val bytes = requestBytes(url, attempts, referer, if (URI(url).host == URI(referer).host) cookie else null)
            totalBytes += bytes.size
            require(totalBytes <= 1_000_000_000L && work.usableSpace > bytes.size * 2L + 20_000_000) { "Chapter is too large or storage is low" }
            val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)
            if (options.outWidth <= 0 || options.outHeight <= 0) throw CaptureFailure("A chapter page returned an invalid image.", true)
            val hash = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
            if (!hashes.add(hash)) throw CaptureFailure("The source returned duplicate chapter images.", true)
            val extension = when {
                bytes.size >= 8 && bytes.copyOfRange(0, 8).contentEquals(byteArrayOf(-119, 80, 78, 71, 13, 10, 26, 10)) -> "png"
                bytes.size >= 12 && String(bytes.copyOfRange(8, 12)) == "WEBP" -> "webp"
                bytes.size >= 3 && bytes[0] == 0x47.toByte() && bytes[1] == 0x49.toByte() -> "gif"
                else -> "jpg"
            }
            File(work, "%05d.%s".format(pages.size + 1, extension)).apply {
                writeBytes(bytes)
                pages += this
            }
            onActivity(CaptureActivity("Receiving pages", pages.size, urls.size, totalBytes))
        }
        if (pages.isEmpty()) throw CaptureFailure("The page URLs did not return valid images.")
        val cbz = File(work, "chapter-pages.cbz")
        onActivity(CaptureActivity("Packing chapter", pages.size, urls.size, totalBytes))
        ZipOutputStream(cbz.outputStream().buffered()).use { zip ->
            pages.forEach { page ->
                zip.putNextEntry(ZipEntry(page.name))
                page.inputStream().use { it.copyTo(zip) }
                zip.closeEntry()
            }
        }
        pages.forEach(File::delete)
        return cbz
    }

    private suspend fun requestText(url: String, attempts: Int, cookie: String?): String =
        String(requestBytes(url, attempts, null, cookie), Charsets.UTF_8)

    private suspend fun requestBytes(url: String, attempts: Int, referer: String?, cookie: String?): ByteArray {
        var last: Exception? = null
        repeat(attempts) { attempt ->
            coroutineContext.ensureActive()
            try {
                val connection = URL(url).openConnection() as HttpURLConnection
                try {
                connection.connectTimeout = 15_000
                connection.readTimeout = 30_000
                connection.instanceFollowRedirects = true
                connection.setRequestProperty("User-Agent", USER_AGENT)
                connection.setRequestProperty("Accept", "*/*")
                referer?.let { connection.setRequestProperty("Referer", it) }
                cookie?.takeIf(String::isNotBlank)?.let { connection.setRequestProperty("Cookie", it) }
                val code = connection.responseCode
                if (code == 401 || code == 403) throw CaptureFailure("The source requires browser access.", true)
                if (code !in 200..299) throw CaptureFailure("The source returned HTTP $code.", code == 429)
                    return connection.inputStream.use { input ->
                        val output = java.io.ByteArrayOutputStream()
                        val buffer = ByteArray(64 * 1024)
                        while (true) {
                            coroutineContext.ensureActive()
                            val n = input.read(buffer)
                            if (n < 0) break
                            require(output.size().toLong() + n <= 50_000_000) { "Source response exceeds safe limits" }
                            output.write(buffer, 0, n)
                        }
                        output.toByteArray()
                    }
                } finally { connection.disconnect() }
            } catch (error: CancellationException) { throw error
            } catch (error: Exception) {
                last = error
                if (attempt + 1 < attempts) delay(500L * (attempt + 1))
            }
        }
        throw last ?: CaptureFailure("Network request failed.")
    }

    private fun seriesTitle(title: String, url: String): String {
        val cleaned = title.replace(
            Regex("\\s*(?:[-|:]\\s*)?(?:chapter|chap|ch|episode|ep)\\s*[-:#]?\\s*\\d+(?:\\.\\d+)?.*$", RegexOption.IGNORE_CASE),
            "",
        ).trim()
        if (cleaned.length >= 2) return cleaned
        val path = URI(url).path.split('/').filter(String::isNotBlank)
        val slug = path.lastOrNull { ChapterRouting.number(it) == null } ?: "Series"
        return slug.replace('-', ' ').replace('_', ' ').split(' ').joinToString(" ") { word ->
            word.replaceFirstChar(Char::uppercase)
        }
    }

    private fun numberLabel(number: Double): String =
        if (number % 1.0 == 0.0) number.toInt().toString().padStart(2, '0') else number.toString()

    private fun canonical(url: String): String = url.substringBefore('#').replace(Regex("[?&](?:page|p|reader_page)=\\d+"), "")

    private fun String.resolveUrl(other: String): String = runCatching { URI(this).resolve(other).toString() }.getOrDefault(other)

    companion object {
        const val USER_AGENT = "Mozilla/5.0 (Linux; Android 10; Panelyra Tablet) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36"
    }
}
