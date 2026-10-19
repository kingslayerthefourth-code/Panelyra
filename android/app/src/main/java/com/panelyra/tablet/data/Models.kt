package com.panelyra.tablet.data

import androidx.room.Entity
import androidx.room.ForeignKey
import androidx.room.Index
import androidx.room.PrimaryKey
import androidx.room.ColumnInfo

@Entity(tableName = "series")
data class SeriesEntity(
    @PrimaryKey val id: String,
    val title: String,
    val coverPath: String? = null,
    val updatedAt: Long = System.currentTimeMillis(),
    @ColumnInfo(defaultValue = "'thumbnail'") val coverProvenance: String = "thumbnail",
    @ColumnInfo(defaultValue = "''") val coverVersion: String = "",
)

@Entity(
    tableName = "chapters",
    primaryKeys = ["seriesId", "chapter"],
    foreignKeys = [ForeignKey(
        entity = SeriesEntity::class,
        parentColumns = ["id"],
        childColumns = ["seriesId"],
        onDelete = ForeignKey.CASCADE,
    )],
    indices = [Index("seriesId"), Index(value = ["sha256"])],
)
data class ChapterEntity(
    val seriesId: String,
    val chapter: String,
    val chapterNumber: Double?,
    val title: String,
    val cbzPath: String,
    val sha256: String,
    val pageCount: Int,
    val byteSize: Long,
    val sourceUrl: String,
    val progressPanel: Int = 0,
    val scrollOffset: Int = 0,
    val readingMode: String = "fit",
    val completed: Boolean = false,
    val downloadedAt: Long = System.currentTimeMillis(),
    val lastReadAt: Long = 0,
)

@Entity(tableName = "download_jobs", indices = [Index("status")])
data class DownloadJobEntity(
    @PrimaryKey val id: String,
    val startUrl: String,
    val chapterCount: Int,
    val performance: String,
    val status: String,
    val engine: String = "tablet",
    val currentChapter: Int = 0,
    val remoteJobId: String? = null,
    val error: String = "",
    val createdAt: Long = System.currentTimeMillis(),
    val updatedAt: Long = System.currentTimeMillis(),
    @ColumnInfo(defaultValue = "''") val resumeUrl: String = "",
)

data class SeriesSummary(
    val id: String,
    val title: String,
    val coverPath: String?,
    val chapterCount: Int,
    val unreadCount: Int,
    val latestChapter: Double?,
    val lastReadAt: Long,
)

@Entity(tableName = "transfers")
data class TransferEntity(
    @PrimaryKey val id: String,
    val manifest: String,
    val status: String = "queued",
    val completedChapters: Int = 0,
    val totalChapters: Int = 0,
    val receivedBytes: Long = 0,
    val totalBytes: Long = 0,
    val bytesPerSecond: Long = 0,
    val etaSeconds: Long = 0,
    val error: String = "",
    val updatedAt: Long = System.currentTimeMillis(),
)
