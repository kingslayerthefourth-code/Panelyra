package com.panelyra.tablet

import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.platform.app.InstrumentationRegistry
import com.panelyra.tablet.data.*
import com.panelyra.tablet.download.DeliveryScheduler
import com.panelyra.tablet.network.CompanionClient
import kotlinx.coroutines.runBlocking
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import java.io.File

class TabletAcceptanceTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()

    @Test fun upgradeRestoresOrphansAndPreservesSurvivingProgress() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val dao = PanelyraDatabase.get(context).dao()
        val started = System.currentTimeMillis()
        while (System.currentTimeMillis() - started < 30_000 && dao.chaptersForSeries("fixture-book").size < 5) Thread.sleep(250)
        assertEquals(listOf("1", "2", "3", "4", "5"), dao.chaptersForSeries("fixture-book").map { it.chapter })
        val five = dao.chapter("fixture-book", "5")!!
        assertEquals(2, five.progressPanel); assertEquals(45, five.scrollOffset); assertTrue(five.completed)
        compose.waitUntil(10_000) { compose.onAllNodesWithText("Fixture Book").fetchSemanticsNodes().isNotEmpty() }
        compose.onAllNodesWithText("Fixture Book")[0].performClick()
        for (chapter in 1..5) compose.onNodeWithText("Chapter $chapter").assertExists()
        println("Old APK → v1.4 upgrade restored all five disk chapters and preserved chapter 5 progress")
    }

    @Test fun selectedWifiChaptersAppearWithoutPickerAndReaderNavigates() = runBlocking {
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val args = InstrumentationRegistry.getArguments()
        val client = CompanionClient(context)
        if (!client.isPaired) client.pairWithCode(args.getString("address")!!, args.getString("code")!!)
        assertTrue("Saved certificate should authenticate a changed Mac address", client.reconnect("127.0.0.1:19443"))
        val dao = PanelyraDatabase.get(context).dao()
        DeliveryScheduler.request(context)
        val started = System.currentTimeMillis()
        while (System.currentTimeMillis() - started < 60_000 && dao.chaptersForSeries("fixture-book").size < 5) Thread.sleep(250)
        assertEquals(listOf("1", "2", "3", "4", "5"), dao.chaptersForSeries("fixture-book").map { it.chapter })
        val book = dao.series("fixture-book")!!
        while (System.currentTimeMillis() - started < 60_000 && dao.series("fixture-book")!!.coverProvenance != "supplied") Thread.sleep(250)
        assertEquals("supplied", dao.series("fixture-book")!!.coverProvenance)
        assertTrue(File(dao.series("fixture-book")!!.coverPath!!).isFile)
        println("Verified five-chapter automatic delivery took ${System.currentTimeMillis() - started}ms")
        compose.waitUntil(10_000) { compose.onAllNodesWithText("Fixture Book").fetchSemanticsNodes().isNotEmpty() }
        compose.onAllNodesWithText("Fixture Book")[0].performClick()
        for (chapter in 1..5) compose.onNodeWithText("Chapter $chapter").assertExists()
        compose.onNodeWithText("Chapter 1").performClick()
        compose.waitUntil(10_000) { compose.onAllNodesWithText("Loading chapter…").fetchSemanticsNodes().isEmpty() }
        compose.onNodeWithText("Next →").performClick()
        compose.onAllNodesWithText("Chapter 2")[0].assertExists()
        compose.onNodeWithText("← Previous").performClick()
        compose.onNodeWithText("Mark complete").performClick()
        compose.onNodeWithText("Chapter 1/5 · All").performClick()
        compose.onNodeWithText("Chapter 5").performClick()
        compose.onNodeWithText("← Previous").performClick()
        compose.onAllNodesWithText("Chapter 4")[0].assertExists()
        assertTrue(dao.chapter("fixture-book", "1")!!.completed)
        val library = LibraryRepository(context, dao)
        val one = dao.chapter("fixture-book", "1")!!
        val first = System.nanoTime(); library.pages(one); val initial = (System.nanoTime() - first) / 1e6
        val second = System.nanoTime(); library.pages(one); val cached = (System.nanoTime() - second) / 1e6
        println("Actual Android cache reopen=${cached}ms first=${initial}ms")
        // Offline Reader uses only the local archive and cache, independent of pairing.
        client.unpair()
        assertEquals(3, library.pages(one).size)
        assertEquals(5, dao.chaptersForSeries("fixture-book").size)
    }
}
