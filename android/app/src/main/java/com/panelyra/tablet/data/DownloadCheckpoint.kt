package com.panelyra.tablet.data

/** A missing next route must never silently restart a partially completed batch. */
object DownloadCheckpoint {
    fun resumeUrl(job: DownloadJobEntity): String {
        require(job.currentChapter in 0..job.chapterCount) { "Invalid saved capture position" }
        require(job.currentChapter == 0 || job.resumeUrl.isNotBlank()) {
            "Saved chapters are safe, but the next chapter URL is unknown. Start a new selection at the next chapter."
        }
        return job.resumeUrl.ifBlank { job.startUrl }
    }

    fun beginAssisted(job: DownloadJobEntity): DownloadJobEntity = job.copy(
        status = "running", engine = "webview", remoteJobId = null, error = "",
        updatedAt = System.currentTimeMillis(),
    )
}
