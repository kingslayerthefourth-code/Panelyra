package com.panelyra.tablet.data

import java.math.BigDecimal
import java.net.URI
import java.security.MessageDigest

object ChapterIdentity {
    fun chapter(label: String): String = LibraryRepository.chapterNumber(label)?.let {
        BigDecimal.valueOf(it).stripTrailingZeros().toPlainString()
    } ?: label.trim().also { require(it !in setOf(".", "..") && Regex("[a-zA-Z0-9._-]+").matches(it)) { "Unsafe chapter label" } }

    // Only a manifest key or a source URL establishes identity. Titles never alias sources.
    fun series(title: String, key: String?, source: String): String {
        if (key != null) {
            require(key !in setOf(".", "..") && Regex("[a-zA-Z0-9._-]+").matches(key)) { "Unsafe series identity" }
            return key
        }
        if (source.startsWith("http")) {
            val uri = URI(source)
            val identity = sourceIdentity(source)
            val digest = MessageDigest.getInstance("SHA-256").digest(identity.toByteArray()).take(8)
                .joinToString("") { "%02x".format(it) }
            return "source-$digest"
        }
        return LibraryRepository.slug(title)
    }

    fun sourceIdentity(source: String): String {
        if (!source.startsWith("http")) return ""
        val uri = URI(source)
        if (uri.host.equals("comic.naver.com", true)) {
            val title = uri.query.orEmpty().split('&').firstOrNull { it.substringBefore('=') == "titleId" }?.substringAfter('=')
            require(!title.isNullOrBlank()) { "Naver series identity is missing titleId" }
            return "comic.naver.com/webtoon/list|titleId=$title"
        }
        val path = uri.path.replace(Regex("(?:chapter|chap|ch|episode|ep)[-_/.:]*\\d+(?:\\.\\d+)?(?:/viewer)?/?$", RegexOption.IGNORE_CASE), "").trimEnd('/')
        val sourceId = uri.query.orEmpty().split('&').filter { it.substringBefore('=') in setOf("title_no", "series_id", "manga_id", "titleId") }.sorted().joinToString("&")
        return "${uri.host.lowercase()}$path|$sourceId"
    }
}
