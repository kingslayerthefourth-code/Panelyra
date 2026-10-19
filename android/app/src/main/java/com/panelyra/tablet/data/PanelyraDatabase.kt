package com.panelyra.tablet.data

import android.content.Context
import androidx.room.Database
import androidx.room.Room
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(
    entities = [SeriesEntity::class, ChapterEntity::class, DownloadJobEntity::class, TransferEntity::class],
    version = 2,
    exportSchema = true,
)
abstract class PanelyraDatabase : RoomDatabase() {
    abstract fun dao(): PanelyraDao

    companion object {
        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("DROP INDEX IF EXISTS index_chapters_sha256")
                db.execSQL("CREATE INDEX index_chapters_sha256 ON chapters(sha256)")
                db.execSQL("ALTER TABLE series ADD COLUMN coverProvenance TEXT NOT NULL DEFAULT 'thumbnail'")
                db.execSQL("ALTER TABLE series ADD COLUMN coverVersion TEXT NOT NULL DEFAULT ''")
                db.execSQL("ALTER TABLE download_jobs ADD COLUMN resumeUrl TEXT NOT NULL DEFAULT ''")
                db.execSQL("CREATE TABLE IF NOT EXISTS transfers (id TEXT NOT NULL PRIMARY KEY, manifest TEXT NOT NULL, status TEXT NOT NULL, completedChapters INTEGER NOT NULL, totalChapters INTEGER NOT NULL, receivedBytes INTEGER NOT NULL, totalBytes INTEGER NOT NULL, bytesPerSecond INTEGER NOT NULL, etaSeconds INTEGER NOT NULL, error TEXT NOT NULL, updatedAt INTEGER NOT NULL)")
            }
        }
        @Volatile private var instance: PanelyraDatabase? = null
        fun get(context: Context): PanelyraDatabase = instance ?: synchronized(this) {
            instance ?: Room.databaseBuilder(
                context.applicationContext,
                PanelyraDatabase::class.java,
                "panelyra-tablet.db",
            ).addMigrations(MIGRATION_1_2).build().also { instance = it }
        }
    }
}
