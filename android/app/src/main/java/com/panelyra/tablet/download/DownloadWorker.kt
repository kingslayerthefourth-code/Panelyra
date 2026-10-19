package com.panelyra.tablet.download

import android.content.Context
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import com.panelyra.tablet.data.LibraryRepository
import com.panelyra.tablet.data.PanelyraDatabase
import com.panelyra.tablet.data.DownloadCheckpoint
import com.panelyra.tablet.network.CompanionClient
import kotlinx.coroutines.delay
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.currentCoroutineContext
import kotlin.coroutines.coroutineContext
import java.io.File

class DownloadWorker(context: Context, parameters: WorkerParameters) : CoroutineWorker(context, parameters) {
    private val dao = PanelyraDatabase.get(context).dao()
    private val library = LibraryRepository(context, dao)
    private val direct = DirectCaptureEngine(context)
    private val companion = CompanionClient(context)
    private var runStartedAt = 0L
    private var savedAtStart = 0
    private var measuredChapterMillis = 0L
    private var measuredChapters = 0
    private var chapterStartedAt = 0L
    private suspend fun report(id: String, activity: CaptureActivity) {
        setProgress(workDataOf(KEY_JOB_ID to id, "stage" to activity.stage,
            "page" to activity.page, "pages" to activity.pages, "bytes" to activity.bytes,
            "started_at" to runStartedAt, "saved_at_start" to savedAtStart,
            "measured_ms" to measuredChapterMillis, "measured_count" to measuredChapters))
    }

    override suspend fun doWork(): Result {
        val id = inputData.getString(KEY_JOB_ID) ?: return Result.failure()
        val job = dao.job(id) ?: return Result.failure()
        if (job.status == "cancelled") return Result.failure()
        var saved = job.currentChapter
        runStartedAt = System.currentTimeMillis()
        chapterStartedAt = runStartedAt
        savedAtStart = saved
        report(id, CaptureActivity("Preparing download"))
        dao.updateJob(id, "running", job.engine, saved, job.remoteJobId, "")
        try {
            if (job.remoteJobId != null) {
                runMacFallback(id, job.resumeUrl.ifBlank { job.startUrl }, job.chapterCount - saved, job.performance)
                return Result.success()
            }
            if (saved >= job.chapterCount) { dao.updateJob(id, "completed", "tablet", saved, null, ""); return Result.success() }
            direct.capture(DownloadCheckpoint.resumeUrl(job), job.chapterCount - saved, job.performance,
                onChapter = { chapter, nextUrl ->
                    currentCoroutineContext().ensureActive()
                    report(id, CaptureActivity("Adding to Reader"))
                    library.installCbz(chapter.cbz, chapter.seriesTitle, chapter.chapterLabel, chapter.title, chapter.sourceUrl)
                    saved++
                    val finishedAt = System.currentTimeMillis()
                    measuredChapterMillis += (finishedAt - chapterStartedAt).coerceAtLeast(0)
                    measuredChapters++
                    chapterStartedAt = finishedAt
                    val current = dao.job(id) ?: job
                    dao.putJob(current.copy(status = "running", engine = "tablet", currentChapter = saved,
                        resumeUrl = nextUrl.orEmpty(), updatedAt = System.currentTimeMillis()))
                    chapter.cbz.parentFile?.deleteRecursively()
                    report(id, CaptureActivity("Chapter saved · preparing the next one"))
                }, onActivity = { report(id, it) })
            dao.updateJob(id, "completed", "tablet", saved, null, "")
            return Result.success()
        } catch (error: CancellationException) {
            withContext(NonCancellable + Dispatchers.IO) {
                val current = dao.job(id)
                current?.remoteJobId?.let { runCatching { companion.cancel(it) } }
                if (current?.status != "cancelled") dao.updateJob(id, "interrupted", current?.engine ?: "tablet", current?.currentChapter ?: saved, current?.remoteJobId, "Reopen and retry to resume")
            }
            throw error
        } catch (directError: Exception) {
            currentCoroutineContext().ensureActive()
            if (dao.job(id)?.status == "cancelled") throw CancellationException("Capture cancelled")
            if (job.remoteJobId != null) {
                val latest = dao.job(id) ?: job
                val terminal = directError is IllegalArgumentException
                dao.updateJob(id, if (terminal) "failed" else "queued_for_mac", "mac", latest.currentChapter,
                    latest.remoteJobId, directError.message.orEmpty())
                return if (terminal) Result.failure() else Result.retry()
            }
            // Another computer cannot fix local disk or validation errors. Only a
            // recognised source/browser failure may request a different engine.
            if (!companion.isPaired || directError !is CaptureFailure) {
                val status = if (directError is CaptureFailure && directError.browserMayHelp) "needs_webview" else "failed"
                dao.updateJob(id, status, "tablet", saved, null, directError.message ?: "Direct capture failed.")
                return Result.failure()
            }
            val current = dao.job(id) ?: job
            return runCatching { runMacFallback(id, current.resumeUrl.ifBlank { current.startUrl }, current.chapterCount - saved, current.performance) }
                .fold(
                    onSuccess = { Result.success() },
                    onFailure = { error ->
                        if (error is CancellationException) throw error
                        val latest = dao.job(id)
                        val terminal = error is IllegalArgumentException
                        dao.updateJob(id, if (terminal) "failed" else "queued_for_mac", "mac", latest?.currentChapter ?: saved, latest?.remoteJobId, error.message ?: directError.message.orEmpty())
                        if (terminal) Result.failure() else Result.retry()
                    },
                )
        }
    }

    private suspend fun runMacFallback(id: String, url: String, count: Int, performance: String) {
        report(id, CaptureActivity("Connecting to your Mac"))
        val current = dao.job(id) ?: error("Missing download job")
        var already = current.currentChapter
        var remote = current.remoteJobId?.let { companion.job(it) } ?: companion.submit(DownloadCheckpoint.resumeUrl(current), count, performance)
        val receipts = mutableSetOf<String>()
        for (artifact in remote.artifacts) {
            if (library.verified(artifact.series, artifact.chapter, artifact.sha256, artifact.size)) receipts.add(artifact.id)
        }
        if (current.remoteJobId != null) already = (already - receipts.size).coerceAtLeast(0)
        dao.updateJob(id, "running", "mac", already, remote.id, "")
        suspend fun publish() {
            for (artifact in remote.artifacts) {
                currentCoroutineContext().ensureActive()
                if (receipts.contains(artifact.id)) continue
                val temporary = File(applicationContext.filesDir, "mac-transfer/${remote.id}/${artifact.id}.cbz")
                if (!library.verified(artifact.series, artifact.chapter, artifact.sha256, artifact.size)) {
                    report(id, CaptureActivity("Receiving chapter from Mac"))
                    companion.download(artifact, temporary)
                    report(id, CaptureActivity("Checking and adding to Reader", bytes = temporary.length()))
                    library.installCbz(temporary, artifact.series.replace('-', ' ').replaceFirstChar(Char::uppercase),
                        artifact.chapter, artifact.title, artifact.sourceUrl.ifBlank { url }, artifact.sha256, artifact.series)
                }
                receipts.add(artifact.id)
                temporary.delete()
                val latest = dao.job(id) ?: current
                dao.putJob(latest.copy(currentChapter = (already + receipts.size).coerceAtMost(current.chapterCount),
                    remoteJobId = remote.id, engine = "mac", resumeUrl = ChapterRouting.increment(artifact.sourceUrl.ifBlank { url }).orEmpty()))
            }
        }
        while (remote.status in setOf("queued", "running", "cancelling")) {
            publish()
            if (isStopped) {
                companion.cancel(remote.id)
                val latest = dao.job(id) ?: current
                dao.updateJob(id, "cancelled", "mac", latest.currentChapter, remote.id, "")
                return
            }
            delay(2_000)
            remote = companion.job(remote.id)
            report(id, CaptureActivity(if (remote.status == "queued") "Waiting in the Mac queue" else "Mac is preparing chapters"))
        }
        publish()
        if (remote.status == "cancelled") {
            val latest = dao.job(id) ?: current
            dao.updateJob(id, "cancelled", "mac", latest.currentChapter, null, "")
            throw CancellationException("Mac capture cancelled")
        }
        if (remote.status in setOf("failed", "interrupted")) {
            val latest = dao.job(id) ?: current
            dao.putJob(latest.copy(status = "failed", remoteJobId = null, error = remote.error.ifBlank { "Mac capture stopped; completed chapters are kept. Retry to resume." }))
            throw IllegalArgumentException("Mac capture stopped; completed chapters are kept. Retry to resume.")
        }
        check(remote.status == "completed") { remote.error.ifBlank { "Mac Capture failed." } }
        dao.updateJob(id, "completed", "mac", (already + receipts.size).coerceAtMost(current.chapterCount), remote.id, "")
    }

    companion object { const val KEY_JOB_ID = "job_id" }
}
