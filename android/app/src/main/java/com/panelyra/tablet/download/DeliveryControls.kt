package com.panelyra.tablet.download

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.app.NotificationManager
import android.app.NotificationChannel
import android.app.PendingIntent
import androidx.core.app.NotificationCompat
import com.panelyra.tablet.MainActivity
import com.panelyra.tablet.R
import androidx.work.*
import com.panelyra.tablet.data.PanelyraDatabase
import com.panelyra.tablet.network.CompanionClient
import kotlinx.coroutines.*

object DeliveryScheduler {
    const val WORK = "panelyra-delivery"
    fun request(context: Context) {
        WorkManager.getInstance(context).enqueueUniqueWork(WORK, ExistingWorkPolicy.KEEP,
            OneTimeWorkRequestBuilder<LibrarySyncWorker>().setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, java.util.concurrent.TimeUnit.SECONDS).build())
    }
}

class DeliveryPollWorker(context: Context, params: WorkerParameters) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result { DeliveryScheduler.request(applicationContext); return Result.success() }
}

class DeliveryControlReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val result = goAsync()
        CoroutineScope(Dispatchers.IO).launch {
            try {
                val resume = intent.action == "com.panelyra.tablet.RESUME"
                context.getSharedPreferences("panelyra_sync", Context.MODE_PRIVATE).edit().putBoolean("paused", !resume).commit()
                val manager = context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
                if (resume) {
                    manager.cancel(141)
                    val client = CompanionClient(context)
                    runCatching {
                        if (client.isPaired) client.pendingTransfers().filter { it.optString("status") == "paused" }
                            .forEach { client.transferAction(it.getString("transfer_id"), "resume") }
                    }
                    DeliveryScheduler.request(context)
                } else {
                    WorkManager.getInstance(context).cancelUniqueWork(DeliveryScheduler.WORK)
                    manager.createNotificationChannel(NotificationChannel("panelyra-delivery", "Chapter delivery", NotificationManager.IMPORTANCE_LOW))
                    val resumeAction = PendingIntent.getBroadcast(context, 141, Intent(context, DeliveryControlReceiver::class.java)
                        .setAction("com.panelyra.tablet.RESUME"), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                    val open = PendingIntent.getActivity(context, 141, Intent(context, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)
                    runCatching { manager.notify(141, NotificationCompat.Builder(context, "panelyra-delivery").setSmallIcon(R.drawable.ic_panelyra)
                        .setContentTitle("Panelyra · Delivery paused").setContentText("Completed chapters are ready. Resume when you want.")
                        .setContentIntent(open).addAction(0, "Resume", resumeAction).build()) }
                }
            } finally { result.finish() }
        }
    }
}
