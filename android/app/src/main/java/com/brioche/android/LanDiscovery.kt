package com.brioche.android

import android.content.Context
import android.net.wifi.WifiManager
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.InetAddress
import java.net.MulticastSocket
import java.net.SocketTimeoutException
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.atomic.AtomicBoolean

data class NearbyLanDevice(
    val ip: String,
    val port: Int,
    val name: String,
    val platform: String,
    val pinPrefix: String,
    val busy: Boolean,
    val seenAtMs: Long,
)

/** Listens for Brioche LAN multicast beacons (239.255.47.95:47993). */
class LanDiscovery(
    private val context: Context,
    private val onUpdate: (List<NearbyLanDevice>) -> Unit,
) {
    companion object {
        const val MULTICAST_GROUP = "239.255.47.95"
        const val MULTICAST_PORT = 47993
        const val TTL_MS = 14_000L

        fun parseBeacon(raw: ByteArray, senderIp: String, nowMs: Long = System.currentTimeMillis()): NearbyLanDevice? {
            if (raw.isEmpty() || raw.size > 1024) return null
            val json = try {
                JSONObject(String(raw, Charsets.UTF_8))
            } catch (_: Exception) {
                return null
            }
            if (json.optString("product") != "Brioche") return null
            val port = json.optInt("port", -1)
            val name = json.optString("name", "").trim()
            if (port !in 1024..65535 || name.isEmpty()) return null
            return NearbyLanDevice(
                ip = senderIp,
                port = port,
                name = name.take(64),
                platform = json.optString("platform", "Desktop").take(32),
                pinPrefix = json.optString("pin_prefix", "").take(16),
                busy = json.optBoolean("busy", false),
                seenAtMs = nowMs,
            )
        }
    }

    private val running = AtomicBoolean(false)
    private val seen = ConcurrentHashMap<String, NearbyLanDevice>()
    private var thread: Thread? = null
    private var multicastLock: WifiManager.MulticastLock? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        try {
            val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager
            multicastLock = wm?.createMulticastLock("brioche-lan-beacon")?.apply {
                setReferenceCounted(false)
                acquire()
            }
        } catch (_: Exception) {
        }
        thread = Thread({ runLoop() }, "brioche-lan-beacon").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        running.set(false)
        thread?.interrupt()
        thread = null
        try {
            multicastLock?.let { if (it.isHeld) it.release() }
        } catch (_: Exception) {
        }
        multicastLock = null
    }

    private fun runLoop() {
        var socket: MulticastSocket? = null
        try {
            val group = InetAddress.getByName(MULTICAST_GROUP)
            socket = MulticastSocket(MULTICAST_PORT).apply {
                reuseAddress = true
                soTimeout = 1500
                joinGroup(group)
            }
            val buf = ByteArray(2048)
            while (running.get()) {
                val pkt = DatagramPacket(buf, buf.size)
                try {
                    socket.receive(pkt)
                    val senderIp = pkt.address?.hostAddress ?: continue
                    val data = buf.copyOfRange(0, pkt.length)
                    val parsed = parseBeacon(data, senderIp)
                    if (parsed != null) {
                        seen["${parsed.ip}:${parsed.port}"] = parsed
                        emitSnapshot()
                    }
                } catch (_: SocketTimeoutException) {
                    emitSnapshot()
                }
            }
        } catch (_: Exception) {
        } finally {
            try {
                socket?.close()
            } catch (_: Exception) {
            }
        }
    }

    private fun emitSnapshot() {
        val now = System.currentTimeMillis()
        seen.entries.removeIf { now - it.value.seenAtMs > TTL_MS }
        val sorted = seen.values.sortedBy { it.name.lowercase() }
        onUpdate(sorted)
    }
}
