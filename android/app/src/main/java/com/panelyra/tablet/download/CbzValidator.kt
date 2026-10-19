package com.panelyra.tablet.download

import android.graphics.BitmapFactory
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest
import java.math.BigInteger
import java.util.zip.CRC32
import java.util.zip.ZipFile

data class CbzValidation(val sha256: String, val entries: List<String>)

class CbzValidator {
    private val extensions = setOf("jpg", "jpeg", "png", "webp", "gif", "avif")

    fun validate(file: File): CbzValidation {
        require(file.isFile && file.length() > 0) { "The CBZ file is empty or missing." }
        val names = HashSet<String>()
        var expanded = 0L
        val entries = ZipFile(file).use { zip ->
            zip.entries().asSequence().filterNot { it.isDirectory }.map { entry ->
                val name = entry.name.replace('\\', '/')
                require(name == entry.name && !name.startsWith('/') && ':' !in name &&
                    name.split('/').none { it == ".." || it == "." || it.isEmpty() }) { "Unsafe CBZ filename: $name" }
                require(names.add(name.lowercase())) { "Duplicate CBZ path: $name" }
                require(names.size <= 2000 && entry.size in 1..50_000_000L) { "CBZ page exceeds limits" }
                expanded += entry.size
                require(expanded <= 1_000_000_000L) { "CBZ is too large" }
                require(name.substringAfterLast('.', "").lowercase() in extensions) { "Non-image CBZ entry: $name" }
                zip.getInputStream(entry).use { stream ->
                    val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
                    BitmapFactory.decodeStream(stream, null, options)
                    require(options.outWidth in 1..100_000 && options.outHeight in 1..100_000) { "Invalid image: $name" }
                    require(options.outWidth.toLong() * options.outHeight <= 32_000_000L) { "Page exceeds 32 megapixel limit: $name" }
                }
                zip.getInputStream(entry).use { stream ->
                    val options = BitmapFactory.Options().apply { inPreferredConfig = android.graphics.Bitmap.Config.RGB_565 }
                    val bitmap = BitmapFactory.decodeStream(stream, null, options)
                    require(bitmap != null) { "Image pixels could not be decoded: $name" }
                    bitmap.recycle()
                }
                val crc = CRC32()
                zip.getInputStream(entry).use { stream ->
                    val buffer = ByteArray(256 * 1024)
                    var count = 0L
                    while (true) {
                        val n = stream.read(buffer)
                        if (n < 0) break
                        count += n
                        require(count <= entry.size) { "Invalid expanded page size" }
                        crc.update(buffer, 0, n)
                    }
                    require(count == entry.size && crc.value == entry.crc) { "Corrupt CBZ page: $name" }
                }
                name
            }.sortedWith(pageOrder).toList()
        }
        require(entries.isNotEmpty()) { "The CBZ contains no readable pages." }
        return CbzValidation(sha256(file), entries)
    }

    @Synchronized fun extract(file: File, destination: File): List<File> {
        val marker = File(destination, "pages.json")
        val signature = "${file.absolutePath}|${file.length()}|${file.lastModified()}"
        runCatching {
            val saved = JSONObject(marker.readText())
            if (saved.getString("signature") == signature) {
                val mapping = saved.getJSONArray("pages")
                val pages = (0 until mapping.length()).map { index ->
                    val item = mapping.getJSONObject(index)
                    val name = item.getString("file")
                    require(Regex("\\d{5}\\.[a-z]+").matches(name))
                    File(destination, name).also { require(it.isFile && it.length() == item.getLong("size")) }
                }
                require(pages.isNotEmpty())
                marker.setLastModified(System.currentTimeMillis())
                return pages
            }
        }
        val validation = validate(file)
        destination.deleteRecursively()
        check(destination.mkdirs())
        val mapping = JSONArray()
        val pages = ZipFile(file).use { zip ->
            validation.entries.mapIndexed { index, name ->
                val target = File(destination, "%05d.%s".format(index + 1, name.substringAfterLast('.').lowercase()))
                zip.getInputStream(zip.getEntry(name)).use { input -> target.outputStream().use(input::copyTo) }
                mapping.put(JSONObject().put("entry", name).put("file", target.name).put("size", target.length()))
                target
            }
        }
        val partial = File(destination, "pages.json.partial")
        partial.writeText(JSONObject().put("signature", signature).put("sha256", validation.sha256).put("pages", mapping).toString())
        check(partial.renameTo(marker))
        return pages
    }

    companion object {
        val pageOrder = Comparator<String> { a, b ->
            val left = Regex("\\d+|\\D+").findAll(a.lowercase()).map { it.value }.toList()
            val right = Regex("\\d+|\\D+").findAll(b.lowercase()).map { it.value }.toList()
            var result = 0
            for (i in 0 until minOf(left.size, right.size)) {
                result = if (left[i].first().isDigit() && right[i].first().isDigit())
                    BigInteger(left[i]).compareTo(BigInteger(right[i])) else left[i].compareTo(right[i])
                if (result != 0) break
            }
            if (result != 0) result else if (left.size != right.size) left.size.compareTo(right.size) else a.compareTo(b)
        }

        fun sha256(file: File): String {
            val digest = MessageDigest.getInstance("SHA-256")
            file.inputStream().use { input ->
                val buffer = ByteArray(256 * 1024)
                while (true) {
                    val read = input.read(buffer)
                    if (read < 0) break
                    digest.update(buffer, 0, read)
                }
            }
            return digest.digest().joinToString("") { "%02x".format(it) }
        }
    }
}
