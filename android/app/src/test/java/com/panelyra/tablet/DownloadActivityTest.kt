package com.panelyra.tablet

import com.panelyra.tablet.download.DownloadActivity
import com.panelyra.tablet.download.waitDuration
import org.junit.Assert.*
import org.junit.Test

class DownloadActivityTest {
    @Test fun unknownAndPausedWorkNeverGetsAnInventedEta() {
        assertNull(DownloadActivity().eta("running", 0, 10))
        val measured = DownloadActivity(measuredChapterMillis = 60000, measuredChapters = 2)
        assertEquals(90L, measured.eta("running", 2, 5))
        for (status in listOf("queued", "interrupted", "failed", "cancelled", "completed", "queued_for_mac"))
            assertNull(measured.eta(status, 2, 5))
        assertNull(measured.eta("running", 5, 5))
        assertNull(measured.copy(startedAt = 1).eta("running", 2, 5, now = 150001))
    }
    @Test fun pageCountsAreMeasuredBoundedAndIndependentFromSavedChapters() {
        assertNull(DownloadActivity().pageFraction())
        assertEquals(.5f, DownloadActivity(page = 5, pages = 10).pageFraction())
        assertEquals(1f, DownloadActivity(page = 11, pages = 10).pageFraction())
        assertEquals(0L, DownloadActivity(startedAt = 10000).elapsed(5000))
        assertEquals("1m 5s", waitDuration(65))
    }
}
