package com.panelyra.tablet.update

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.Settings
import androidx.core.app.NotificationCompat
import androidx.core.content.FileProvider
import com.panelyra.tablet.BuildConfig
import com.panelyra.tablet.MainActivity
import com.panelyra.tablet.R
import com.panelyra.tablet.download.CbzValidator
import org.json.JSONObject
import java.io.File
import java.security.MessageDigest

data class AppUpdateState(val enabled: Boolean = true, val version: String = "", val available: Boolean = false,
    val message: String = "New releases arrive from your paired Mac over Wi-Fi. Android asks before installing.")

class AppUpdates(private val context: Context) {
    private val prefs = context.getSharedPreferences("app_updates", Context.MODE_PRIVATE)
    val enabled get() = prefs.getBoolean("enabled", true)
    fun setEnabled(value: Boolean) { prefs.edit().putBoolean("enabled", value).apply() }
    val pendingCode get() = prefs.getLong("version_code", 0).takeIf { pendingFile()?.isFile == true } ?: 0
    fun state() = AppUpdateState(enabled, prefs.getString("version_name", "").orEmpty(), pendingCode > BuildConfig.VERSION_CODE)
    private fun pendingFile(): File? = prefs.getString("sha", null)?.takeIf { it.matches(Regex("[a-f0-9]{64}")) }?.let { File(context.filesDir, "updates/$it.apk") }
    fun alreadyReceived(meta: JSONObject): Boolean = meta.getLong("version_code") <= BuildConfig.VERSION_CODE ||
        (pendingCode == meta.getLong("version_code") && prefs.getString("sha", "") == meta.getString("sha256"))

    @Suppress("DEPRECATION")
    fun receive(file: File, meta: JSONObject) {
        check(enabled) { "Automatic app-update downloads are off on the tablet." }
        val sha = CbzValidator.sha256(file)
        require(sha == meta.getString("sha256")) { "Update checksum did not match." }
        val pm = context.packageManager
        val candidate = pm.getPackageArchiveInfo(file.absolutePath, PackageManager.GET_SIGNING_CERTIFICATES) ?: error("Invalid APK")
        val installed = pm.getPackageInfo(context.packageName, PackageManager.GET_SIGNING_CERTIFICATES)
        validateIdentity(candidate.packageName, context.packageName, candidate.longVersionCode, installed.longVersionCode, signers(candidate), signers(installed))
        require(candidate.longVersionCode == meta.getLong("version_code")) { "Update version did not match its manifest." }
        require(candidate.longVersionCode >= pendingCode) { "A newer update is already downloaded." }
        val target = File(context.filesDir, "updates/$sha.apk"); target.parentFile!!.mkdirs()
        val partial = File(target.parentFile, "$sha.partial")
        file.inputStream().use { input -> java.io.FileOutputStream(partial).use { out -> input.copyTo(out); out.fd.sync() } }
        check(CbzValidator.sha256(partial) == sha && partial.renameTo(target))
        check(prefs.edit().putString("sha", sha).putLong("version_code", candidate.longVersionCode).putString("version_name", candidate.versionName.orEmpty()).commit())
        // An update is downloaded, never silently installed or reported installed.
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel("app-updates", "Panelyra app updates", NotificationManager.IMPORTANCE_DEFAULT))
        val open = PendingIntent.getActivity(context, 181, Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        runCatching { manager.notify(181, NotificationCompat.Builder(context, "app-updates").setSmallIcon(R.drawable.ic_panelyra)
            .setContentTitle("Panelyra ${candidate.versionName} is ready")
            .setContentText("Open Panelyra → Settings to approve the update. Your books stay saved.").setContentIntent(open).setAutoCancel(true).build()) }
    }

    @Suppress("DEPRECATION")
    fun installIntent(): Intent {
        val file = pendingFile()?.takeIf { it.isFile && pendingCode > BuildConfig.VERSION_CODE } ?: error("No newer update is ready")
        require(CbzValidator.sha256(file) == prefs.getString("sha", "")) { "The update changed. Request a fresh copy from the Mac." }
        if (!context.packageManager.canRequestPackageInstalls())
            return Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${context.packageName}")).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        return Intent(Intent.ACTION_INSTALL_PACKAGE).setData(FileProvider.getUriForFile(context, "${context.packageName}.files", file))
            .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
    }

    private fun signers(info: PackageInfo): Set<String> = info.signingInfo?.apkContentsSigners.orEmpty().map { signature ->
        MessageDigest.getInstance("SHA-256").digest(signature.toByteArray()).joinToString("") { "%02x".format(it) }
    }.toSet()

    companion object {
        fun validateIdentity(candidate: String, ownPackage: String, version: Long, installed: Long, signers: Set<String>, ownSigners: Set<String>) {
            require(candidate == ownPackage && ownPackage == "com.panelyra.tablet") { "This APK is not Panelyra." }
            require(version > installed) { "Only a newer Panelyra update can be installed." }
            require(signers.isNotEmpty() && signers == ownSigners) { "The update has a different signing identity." }
        }
    }
}
