package com.panelyra.tablet.download

import androidx.work.Data

/** Small durable WorkManager snapshots, not timer-generated percentages. */
data class CaptureActivity(val stage: String, val page: Int = 0, val pages: Int = 0, val bytes: Long = 0)

data class DownloadActivity(
    val stage: String = "Preparing download", val page: Int = 0, val pages: Int = 0,
    val bytes: Long = 0, val startedAt: Long = 0, val savedAtStart: Int = 0,
    val measuredChapterMillis: Long = 0, val measuredChapters: Int = 0,
) {
    fun elapsed(now: Long) = if (startedAt > 0) ((now - startedAt) / 1000).coerceAtLeast(0) else 0
    fun eta(status: String, saved: Int, total: Int, now: Long = System.currentTimeMillis()): Long? {
        if (status != "running" || measuredChapters < 2 || measuredChapterMillis <= 0 || total <= saved) return null
        val average = measuredChapterMillis / measuredChapters
        val currentMillis = if (startedAt > 0) (now - startedAt - measuredChapterMillis).coerceAtLeast(0) else 0
        if (currentMillis > average * 2) return null
        return ((average * (total - saved) - currentMillis.coerceAtMost(average)) / 1000).coerceAtLeast(1)
    }
    fun pageFraction(): Float? = if (pages > 0) (page.toFloat() / pages).coerceIn(0f, 1f) else null
    companion object {
        fun from(data: Data) = DownloadActivity(data.getString("stage") ?: "Preparing download",
            data.getInt("page", 0), data.getInt("pages", 0), data.getLong("bytes", 0),
            data.getLong("started_at", 0), data.getInt("saved_at_start", 0),
            data.getLong("measured_ms", 0), data.getInt("measured_count", 0))
    }
}

fun waitDuration(seconds: Long): String = seconds.coerceAtLeast(0).let {
    if (it >= 60) "${it / 60}m ${it % 60}s" else "${it}s"
}

val DOWNLOAD_NOTES = listOf(
    "Finished chapters appear in Reader while the rest download.",
    "We check each archive before adding it to your library.",
    "Your downloaded chapters stay readable offline.",
    "Retry keeps the chapters that already finished.",
)
