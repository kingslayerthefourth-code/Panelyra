package com.panelyra.tablet

import com.panelyra.tablet.data.*
import com.panelyra.tablet.ui.LibraryPresentation
import com.panelyra.tablet.update.AppUpdates
import org.junit.Assert.*
import org.junit.Test

class LibraryPresentationTest {
    private fun chapter(number: String) = ChapterEntity("book", number, number.toDouble(), "Chapter $number", "file", "sha", 10, 100, "source")
    @Test fun numericOrderingFiltersAndResumeNeverDropChapters() {
        val rows = listOf(chapter("10"), chapter("2"), chapter("1").copy(lastReadAt = 3, progressPanel = 4), chapter("3").copy(completed = true, lastReadAt = 5))
        assertEquals(listOf("1", "2", "3", "10"), LibraryPresentation.chapters(rows, newest = false).map { it.chapter })
        assertEquals("1", LibraryPresentation.start(rows)?.chapter)
        assertEquals(listOf("1"), LibraryPresentation.chapters(rows, filter = "In progress").map { it.chapter })
        assertEquals(3, LibraryPresentation.chapters(rows, filter = "Unread").size)
        assertEquals(4, rows.size)
        assertEquals(0f, LibraryPresentation.progress(chapter("1")))
        assertEquals(.5f, LibraryPresentation.progress(rows[2]))
    }
    @Test fun shelfMatchesMacAlphabeticalOrderAndTrimsSearch() {
        val rows = listOf(SeriesSummary("b", "Rebirth", null, 38, 38, 38.0, 0), SeriesSummary("a", "Assassin", null, 20, 0, 20.0, 0))
        assertEquals(listOf("a", "b"), LibraryPresentation.books(rows).map { it.id })
        assertEquals(listOf("b"), LibraryPresentation.books(rows, "  REB  ").map { it.id })
        assertEquals(listOf("b"), LibraryPresentation.books(rows, unreadOnly = true).map { it.id })
    }
    @Test fun appUpdatesRejectWrongPackagesSignersAndDowngrades() {
        AppUpdates.validateIdentity("com.panelyra.tablet", "com.panelyra.tablet", 17, 16, setOf("same"), setOf("same"))
        for ((pkg, version, signatures) in listOf(Triple("other.app", 17L, setOf("same")), Triple("com.panelyra.tablet", 16L, setOf("same")), Triple("com.panelyra.tablet", 17L, setOf("wrong")), Triple("com.panelyra.tablet", 17L, emptySet()))) {
            try { AppUpdates.validateIdentity(pkg, "com.panelyra.tablet", version, 16, signatures, setOf("same")); fail("Unsafe APK accepted") }
            catch (_: IllegalArgumentException) { }
        }
    }
}
