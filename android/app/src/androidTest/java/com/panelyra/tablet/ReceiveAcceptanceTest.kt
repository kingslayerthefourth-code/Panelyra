package com.panelyra.tablet

import android.graphics.Bitmap
import android.os.Build
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.test.*
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.test.platform.app.InstrumentationRegistry
import com.panelyra.tablet.data.PanelyraDatabase
import com.panelyra.tablet.receive.TabletReceiveService
import kotlinx.coroutines.runBlocking
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import java.io.File

/** Synthetic emulator only. Host runner pushes fixtures over the real TLS service. */
class ReceiveAcceptanceTest {
    @get:Rule val compose = createAndroidComposeRule<MainActivity>()
    @Test fun receivesInBackgroundAndRemembersPairing() = runBlocking {
        require(Build.HARDWARE in setOf("ranchu", "goldfish")) { "Use the isolated emulator only" }
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val dao = PanelyraDatabase.get(context).dao()
        val signal = File(context.filesDir, "receive-acceptance.json")
        fun phase(name: String) {
            val state = TabletReceiveService.state.value
            signal.writeText(JSONObject().put("phase", name).put("code", state.code).put("message", state.message).toString())
        }
        compose.onNodeWithContentDescription("Connections & transfers").performClick()
        compose.onAllNodesWithText("Receive from Mac").filter(hasClickAction())[0].performClick()
        compose.waitUntil(20_000) { TabletReceiveService.state.value.running || TabletReceiveService.state.value.message.startsWith("Receive mode could not start") }
        assertTrue(TabletReceiveService.state.value.message, TabletReceiveService.state.value.running)
        phase("ready")
        compose.waitUntil(90_000) { runBlocking { dao.chapter("receive-fixture", "1") != null && dao.series("receive-fixture")?.coverProvenance == "supplied" } }
        assertTrue(TabletReceiveService.state.value.paired)
        assertEquals("", TabletReceiveService.state.value.code)
        dao.savePosition("receive-fixture", "1", 0, 41, "continuous")
        compose.onAllNodes(isRoot()).onLast().captureToImage().asAndroidBitmap().let { image -> File(context.filesDir, "receive-screen.png").outputStream().use { image.compress(Bitmap.CompressFormat.PNG, 100, it) } }
        compose.runOnUiThread { TabletReceiveService.stop(context) }
        compose.waitUntil(10_000) { !TabletReceiveService.state.value.running }
        Thread.sleep(500)
        compose.runOnUiThread { TabletReceiveService.start(context) }
        compose.waitUntil(20_000) { TabletReceiveService.state.value.running }
        assertTrue(TabletReceiveService.state.value.paired)
        assertEquals("", TabletReceiveService.state.value.code)
        phase("restarted")
        compose.waitUntil(90_000) { File(context.filesDir, "host-reconnected").exists() }
        assertEquals(41, dao.chapter("receive-fixture", "1")!!.scrollOffset)
        compose.runOnUiThread { compose.activity.moveTaskToBack(true) }
        phase("background")
        val deadline = System.currentTimeMillis() + 90_000
        while (dao.chapter("receive-fixture", "2") == null && System.currentTimeMillis() < deadline) Thread.sleep(200)
        assertNotNull("Background receive did not install chapter 2", dao.chapter("receive-fixture", "2"))
        assertEquals(41, dao.chapter("receive-fixture", "1")!!.scrollOffset)
        assertEquals(2, dao.chaptersForSeries("receive-fixture").size)
        phase("done")
        TabletReceiveService.stop(context)
    }
}
