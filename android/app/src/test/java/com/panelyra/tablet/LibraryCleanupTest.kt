package com.panelyra.tablet

import android.content.Context
import android.graphics.Bitmap
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import com.panelyra.tablet.data.*
import com.panelyra.tablet.download.CbzValidator
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject
import org.junit.*
import org.junit.Assert.*
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28], application = android.app.Application::class)
class LibraryCleanupTest {
    private lateinit var context: Context
    private lateinit var db: PanelyraDatabase
    private lateinit var library: LibraryRepository
    private lateinit var archive: File
    private lateinit var cover: File
    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        File(context.getExternalFilesDir(null), "Panelyra").deleteRecursively()
        File(context.getExternalFilesDir(null), "PanelyraTrash").deleteRecursively()
        context.getSharedPreferences("library_cleanup", Context.MODE_PRIVATE).edit().clear().commit()
        db = Room.inMemoryDatabaseBuilder(context, PanelyraDatabase::class.java).allowMainThreadQueries().build()
        library = LibraryRepository(context, db.dao())
        cover = File(context.cacheDir, "cleanup-cover.png")
        cover.outputStream().use { Bitmap.createBitmap(8, 8, Bitmap.Config.ARGB_8888).apply { eraseColor(android.graphics.Color.BLUE); compress(Bitmap.CompressFormat.PNG, 100, it); recycle() } }
        archive = File(context.cacheDir, "cleanup.cbz")
        ZipOutputStream(archive.outputStream()).use { it.putNextEntry(ZipEntry("0001.png")); it.write(cover.readBytes()); it.closeEntry() }
    }
    @After fun close() { db.close() }
    @Test fun emptyPlanCannotRemoveBooks() = runBlocking {
        val cleanup = LibraryCleanup(context, library)
        val empty = JSONObject().put("id", "none").put("chapters", JSONArray())
        assertFalse(cleanup.inspect(empty).ready)
        try {
            cleanup.execute(empty)
            fail("An empty cleanup plan must never execute")
        } catch (_: IllegalStateException) { }
    }
    private suspend fun install(key: String): ChapterEntity = library.installCbz(archive, key, "1", "Chapter 1", "", seriesKey = key).also {
        library.installCover(key, cover, CbzValidator.sha256(cover))
    }
    private suspend fun plan(rows: List<ChapterEntity>) = JSONObject().put("id", "test-current-books").put("chapters", JSONArray(rows.map { c ->
        JSONObject().put("series_key", c.seriesId).put("chapter", c.chapter).put("size", c.byteSize).put("sha256", c.sha256).put("cover_version", db.dao().series(c.seriesId)!!.coverVersion)
    }))
    @Test fun missingReplacementBlocksCleanupBeforeAnyMutation() = runBlocking {
        val current = install("current"); install("old")
        val p = plan(listOf(current)); p.getJSONArray("chapters").getJSONObject(0).put("sha256", "f".repeat(64))
        val cleanup = LibraryCleanup(context, library)
        assertFalse(cleanup.inspect(p).ready)
        try { cleanup.execute(p); fail("Missing replacement allowed deletion") } catch (_: IllegalStateException) { }
        assertEquals(2, db.dao().allChapters().size)
    }
    @Test fun cleanupKeepsBothCurrentBooksRemovesOldVersionsAndCanUndo() = runBlocking {
        val one = install("keep-one"); val two = install("keep-two"); val old = install("older-book")
        db.dao().savePosition(old.seriesId, old.chapter, 0, 42, "comfort")
        val obsolete = File(File(one.cbzPath).parentFile, "chapter-pages-${"f".repeat(64)}.cbz").apply { writeBytes(archive.readBytes()) }
        val cleanup = LibraryCleanup(context, library)
        assertTrue(cleanup.execute(plan(listOf(one, two))).done)
        assertEquals(setOf(one.seriesId, two.seriesId), db.dao().allSeries().map { it.id }.toSet())
        assertTrue(File(one.cbzPath).isFile); assertTrue(File(two.cbzPath).isFile)
        assertFalse(obsolete.exists()); assertFalse(File(old.cbzPath).exists())
        library.recoverOrphans()
        assertEquals(2, db.dao().allChapters().size)
        cleanup.undo()
        assertEquals(3, db.dao().allChapters().size)
        assertEquals(42, db.dao().chapter(old.seriesId, old.chapter)!!.scrollOffset)
        assertTrue(obsolete.isFile)
    }
    @Test fun interruptedCleanupRestoresFilesDatabaseAndProgress() = runBlocking {
        val current = install("current"); val old = install("old")
        db.dao().setCompleted(old.seriesId, old.chapter, true)
        val cleanup = LibraryCleanup(context, library) { if (it == "database") throw AssertionError("simulated process death") }
        try { cleanup.execute(plan(listOf(current))); fail("Fault was not injected") } catch (_: AssertionError) { }
        LibraryCleanup(context, library).recoverInterrupted()
        assertTrue(File(current.cbzPath).isFile); assertTrue(File(old.cbzPath).isFile)
        assertTrue(db.dao().chapter(old.seriesId, old.chapter)!!.completed)
    }
}
