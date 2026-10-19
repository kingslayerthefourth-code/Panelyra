package com.panelyra.tablet.update

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.panelyra.tablet.receive.TabletReceiveService

/** Resume an already-enabled receiver after an OS-approved package update. */
class AppUpdatedReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_MY_PACKAGE_REPLACED) return
        val prefs = context.getSharedPreferences("panelyra_receive", Context.MODE_PRIVATE)
        if (prefs.getBoolean("enabled", false) && prefs.getString("token", "").orEmpty().isNotBlank())
            runCatching { TabletReceiveService.start(context) }
        // EMUI can still restrict background startup; opening Panelyra retries.
    }
}
