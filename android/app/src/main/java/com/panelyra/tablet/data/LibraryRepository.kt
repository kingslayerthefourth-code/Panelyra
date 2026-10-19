package com.panelyra.tablet.data

import android.content.Context
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import androidx.core.content.FileProvider
import com.panelyra.tablet.download.CbzValidator
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.io.File
import java.text.Normalizer
import java.util.zip.ZipFile
import org.json.JSONObject
import org.json.JSONArray
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

class LibraryRepository(
    private val context: Context,
    val dao: PanelyraDao,
    private val fault: (String) -> Unit = {},
    private val availableBytes: (File) -> Long = { it.usableSpace },
) {
    private val validator = CbzValidator()

    fun libraryRoot(): File = File(context.getExternalFilesDir(null), "Panelyra").apply { mkdirs() }

    suspend fun <T> serialized(action: suspend () -> T): T = installLock.withLock { action() }

    suspend fun installCbz(
        temporary: File,
        seriesTitle: String,
        chapterLabel: String,
        title: String,
        sourceUrl: String,
        expectedSha256: String? = null,
        seriesKey: String? = null,
        legacySeriesKeys: List<String> = emptyList(),
    ): ChapterEntity = withContext(Dispatchers.IO) { installLock.withLock {
        val validation = validator.validate(temporary)
        require(expectedSha256 == null || expectedSha256.equals(validation.sha256, true)) {
            "The transferred chapter checksum did not match."
        }
        var seriesId = ChapterIdentity.series(seriesTitle, seriesKey, sourceUrl)
        val sourceIdentity = ChapterIdentity.sourceIdentity(sourceUrl)
        val aliases = if (sourceIdentity.isBlank()) emptySet() else dao.allChapters().filter {
            it.seriesId.startsWith("source-") && ChapterIdentity.sourceIdentity(it.sourceUrl) == sourceIdentity
        }.map { it.seriesId }.toSet()
        if (seriesKey == null && aliases.isNotEmpty()) seriesId = aliases.first()
        require(legacySeriesKeys.size <= 8 && legacySeriesKeys.all { it !in setOf(".","..") && Regex("[a-zA-Z0-9._-]+").matches(it) }) { "Invalid legacy source mapping" }
        // Only the authenticated Mac's explicit old storage IDs can bridge a
        // pre-foundation delivery. Blank-source external imports never alias.
        val provenLegacy = legacySeriesKeys.filter { key -> dao.chaptersForSeries(key).any {
            it.sourceUrl in setOf("mac-push","usb") || (sourceIdentity.isNotBlank() && ChapterIdentity.sourceIdentity(it.sourceUrl)==sourceIdentity)
        } }
        val remap = if (seriesKey != null) (aliases + provenLegacy).filter { it != seriesId }.distinct() else emptyList()
        val remappedChapters = remap.flatMap { dao.chaptersForSeries(it) }
        val chapter = ChapterIdentity.chapter(chapterLabel)
        val previous = dao.chaptersForSeries(seriesId).firstOrNull { ChapterIdentity.chapter(it.chapter) == chapter }
            ?: remappedChapters.filter { ChapterIdentity.chapter(it.chapter) == chapter }.maxByOrNull { it.lastReadAt }
        // Immutable versions keep the previous readable file intact through failure.
        val target = File(libraryRoot(), "$seriesId/$chapter/chapter-pages-${validation.sha256}.cbz")
        target.parentFile?.mkdirs()
        val partial = File(target.parentFile, ".chapter-pages.cbz.partial")
        check(availableBytes(target.parentFile!!) > temporary.length() + 10_000_000) { "Not enough tablet storage" }
        val intent = File(target.parentFile, "install-intent.json")
        val repairs = runCatching { JSONObject(intent.readText()).getJSONArray("sidecars") }.getOrDefault(JSONArray())
        for (old in remappedChapters) {
            val sidecar = File(File(old.cbzPath).parentFile, "metadata.json")
            val metadata = runCatching { JSONObject(sidecar.readText()) }.getOrDefault(JSONObject())
            repairs.put(JSONObject().put("path", sidecar.absolutePath).put("metadata", metadata
                .put("series_key", seriesId).put("series_title", seriesTitle).put("chapter", ChapterIdentity.chapter(old.chapter))
                .put("title", old.title).put("source_url", old.sourceUrl).put("sha256", old.sha256).put("file", File(old.cbzPath).name)))
        }
        val plan = JSONObject().put("series_key", seriesId).put("series_title", seriesTitle)
            .put("chapter", chapter).put("title", title).put("source_url", sourceUrl)
            .put("sha256", validation.sha256).put("file", target.name).put("sidecars", repairs)
            .put("legacy_series_keys", JSONArray(legacySeriesKeys))
        writeAtomic(intent, plan.toString())
        fault("intent")
        if (temporary.canonicalFile != target.canonicalFile) {
            temporary.inputStream().use { input -> java.io.FileOutputStream(partial).use { out -> input.copyTo(out); out.fd.sync() } }
            // A same-directory atomic rename never exposes a partly copied chapter.
            check(partial.renameTo(target)) { "Could not commit chapter file" }
        }
        fault("file")
        val entity = ChapterEntity(
            seriesId = seriesId,
            chapter = chapter,
            chapterNumber = chapterNumber(chapterLabel),
            title = title,
            cbzPath = target.absolutePath,
            sha256 = validation.sha256,
            pageCount = validation.entries.size,
            byteSize = target.length(),
            sourceUrl = sourceUrl,
            progressPanel = previous?.progressPanel ?: 0,
            scrollOffset = previous?.scrollOffset ?: 0,
            readingMode = previous?.readingMode ?: "fit",
            completed = previous?.completed ?: false,
            lastReadAt = previous?.lastReadAt ?: 0,
            downloadedAt = previous?.downloadedAt ?: System.currentTimeMillis(),
        )
        val previousSeries = dao.series(seriesId) ?: remap.firstNotNullOfOrNull { dao.series(it) }?.copy(id=seriesId)
        val coverPath = previousSeries?.coverPath?.takeIf { File(it).isFile }
            ?: runCatching { createChapterThumbnail(target, validation.entries.first(), seriesId) }.getOrNull()
        val metadata = JSONObject().put("series_key", seriesId).put("series_title", seriesTitle)
            .put("chapter", chapter).put("title", title).put("source_url", sourceUrl)
            .put("sha256", validation.sha256).put("page_count", entity.pageCount)
            .put("file_size", target.length()).put("file_mtime", target.lastModified()).put("file", target.name)
        writeAtomic(File(target.parentFile, "metadata.json"), metadata.toString())
        fault("metadata")
        dao.installMapped((previousSeries ?: SeriesEntity(seriesId, seriesTitle)).copy(
            title = seriesTitle, coverPath = coverPath, updatedAt = System.currentTimeMillis()), entity, remap)
        fault("database")
        for (i in 0 until repairs.length()) {
            val repair = repairs.getJSONObject(i)
            val sidecar = File(repair.getString("path"))
            require(sidecar.canonicalPath.startsWith(libraryRoot().canonicalPath + File.separator))
            writeAtomic(sidecar, repair.getJSONObject("metadata").toString())
        }
        intent.delete()
        if (previous != null && previous.chapter != chapter) dao.deleteChapterRow(seriesId, previous.chapter)
        entity
    } }

    suspend fun verified(seriesId: String, label: String, sha: String, size: Long): Boolean = withContext(Dispatchers.IO) {
        val chapter = dao.chapter(seriesId, ChapterIdentity.chapter(label)) ?: return@withContext false
        val file = File(chapter.cbzPath)
        if (!file.isFile || file.length() != size || chapter.sha256 != sha) return@withContext false
        val meta = runCatching { JSONObject(File(file.parentFile, "metadata.json").readText()) }.getOrNull()
        if (meta?.optString("sha256") == sha && meta.optLong("file_size") == size && meta.optLong("file_mtime") == file.lastModified()) true
        else runCatching { validator.validate(file).sha256 == sha }.getOrDefault(false)
    }

    suspend fun recoverOrphans(): List<String> = withContext(Dispatchers.IO) {
        LibraryCleanup(context, this@LibraryRepository).recoverInterrupted()
        val errors = mutableListOf<String>()
        for (directory in libraryRoot().listFiles().orEmpty().filter { it.isDirectory && it.name !in setOf("covers", "inbox") }) {
            for (chapterDir in directory.listFiles().orEmpty().filter(File::isDirectory)) {
                val saved = runCatching { JSONObject(File(chapterDir, "install-intent.json").takeIf { it.isFile }?.readText()
                    ?: File(chapterDir, "metadata.json").readText()) }.getOrNull()
                val filename = saved?.optString("file")?.takeIf { it.matches(Regex("chapter-pages(?:-[a-f0-9]{64})?\\.cbz")) } ?: "chapter-pages.cbz"
                val cbz = File(chapterDir, filename)
                if (!cbz.isFile) continue
                try {
                    val meta = saved
                    val key = meta?.optString("series_key")?.takeIf(String::isNotBlank) ?: directory.name
                    val label = meta?.optString("chapter")?.takeIf(String::isNotBlank) ?: chapterDir.name
                    val canonical = ChapterIdentity.chapter(label)
                    val old = dao.chapter(key, canonical)
                    if (old != null && File(old.cbzPath).isFile && !File(chapterDir, "install-intent.json").isFile) {
                        if (verified(key, canonical, old.sha256, old.byteSize)) continue
                        throw IllegalArgumentException("Stored chapter is damaged; retry its transfer")
                    }
                    installCbz(cbz, meta?.optString("series_title")?.takeIf(String::isNotBlank)
                        ?: dao.series(key)?.title ?: key.replace('-', ' '), label,
                        meta?.optString("title")?.takeIf(String::isNotBlank) ?: "Chapter $canonical",
                        meta?.optString("source_url").orEmpty(), meta?.optString("sha256")?.takeIf(String::isNotBlank), key,
                        meta?.optJSONArray("legacy_series_keys")?.let { list -> (0 until list.length()).map { list.getString(it) } }.orEmpty())
                } catch (error: Exception) { errors += "${directory.name} chapter ${chapterDir.name}: ${error.message}" }
            }
            runCatching {
                val meta = JSONObject(File(directory, "series.json").readText())
                val cover = File(meta.getString("cover"))
                require(cover.canonicalFile.parentFile == File(libraryRoot(), "covers").canonicalFile)
                if (cover.isFile && dao.series(meta.getString("id"))?.coverProvenance != "supplied")
                    installCover(meta.getString("id"), cover, meta.getString("cover_version"), meta.getString("cover_provenance"))
            }
        }
        errors
    }

    suspend fun installCover(seriesId: String, file: File, version: String, provenance: String = "supplied") = withContext(Dispatchers.IO) {
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeFile(file.absolutePath, bounds)
        require(bounds.outWidth in 1..10_000 && bounds.outHeight in 1..10_000) { "Invalid cover" }
        val actual = CbzValidator.sha256(file)
        require(version.isBlank() || version == actual) { "Cover checksum did not match" }
        installLock.withLock {
            val cover = File(libraryRoot(), "covers/$seriesId-$actual.cover")
            cover.parentFile!!.mkdirs()
            val partial = File(cover.parentFile, "${cover.name}.partial")
            file.copyTo(partial, overwrite = true)
            check(partial.renameTo(cover))
            val series = dao.series(seriesId) ?: return@withLock
            dao.putSeries(series.copy(coverPath = cover.absolutePath, coverProvenance = provenance, coverVersion = actual))
            writeAtomic(File(libraryRoot(), "$seriesId/series.json"), JSONObject().put("id", seriesId)
                .put("title", series.title).put("cover", cover.absolutePath).put("cover_version", actual)
                .put("cover_provenance", provenance).toString())
        }
    }

    private fun createChapterThumbnail(cbz: File, firstPage: String, seriesId: String): String {
        val cover = File(libraryRoot(), "covers/$seriesId.jpg")
        cover.parentFile?.mkdirs()
        ZipFile(cbz).use { zip ->
            val entry = requireNotNull(zip.getEntry(firstPage))
            val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
            zip.getInputStream(entry).use { BitmapFactory.decodeStream(it, null, bounds) }
            var sample = 1
            while (bounds.outWidth / sample > 600 || bounds.outHeight / sample > 900) sample *= 2
            val options = BitmapFactory.Options().apply { inSampleSize = sample }
            val bitmap = zip.getInputStream(entry).use { BitmapFactory.decodeStream(it, null, options) }
                ?: error("Could not make a chapter thumbnail")
            try {
                cover.outputStream().use { check(bitmap.compress(Bitmap.CompressFormat.JPEG, 82, it)) }
            } finally {
                bitmap.recycle()
            }
        }
        return cover.absolutePath
    }

    suspend fun repairMissingCovers() = withContext(Dispatchers.IO) {
        for (series in dao.allSeries()) {
            val existing = series.coverPath?.let { File(it) }
            if (existing?.isFile == true) {
                if (existing.extension.lowercase() != "cover") continue
                val signature = ByteArray(12)
                val signatureLength = existing.inputStream().use { it.read(signature) }
                val extension = when {
                    signatureLength >= 2 && signature[0] == 0xff.toByte() && signature[1] == 0xd8.toByte() -> "jpg"
                    signatureLength >= 4 && signature.copyOfRange(0, 4).contentEquals(byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47)) -> "png"
                    signatureLength >= 4 && String(signature, 0, 4) == "RIFF" -> "webp"
                    else -> null
                }
                if (extension != null) {
                    val cover = File(libraryRoot(), "covers/${series.id}.$extension")
                    existing.copyTo(cover, overwrite = true)
                    dao.updateCover(series.id, cover.absolutePath)
                    continue
                }
            }
            val chapter = dao.chaptersForSeries(series.id).firstOrNull() ?: continue
            val cbz = File(chapter.cbzPath)
            if (!cbz.isFile) continue
            runCatching {
                val firstPage = ZipFile(cbz).use { zip ->
                    zip.entries().asSequence()
                        .filterNot { it.isDirectory }
                        .map { it.name }
                        .filter { it.substringAfterLast('.', "").lowercase() in setOf("jpg", "jpeg", "png", "webp", "gif", "avif") }
                        .sortedWith(CbzValidator.pageOrder)
                        .firstOrNull()
                } ?: return@runCatching
                val path = createChapterThumbnail(cbz, firstPage, series.id)
                dao.updateCover(series.id, path)
            }
        }
    }

    suspend fun pages(chapter: ChapterEntity): List<File> = withContext(Dispatchers.IO) {
        val cache = File(context.cacheDir, "cbz/${chapter.sha256}")
        val pages = validator.extract(File(chapter.cbzPath), cache)
        var bytes = File(context.cacheDir, "cbz").walkTopDown().filter(File::isFile).sumOf(File::length)
        File(context.cacheDir, "cbz").listFiles().orEmpty().filter { it.isDirectory && it != cache }
            .sortedBy { File(it, "pages.json").lastModified() }.forEach { candidate ->
                if (bytes > 500_000_000L) { bytes -= candidate.walkTopDown().filter(File::isFile).sumOf(File::length); candidate.deleteRecursively() }
            }
        pages
    }

    suspend fun delete(chapter: ChapterEntity) = withContext(Dispatchers.IO) {
        File(chapter.cbzPath).parentFile?.deleteRecursively()
        File(context.cacheDir, "cbz/${chapter.sha256}").deleteRecursively()
        dao.deleteChapterRow(chapter.seriesId, chapter.chapter)
        dao.pruneSeries(chapter.seriesId)
    }

    fun shareIntent(chapter: ChapterEntity): Intent {
        val file = File(chapter.cbzPath)
        val uri = FileProvider.getUriForFile(context, "${context.packageName}.files", file)
        return Intent(Intent.ACTION_SEND).apply {
            type = "application/vnd.comicbook+zip"
            putExtra(Intent.EXTRA_STREAM, uri)
            addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        }
    }

    companion object {
        private val installLock = Mutex()
        private fun writeAtomic(target: File, text: String) {
            val temporary = File(target.parentFile, "${target.name}.partial")
            java.io.FileOutputStream(temporary).use { it.write(text.toByteArray()); it.fd.sync() }
            check(temporary.renameTo(target))
        }
        fun chapterNumber(value: String): Double? = Regex(
            "(?:chapter|chap|ch|episode|ep)?[-_\\s.:]*(\\d+(?:\\.\\d+)?)$",
            RegexOption.IGNORE_CASE,
        ).find(value.trim())?.groupValues?.getOrNull(1)?.toDoubleOrNull()

        fun slug(value: String): String {
            val ascii = Normalizer.normalize(value, Normalizer.Form.NFD).replace(Regex("\\p{M}+"), "")
            return ascii.lowercase().replace(Regex("[^a-z0-9]+"), "-").trim('-').ifEmpty { "series" }
        }
    }
}
