package com.panelyra.tablet.network

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo

@Suppress("DEPRECATION")
class MacDiscovery(context: Context, private val found: (String) -> Unit) {
    private val manager = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private var active = false
    private var currentListener: NsdManager.DiscoveryListener? = null
    private fun listener() = object : NsdManager.DiscoveryListener {
        override fun onDiscoveryStarted(serviceType: String) = Unit
        override fun onDiscoveryStopped(serviceType: String) { if (currentListener === this) active = false }
        override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) { if (currentListener === this) active = false }
        override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) { if (currentListener === this) active = false }
        override fun onServiceLost(serviceInfo: NsdServiceInfo) = Unit
        override fun onServiceFound(serviceInfo: NsdServiceInfo) {
            if (currentListener !== this || !active) return
            if (!serviceInfo.serviceName.startsWith("Panelyra")) return
            val owner = this
            manager.resolveService(serviceInfo, object : NsdManager.ResolveListener {
                override fun onResolveFailed(serviceInfo: NsdServiceInfo, errorCode: Int) = Unit
                override fun onServiceResolved(resolved: NsdServiceInfo) {
                    if (currentListener !== owner || !active) return
                    resolved.host?.hostAddress?.let {
                        val host = if (it.contains(':')) "[${it.replace("%", "%25")}]" else it
                        found("$host:${resolved.port}")
                    }
                }
            })
        }
    }

    fun start() {
        if (active) return
        val next = listener()
        currentListener = next
        active = true
        runCatching { manager.discoverServices("_panelyra._tcp.", NsdManager.PROTOCOL_DNS_SD, next) }
            .onFailure { active = false; currentListener = null }
    }

    fun stop() {
        val old = currentListener
        currentListener = null
        if (active && old != null) runCatching { manager.stopServiceDiscovery(old) }
        active = false
    }
}
