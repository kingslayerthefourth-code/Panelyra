package com.panelyra.tablet.download

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ChapterRoutingTest {
    @Test fun advancesNumberedChapterWithoutDroppingQuery() {
        assertEquals(
            "https://reader.test/book/chapter-2?mode=reader",
            ChapterRouting.increment("https://reader.test/book/chapter-1?mode=reader"),
        )
        assertEquals(20.0, ChapterRouting.number("Series — Chapter 20") ?: Double.NaN, 0.0)
    }

    @Test fun refusesUnnumberedAndDecimalFallbacks() {
        assertNull(ChapterRouting.increment("https://reader.test/book/volume-seven"))
        assertNull(ChapterRouting.increment("https://reader.test/book/chapter-7.5"))
    }
}
