package com.panelyra.tablet.ui

import com.panelyra.tablet.data.ChapterEntity
import com.panelyra.tablet.data.SeriesSummary
import java.util.Locale

/** Mac library semantics, independent of screen size and without changing progress. */
object LibraryPresentation {
    fun books(rows: List<SeriesSummary>, query: String = "", unreadOnly: Boolean = false): List<SeriesSummary> =
        rows.filter { it.title.contains(query.trim(), ignoreCase = true) && (!unreadOnly || it.unreadCount > 0) }
            .sortedWith(compareBy<SeriesSummary> { it.title.lowercase(Locale.ROOT) }.thenBy { it.id })

    fun progress(chapter: ChapterEntity): Float = when {
        chapter.completed -> 1f
        chapter.lastReadAt <= 0 -> 0f
        else -> ((chapter.progressPanel + 1).toFloat() / chapter.pageCount.coerceAtLeast(1)).coerceIn(0f, 1f)
    }

    fun chapters(rows: List<ChapterEntity>, query: String = "", filter: String = "All chapters", newest: Boolean = true): List<ChapterEntity> {
        val filtered = rows.filter { chapter ->
            ("Chapter ${chapter.chapter} ${chapter.title}").contains(query.trim(), ignoreCase = true) &&
                when (filter) {
                    "Unread" -> !chapter.completed
                    "In progress" -> !chapter.completed && chapter.lastReadAt > 0
                    else -> true
                }
        }.sortedWith(compareBy<ChapterEntity> { it.chapterNumber ?: it.chapter.toDoubleOrNull() ?: Double.MAX_VALUE }.thenBy { it.chapter })
        return if (newest) filtered.reversed() else filtered
    }

    fun start(rows: List<ChapterEntity>): ChapterEntity? =
        rows.filter { !it.completed && it.lastReadAt > 0 }.maxByOrNull { it.lastReadAt }
            ?: chapters(rows, newest = false).firstOrNull { !it.completed }
            ?: chapters(rows, newest = false).firstOrNull()
}
