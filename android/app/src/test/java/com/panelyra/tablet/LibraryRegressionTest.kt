package com.panelyra.tablet

import android.content.Context
import android.graphics.Bitmap
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import androidx.sqlite.db.framework.FrameworkSQLiteOpenHelperFactory
import androidx.sqlite.db.SupportSQLiteOpenHelper
import com.panelyra.tablet.data.*
import com.panelyra.tablet.download.CbzValidator
import kotlinx.coroutines.runBlocking
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
class LibraryRegressionTest {
    @Test fun assistedResumeRetainsSavedCountAndNeverFallsBackToStartWithAnUnknownRoute() {
        val job = DownloadJobEntity("resume", "https://fixture/chapter-1", 5, "reliable", "interrupted",
                                    currentChapter = 3, resumeUrl = "https://fixture/chapter-4")
        val resumed = DownloadCheckpoint.beginAssisted(job)
        assertEquals(3, resumed.currentChapter)
        assertEquals("https://fixture/chapter-4", DownloadCheckpoint.resumeUrl(resumed))
        try {
            DownloadCheckpoint.resumeUrl(resumed.copy(resumeUrl = ""))
            fail("Unknown next chapter restarted at chapter one")
        } catch (_: IllegalArgumentException) { }
    }
    @Test fun companionResponsesAreBoundedBeforeAllocationGrows() {
        val client = com.panelyra.tablet.network.CompanionClient
        assertEquals("ok", client.readBounded(java.io.ByteArrayInputStream("ok".toByteArray()), 2))
        try {
            client.readBounded(java.io.ByteArrayInputStream(ByteArray(65_537)), 65_536)
            fail("Oversized response accepted")
        } catch (_: IllegalArgumentException) { }
    }
    private lateinit var context: Context
    private lateinit var db: PanelyraDatabase
    private lateinit var library: LibraryRepository
    private lateinit var cbz: File
    @Before fun setup() {
        context = ApplicationProvider.getApplicationContext()
        File(context.getExternalFilesDir(null), "Panelyra").deleteRecursively()
        db = Room.inMemoryDatabaseBuilder(context, PanelyraDatabase::class.java).allowMainThreadQueries().build()
        library = LibraryRepository(context, db.dao())
        cbz = File(context.cacheDir, "fixture.cbz")
        val image = java.io.ByteArrayOutputStream()
        Bitmap.createBitmap(8, 8, Bitmap.Config.ARGB_8888).apply { eraseColor(android.graphics.Color.MAGENTA); compress(Bitmap.CompressFormat.PNG, 100, image); recycle() }
        ZipOutputStream(cbz.outputStream()).use { zip ->
            for (name in listOf("page10.png", "page2.png", "page1.png")) {
                zip.putNextEntry(ZipEntry(name)); zip.write(image.toByteArray()); zip.closeEntry()
            }
        }
    }
    @After fun teardown() { db.close() }

    @Test fun sequentialRepeatedImportsAndCoverUpdatesRetainAllFiveChapters() = runBlocking {
        for (chapter in 1..5) library.installCbz(cbz, "Book", "0$chapter", "Chapter $chapter", "", seriesKey = "book")
        assertEquals(listOf("1", "2", "3", "4", "5"), db.dao().chaptersForSeries("book").map { it.chapter })
        assertEquals(1, db.dao().allChapters().map { it.sha256 }.distinct().size)
        db.dao().savePosition("book", "1", 2, 31, "continuous")
        db.dao().setCompleted("book", "1", true)
        db.dao().savePosition("book", "1", 1, 5, "fit")
        library.installCbz(cbz, "Book updated", "01", "Chapter 1", "", seriesKey = "book")
        db.dao().putSeries(db.dao().series("book")!!.copy(coverPath = "cover", coverProvenance = "supplied"))
        assertEquals(5, db.dao().allChapters().size)
        val one = db.dao().chapter("book", "1")!!
        assertTrue(one.completed); assertEquals(1, one.progressPanel); assertEquals(5, one.scrollOffset)
        assertEquals(0, db.dao().chapter("book", "2")!!.progressPanel)
    }

    @Test fun numericalOrderCacheReuseOrphanRecoveryAndCorruption() = runBlocking {
        val validator = CbzValidator()
        assertEquals(listOf("page1.png", "page2.png", "page10.png"), validator.validate(cbz).entries)
        val chapter = library.installCbz(cbz, "Book", "1", "Chapter 1", "", seriesKey = "book")
        val started = System.nanoTime(); val first = library.pages(chapter)
        val firstTime = System.nanoTime() - started
        val mtime = first.map { it.lastModified() }
        val reopened = System.nanoTime(); val second = library.pages(chapter)
        println("Cache first=${firstTime / 1e6}ms reopen=${(System.nanoTime() - reopened) / 1e6}ms")
        assertEquals(first, second); assertEquals(mtime, second.map { it.lastModified() })
        db.dao().deleteChapterRow("book", "1")
        assertTrue(library.recoverOrphans().isEmpty())
        assertNotNull(db.dao().chapter("book", "1"))
        val corrupt = File(library.libraryRoot(), "broken/2/chapter-pages.cbz")
        corrupt.parentFile!!.mkdirs(); corrupt.writeText("not a zip")
        assertTrue(library.recoverOrphans().any { it.contains("broken chapter 2") })
        assertNull(db.dao().chapter("broken", "2"))
        File(context.cacheDir, "cbz").deleteRecursively()
        assertTrue(File(chapter.cbzPath).isFile)
    }

    @Test fun restartPreservesLibraryAndProgress() = runBlocking {
        val name = "restart-regression.db"
        context.deleteDatabase(name)
        db.close()
        db = Room.databaseBuilder(context, PanelyraDatabase::class.java, name).allowMainThreadQueries().build()
        library = LibraryRepository(context, db.dao())
        for (chapter in 1..5) library.installCbz(cbz, "Book", "$chapter", "Chapter $chapter", "", seriesKey = "book")
        db.dao().savePosition("book", "2", 2, 15, "single")
        db.close()
        db = Room.databaseBuilder(context, PanelyraDatabase::class.java, name).allowMainThreadQueries().build()
        assertEquals(5, db.dao().allChapters().size)
        assertEquals(2, db.dao().chapter("book", "2")!!.progressPanel)
    }

    @Test fun sourceIdentityNotTitleDeterminesAliases() {
        assertNotEquals(ChapterIdentity.sourceIdentity("https://comic.naver.com/webtoon/detail?titleId=845837&no=23"), ChapterIdentity.sourceIdentity("https://comic.naver.com/webtoon/detail?titleId=851610&no=1"))
        assertEquals(ChapterIdentity.sourceIdentity("https://comic.naver.com/webtoon/detail?titleId=845837&no=23"), ChapterIdentity.sourceIdentity("https://comic.naver.com/webtoon/list?titleId=845837"))
        assertEquals(ChapterIdentity.series("Alias", null, "https://a.test/book-chapter-1/"), ChapterIdentity.series("Other title", null, "https://a.test/book-chapter-2/"))
        assertNotEquals(ChapterIdentity.series("Same title", null, "https://a.test/book-chapter-1/"), ChapterIdentity.series("Same title", null, "https://b.test/book-chapter-1/"))
        assertEquals("1", ChapterIdentity.chapter("Chapter 01"))
        assertEquals("1.5", ChapterIdentity.chapter("01.50"))
    }

    @Test fun interruptedCommitRecoversWithoutDeletingOtherChapters() = runBlocking {
        for (stage in listOf("file", "metadata", "database")) {
            val key = "fault-$stage"
            library.installCbz(cbz, "Book", "1", "Chapter 1", "", seriesKey = key)
            val broken = LibraryRepository(context, db.dao(), fault = { if (it == stage) error("Simulated crash") })
            try { broken.installCbz(cbz, "Book", "2", "Chapter 2", "", seriesKey = key); fail("Fault not reached") }
            catch (expected: IllegalStateException) { assertTrue(expected.message!!.contains("Simulated")) }
            assertTrue(library.recoverOrphans().isEmpty())
            assertNotNull(db.dao().chapter(key, "1")); assertNotNull(db.dao().chapter(key, "2"))
        }
    }

    @Test fun lowStorageDoesNotPublishAndCaseDuplicatePathsAreRejected() = runBlocking {
        val noStorage = LibraryRepository(context, db.dao()) { 0 }
        try { noStorage.installCbz(cbz, "Book", "1", "Chapter 1", "", seriesKey = "book"); fail("Low storage published a chapter") }
        catch (expected: IllegalStateException) { assertTrue(expected.message!!.contains("storage")) }
        assertTrue(db.dao().allChapters().isEmpty())
        val duplicate = File(context.cacheDir, "duplicate.cbz")
        val bytes = java.util.zip.ZipFile(cbz).use { zip -> zip.getInputStream(zip.getEntry("page1.png")).readBytes() }
        ZipOutputStream(duplicate.outputStream()).use { zip ->
            for (name in listOf("page.png", "PAGE.png")) { zip.putNextEntry(ZipEntry(name)); zip.write(bytes); zip.closeEntry() }
        }
        try { CbzValidator().validate(duplicate); fail("Duplicate archive path was accepted") }
        catch (expected: IllegalArgumentException) { assertTrue(expected.message!!.contains("Duplicate")) }
    }

    @Test fun manifestsUnifyOnlyProvenSourceAliasesAndKeepProgress() = runBlocking {
        val one = library.installCbz(cbz, "Old title", "01", "Chapter 1", "https://a.test/book-chapter-1/")
        db.dao().savePosition(one.seriesId, "1", 2, 15, "single")
        library.installCbz(cbz, "Canonical title", "2", "Chapter 2", "https://a.test/book-chapter-2/", seriesKey = "canonical")
        assertEquals(2, db.dao().chaptersForSeries("canonical").size)
        assertEquals(2, db.dao().chapter("canonical", "1")!!.progressPanel)
        library.installCbz(cbz, "Canonical title", "1", "Chapter 1", "https://other.test/book-chapter-1/", seriesKey = "unrelated")
        assertEquals(1, db.dao().chaptersForSeries("unrelated").size)
        assertEquals(2, db.dao().chaptersForSeries("canonical").size)
    }

    @Test fun trustedLegacyDeliveryMappingPreservesProgressWithoutMergingExternalBooks() = runBlocking {
        library.installCbz(cbz,"Book","1","Chapter 1","mac-push",seriesKey="old-mac-id")
        db.dao().savePosition("old-mac-id","1",2,15,"single")
        library.installCbz(cbz,"Book","2","Chapter 2","https://a.test/book/chapter-2",seriesKey="canonical",legacySeriesKeys=listOf("old-mac-id"))
        assertEquals(2,db.dao().chaptersForSeries("canonical").size)
        assertEquals(15,db.dao().chapter("canonical","1")!!.scrollOffset)
        library.installCbz(cbz,"Same title","1","Chapter 1","",seriesKey="external")
        library.installCbz(cbz,"Same title","3","Chapter 3","https://a.test/book/chapter-3",seriesKey="canonical",legacySeriesKeys=listOf("external"))
        assertNotNull(db.dao().chapter("external","1"))
    }

    @Test fun migrationPreservesExistingRowsAndRemovesUniqueChecksum() = runBlocking {
        val name = "migration-regression.db"
        context.deleteDatabase(name)
        val helper = FrameworkSQLiteOpenHelperFactory().create(SupportSQLiteOpenHelper.Configuration.builder(context).name(name)
            .callback(object : SupportSQLiteOpenHelper.Callback(1) {
                override fun onCreate(sql: androidx.sqlite.db.SupportSQLiteDatabase) {
                    sql.execSQL("CREATE TABLE series (id TEXT NOT NULL PRIMARY KEY, title TEXT NOT NULL, coverPath TEXT, updatedAt INTEGER NOT NULL)")
                    sql.execSQL("CREATE TABLE chapters (seriesId TEXT NOT NULL, chapter TEXT NOT NULL, chapterNumber REAL, title TEXT NOT NULL, cbzPath TEXT NOT NULL, sha256 TEXT NOT NULL, pageCount INTEGER NOT NULL, byteSize INTEGER NOT NULL, sourceUrl TEXT NOT NULL, progressPanel INTEGER NOT NULL, scrollOffset INTEGER NOT NULL, readingMode TEXT NOT NULL, completed INTEGER NOT NULL, downloadedAt INTEGER NOT NULL, lastReadAt INTEGER NOT NULL, PRIMARY KEY(seriesId,chapter), FOREIGN KEY(seriesId) REFERENCES series(id) ON UPDATE NO ACTION ON DELETE CASCADE)")
                    sql.execSQL("CREATE INDEX index_chapters_seriesId ON chapters(seriesId)")
                    sql.execSQL("CREATE UNIQUE INDEX index_chapters_sha256 ON chapters(sha256)")
                    sql.execSQL("CREATE TABLE download_jobs (id TEXT NOT NULL PRIMARY KEY, startUrl TEXT NOT NULL, chapterCount INTEGER NOT NULL, performance TEXT NOT NULL, status TEXT NOT NULL, engine TEXT NOT NULL, currentChapter INTEGER NOT NULL, remoteJobId TEXT, error TEXT NOT NULL, createdAt INTEGER NOT NULL, updatedAt INTEGER NOT NULL)")
                    sql.execSQL("CREATE INDEX index_download_jobs_status ON download_jobs(status)")
                    sql.execSQL("INSERT INTO series VALUES ('book','Book',NULL,1)")
                    sql.execSQL("INSERT INTO chapters VALUES ('book','5',5,'Chapter 5','cbz','same',3,100,'',2,4,'fit',1,1,1)")
                }
                override fun onUpgrade(sql: androidx.sqlite.db.SupportSQLiteDatabase, old: Int, new: Int) = Unit
            }).build())
        helper.writableDatabase.close(); helper.close()
        val migrated = Room.databaseBuilder(context, PanelyraDatabase::class.java, name).addMigrations(PanelyraDatabase.MIGRATION_1_2).allowMainThreadQueries().build()
        val five = migrated.dao().chapter("book", "5")!!
        assertTrue(five.completed); assertEquals(2, five.progressPanel)
        migrated.dao().putChapter(five.copy(chapter = "1", chapterNumber = 1.0))
        assertEquals(2, migrated.dao().allChapters().size)
        migrated.close()
    }
}
