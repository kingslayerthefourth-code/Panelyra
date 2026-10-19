package com.panelyra.tablet

import android.os.Bundle
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.Manifest
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import com.google.zxing.client.android.Intents
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import com.panelyra.tablet.ui.MainViewModel
import com.panelyra.tablet.ui.PanelyraApp

class MainActivity : ComponentActivity() {
    private val viewModel: MainViewModel by viewModels()
    private val scanner = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let(viewModel::pair)
    }
    private val importChapters = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        if (uris.isNotEmpty()) viewModel.importCbzFiles(uris)
    }
    private val notifications = registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (savedInstanceState == null) acceptShare(intent)
        if (Build.VERSION.SDK_INT >= 33) notifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        setContent {
            PanelyraApp(
                viewModel = viewModel,
                chooseCbzFiles = { importChapters.launch(arrayOf("*/*")) },
                scanPairingCode = {
                    scanner.launch(
                        ScanOptions()
                            .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                            .setPrompt("Scan the QR shown by Panelyra on your Mac")
                            .setBeepEnabled(false)
                            .setOrientationLocked(false)
                    )
                },
            )
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        acceptShare(intent)
    }

    @Suppress("DEPRECATION")
    private fun acceptShare(intent: Intent) {
        val uris = when (intent.action) {
            Intent.ACTION_VIEW -> listOfNotNull(intent.data)
            Intent.ACTION_SEND -> listOfNotNull(intent.getParcelableExtra<Uri>(Intent.EXTRA_STREAM))
            Intent.ACTION_SEND_MULTIPLE -> intent.getParcelableArrayListExtra<Uri>(Intent.EXTRA_STREAM).orEmpty()
            else -> emptyList()
        }.filter { it.scheme == "content" }
        if (uris.isNotEmpty()) viewModel.importCbzFiles(uris)
    }

    override fun onResume() {
        super.onResume()
        viewModel.appResumed()
    }

    override fun onPause() {
        viewModel.appPaused()
        super.onPause()
    }
}
