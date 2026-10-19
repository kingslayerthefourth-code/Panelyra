package com.panelyra.tablet.data

import androidx.room.Dao
import androidx.room.Delete
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query
import androidx.room.Transaction
import androidx.room.Upsert
import kotlinx.coroutines.flow.Flow

@Dao
interface PanelyraDao {
    @Query("""
        SELECT s.id, s.title, s.coverPath,
               COUNT(c.chapter) AS chapterCount,
               SUM(CASE WHEN c.completed = 0 THEN 1 ELSE 0 END) AS unreadCount,
               MAX(c.chapterNumber) AS latestChapter,
               MAX(c.lastReadAt) AS lastReadAt
        FROM series s LEFT JOIN chapters c ON s.id = c.seriesId
        GROUP BY s.id ORDER BY MAX(s.updatedAt, COALESCE(c.lastReadAt, 0)) DESC
    """)
    fun observeSeries(): Flow<List<SeriesSummary>>

    @Query("SELECT * FROM chapters WHERE seriesId = :seriesId ORDER BY chapterNumber, chapter")
    fun observeChapters(seriesId: String): Flow<List<ChapterEntity>>

    @Query("SELECT * FROM chapters WHERE seriesId = :seriesId ORDER BY chapterNumber, chapter")
    suspend fun chaptersForSeries(seriesId: String): List<ChapterEntity>

    @Query("SELECT * FROM series")
    suspend fun allSeries(): List<SeriesEntity>

    @Query("SELECT * FROM chapters WHERE seriesId = :seriesId AND chapter = :chapter LIMIT 1")
    suspend fun chapter(seriesId: String, chapter: String): ChapterEntity?

    @Query("SELECT * FROM chapters")
    suspend fun allChapters(): List<ChapterEntity>

    @Query("SELECT * FROM series WHERE id=:seriesId LIMIT 1")
    suspend fun series(seriesId: String): SeriesEntity?

    @Query("UPDATE series SET coverPath=:path WHERE id=:seriesId")
    suspend fun updateCover(seriesId: String, path: String)

    @Query("SELECT * FROM chapters WHERE lastReadAt > 0 AND completed = 0 ORDER BY lastReadAt DESC LIMIT 1")
    fun observeContinueReading(): Flow<ChapterEntity?>

    @Query("SELECT * FROM download_jobs ORDER BY createdAt DESC")
    fun observeJobs(): Flow<List<DownloadJobEntity>>

    @Query("SELECT * FROM download_jobs WHERE id=:id LIMIT 1")
    suspend fun job(id: String): DownloadJobEntity?

    @Upsert
    suspend fun putSeries(series: SeriesEntity)

    @Upsert
    suspend fun putChapter(chapter: ChapterEntity)

    @Insert(onConflict = OnConflictStrategy.REPLACE)
    suspend fun putJob(job: DownloadJobEntity)

    @Query("UPDATE download_jobs SET status=:status, engine=:engine, currentChapter=:current, remoteJobId=:remoteId, error=:error, updatedAt=:updatedAt WHERE id=:id")
    suspend fun updateJob(id: String, status: String, engine: String, current: Int, remoteId: String?, error: String, updatedAt: Long = System.currentTimeMillis())

    @Query("UPDATE chapters SET progressPanel=:panel, scrollOffset=:offset, readingMode=:mode, completed=:completed, lastReadAt=:now WHERE seriesId=:seriesId AND chapter=:chapter")
    suspend fun updateProgress(seriesId: String, chapter: String, panel: Int, offset: Int, mode: String, completed: Boolean, now: Long = System.currentTimeMillis())

    @Query("UPDATE chapters SET progressPanel=:panel, scrollOffset=:offset, readingMode=:mode, lastReadAt=:now WHERE seriesId=:seriesId AND chapter=:chapter")
    suspend fun savePosition(seriesId: String, chapter: String, panel: Int, offset: Int, mode: String, now: Long = System.currentTimeMillis())

    @Query("UPDATE chapters SET completed=:completed WHERE seriesId=:seriesId AND chapter=:chapter")
    suspend fun setCompleted(seriesId: String, chapter: String, completed: Boolean)

    @Query("SELECT * FROM transfers ORDER BY updatedAt DESC")
    fun observeTransfers(): Flow<List<TransferEntity>>

    @Query("SELECT * FROM transfers WHERE id=:id")
    suspend fun transfer(id: String): TransferEntity?

    @Upsert
    suspend fun putTransfer(transfer: TransferEntity)

    @Query("UPDATE transfers SET status=:status WHERE id=:id")
    suspend fun transferStatus(id: String, status: String)

    @Query("DELETE FROM chapters WHERE seriesId=:seriesId AND chapter=:chapter")
    suspend fun deleteChapterRow(seriesId: String, chapter: String)

    @Query("DELETE FROM series WHERE id=:seriesId AND NOT EXISTS (SELECT 1 FROM chapters WHERE seriesId=:seriesId)")
    suspend fun pruneSeries(seriesId: String)

    @Query("DELETE FROM series WHERE id IN (:ids)")
    suspend fun deleteSeries(ids: List<String>)

    @Transaction
    suspend fun install(series: SeriesEntity, chapter: ChapterEntity) {
        putSeries(series)
        putChapter(chapter)
    }

    @Transaction
    suspend fun installMapped(series: SeriesEntity, chapter: ChapterEntity, aliases: List<String>) {
        putSeries(series)
        for (alias in aliases.filter { it != series.id }) {
            for (old in chaptersForSeries(alias)) {
                val label = ChapterIdentity.chapter(old.chapter)
                val current = chapter(series.id, label)
                if (current == null || current.lastReadAt < old.lastReadAt)
                    putChapter(old.copy(seriesId = series.id, chapter = label))
                deleteChapterRow(alias, old.chapter)
            }
            pruneSeries(alias)
        }
        putChapter(chapter)
    }
}
