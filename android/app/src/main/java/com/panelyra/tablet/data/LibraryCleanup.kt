package com.panelyra.tablet.data

import android.content.Context
import com.panelyra.tablet.download.CbzValidator
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.UUID

data class CleanupState(val ready: Boolean = false, val done: Boolean = false, val busy: Boolean = false,
    val message: String = "Checking replacement chapters…", val restoreAvailable: Boolean = false)

/** Explicit cleanup only; the replacement versions must exist before anything is removed. */
class LibraryCleanup(private val context: Context, private val library: LibraryRepository,
    private val fault: (String) -> Unit = {}) {
    private val dao = library.dao
    private val prefs = context.getSharedPreferences("library_cleanup", Context.MODE_PRIVATE)
    private val trash = File(library.libraryRoot().parentFile, "PanelyraTrash").apply { mkdirs() }
    fun plan(): JSONObject = try {
        context.assets.open("library-cleanup.json").bufferedReader().use { JSONObject(it.readText()) }
    } catch (_: java.io.FileNotFoundException) {
        JSONObject().put("id", "no-cleanup-configured").put("chapters", JSONArray())
    }
    private fun expected(plan: JSONObject) = plan.getJSONArray("chapters").let { list -> (0 until list.length()).map(list::getJSONObject) }

    suspend fun inspect(plan: JSONObject = plan()): CleanupState = withContext(Dispatchers.IO) {
        if (expected(plan).isEmpty()) return@withContext CleanupState(message = "No library cleanup is configured.")
        if (prefs.getString("done", "") == plan.getString("id")) return@withContext CleanupState(done = true,
            message = "Only the two current books remain in Reader. Older files are in recoverable Trash.", restoreAvailable = prefs.contains("journal"))
        val chapters = expected(plan)
        val received = chapters.count { c -> library.verified(c.getString("series_key"), c.getString("chapter"), c.getString("sha256"), c.getLong("size")) }
        val covers = chapters.distinctBy { it.getString("series_key") }.all { c -> dao.series(c.getString("series_key"))?.let {
            it.coverVersion == c.getString("cover_version") && it.coverPath?.let(::File)?.isFile == true
        } == true }
        CleanupState(ready = received == chapters.size && covers,
            message = if (received == chapters.size && covers) "All ${chapters.size} replacement chapters and both covers are verified. Ready to remove the other books and old versions."
                else "$received / ${chapters.size} replacement chapters ready. Cleanup waits for both complete books and covers.")
    }

    suspend fun execute(plan: JSONObject = plan()): CleanupState = withContext(Dispatchers.IO) { library.serialized {
        check(expected(plan).isNotEmpty()) { "No library cleanup is configured." }
        check(inspect(plan).ready) { "Wait until both replacement books and covers have finished receiving." }
        // Recheck actual file contents before retiring the old readable copies.
        for (c in expected(plan)) {
            val chapter = dao.chapter(c.getString("series_key"), ChapterIdentity.chapter(c.getString("chapter")))!!
            check(CbzValidator.sha256(File(chapter.cbzPath)) == c.getString("sha256")) { "Replacement chapter ${chapter.chapter} needs to be transferred again." }
        }
        for (c in expected(plan).distinctBy { it.getString("series_key") }) {
            val cover = dao.series(c.getString("series_key"))?.coverPath?.let(::File) ?: error("Replacement cover is missing")
            check(CbzValidator.sha256(cover) == c.getString("cover_version")) { "A replacement cover needs to be sent again." }
        }
        val keep = expected(plan).map { it.getString("series_key") }.toSet()
        val allSeries = dao.allSeries(); val allChapters = dao.allChapters()
        val retiredSeries = allSeries.filter { it.id !in keep }
        val retiredChapters = allChapters.filter { it.seriesId !in keep }
        val root = library.libraryRoot().canonicalFile
        val protected = (allChapters.filter { it.seriesId in keep }.map { File(it.cbzPath).canonicalFile } +
            allSeries.filter { it.id in keep }.mapNotNull { it.coverPath?.let(::File)?.canonicalFile }).toSet()
        val protectedParents = protected.mapNotNull { it.parentFile }.toSet()
        val moves = mutableSetOf<File>()
        for (directory in root.listFiles().orEmpty().filter { it.isDirectory && it.name !in setOf("covers", "inbox") }) {
            if (directory.name !in keep && protected.none { it.toPath().startsWith(directory.canonicalFile.toPath()) }) moves += directory
            else for (file in directory.walkTopDown().filter(File::isFile)) {
                if (file.extension.equals("cbz", true) && file.canonicalFile !in protected) moves += file
                else if (directory.name !in keep && file.canonicalFile !in protected && file.parentFile?.canonicalFile !in protectedParents) moves += file
            }
        }
        for (cover in File(root, "covers").listFiles().orEmpty().filter(File::isFile)) if (cover.canonicalFile !in protected) moves += cover
        val operation = File(trash, UUID.randomUUID().toString()).apply { mkdirs() }
        val journal = File(operation, "journal.json")
        val entries = JSONArray()
        for (file in moves) {
            val relative = file.canonicalFile.relativeTo(root).path
            entries.put(JSONObject().put("source", relative).put("trash", "files/$relative"))
        }
        val record = JSONObject().put("state", "planned").put("plan_id", plan.getString("id"))
            .put("moves", entries).put("series", JSONArray(retiredSeries.map(::seriesJson))).put("chapters", JSONArray(retiredChapters.map(::chapterJson)))
        atomic(journal, record)
        try {
            for (i in 0 until entries.length()) {
                val move = entries.getJSONObject(i); val source = safe(root, move.getString("source")); val destination = safe(operation, move.getString("trash"))
                destination.parentFile!!.mkdirs()
                check(source.renameTo(destination)) { "Could not move an old file to Trash. Nothing was permanently deleted." }
            }
            fault("files")
            dao.deleteSeries(retiredSeries.map { it.id })
            fault("database")
            record.put("state", "committed"); atomic(journal, record)
            check(prefs.edit().putString("done", plan.getString("id")).putString("journal", journal.absolutePath).commit())
        } catch (error: Exception) { restore(journal); throw error }
        inspect(plan)
    } }

    suspend fun undo() = withContext(Dispatchers.IO) { library.serialized {
        val path = prefs.getString("journal", null) ?: error("No cleanup to undo")
        val journal = File(path).canonicalFile
        require(journal.toPath().startsWith(trash.canonicalFile.toPath()))
        restore(journal)
        prefs.edit().remove("done").remove("journal").commit()
    } }

    suspend fun recoverInterrupted() = withContext(Dispatchers.IO) { library.serialized {
        for (directory in trash.listFiles().orEmpty().filter(File::isDirectory)) {
            val journal = File(directory, "journal.json")
            if (journal.isFile) {
                val record = JSONObject(journal.readText())
                if (record.getString("state") == "planned") restore(journal)
                else if (record.getString("state") == "committed" && !prefs.contains("journal"))
                    prefs.edit().putString("done", record.getString("plan_id")).putString("journal", journal.absolutePath).commit()
            }
        }
    } }

    private suspend fun restore(journal: File) {
        val record = JSONObject(journal.readText()); val entries = record.getJSONArray("moves")
        for (i in 0 until entries.length()) {
            val move = entries.getJSONObject(i); val source = safe(library.libraryRoot(), move.getString("source")); val saved = safe(journal.parentFile!!, move.getString("trash"))
            if (saved.exists()) { check(!source.exists()) { "A newer file occupies this location; Trash was retained." }; source.parentFile!!.mkdirs(); check(saved.renameTo(source)) }
        }
        val series = record.getJSONArray("series")
        for (i in 0 until series.length()) { val s = series.getJSONObject(i); dao.putSeries(SeriesEntity(s.getString("id"), s.getString("title"), s.optString("cover").takeIf(String::isNotBlank), s.getLong("updated"), s.getString("provenance"), s.getString("version"))) }
        val chapters = record.getJSONArray("chapters")
        for (i in 0 until chapters.length()) { val c = chapters.getJSONObject(i)
            dao.putChapter(ChapterEntity(c.getString("series"), c.getString("chapter"), if (c.isNull("number")) null else c.getDouble("number"),
                c.getString("title"), c.getString("file"), c.getString("sha"), c.getInt("pages"), c.getLong("size"), c.getString("source"),
                c.getInt("panel"), c.getInt("offset"), c.getString("mode"), c.getBoolean("completed"), c.getLong("downloaded"), c.getLong("read")))
        }
        record.put("state", "restored"); atomic(journal, record)
    }

    private fun safe(root: File, relative: String): File = File(root, relative).canonicalFile.also {
        require(it != root.canonicalFile && it.toPath().startsWith(root.canonicalFile.toPath())) { "Invalid cleanup path" }
    }
    private fun atomic(file: File, value: JSONObject) { val tmp = File(file.parentFile, "${file.name}.partial"); java.io.FileOutputStream(tmp).use { it.write(value.toString().toByteArray()); it.fd.sync() }; check(tmp.renameTo(file)) }
    private fun seriesJson(s: SeriesEntity) = JSONObject().put("id", s.id).put("title", s.title).put("cover", s.coverPath.orEmpty()).put("updated", s.updatedAt).put("provenance", s.coverProvenance).put("version", s.coverVersion)
    private fun chapterJson(c: ChapterEntity) = JSONObject().put("series", c.seriesId).put("chapter", c.chapter).put("number", c.chapterNumber ?: JSONObject.NULL)
        .put("title", c.title).put("file", c.cbzPath).put("sha", c.sha256).put("pages", c.pageCount).put("size", c.byteSize).put("source", c.sourceUrl)
        .put("panel", c.progressPanel).put("offset", c.scrollOffset).put("mode", c.readingMode).put("completed", c.completed).put("downloaded", c.downloadedAt).put("read", c.lastReadAt)
}
