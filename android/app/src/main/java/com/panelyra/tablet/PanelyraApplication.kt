package com.panelyra.tablet

import android.app.Application
import com.panelyra.tablet.data.PanelyraDatabase

class PanelyraApplication : Application() {
    override fun onCreate() {
        super.onCreate()
        PanelyraDatabase.get(this)
    }
}
