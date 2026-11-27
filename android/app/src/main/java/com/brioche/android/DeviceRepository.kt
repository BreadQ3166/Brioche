package com.brioche.android

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

data class SavedDevice(
    val ip: String,
    val port: Int,
    val name: String,
    val platform: String,
    val pin: String,
    val token: String,
    val alias: String = "",
    val favorite: Boolean = false,
    val lanOnly: Boolean = false,
    val customResolution: String = "",
    val customFps: Int = 0,
    val customBitrate: Int = 0,
    val lastConnectedAt: Long = 0L,
    val tailscaleIp: String = "",
) {
    val key: String get() = "$ip:$port"
    val displayName: String get() = alias.ifBlank { name.ifBlank { ip } }

    fun toJson(): JSONObject = JSONObject().apply {
        put("ip", ip)
        put("port", port)
        put("name", name)
        put("platform", platform)
        put("pin", pin)
        put("token", token)
        put("alias", alias)
        put("favorite", favorite)
        put("lan_only", lanOnly)
        put("custom_resolution", customResolution)
        put("custom_fps", customFps)
        put("custom_bitrate", customBitrate)
        put("last_connected_at", lastConnectedAt)
        put("tailscale_ip", tailscaleIp)
    }

    companion object {
        fun fromJson(obj: JSONObject): SavedDevice? {
            val ip = obj.optString("ip", "")
            val port = obj.optInt("port", BriocheProtocol.DEFAULT_PORT)
            val pin = obj.optString("pin", "")
            val token = obj.optString("token", "")
            if (ip.isEmpty() || pin.isEmpty()) return null
            return SavedDevice(
                ip = ip,
                port = port,
                name = obj.optString("name", ip),
                platform = obj.optString("platform", "Desktop"),
                pin = pin,
                token = token,
                alias = obj.optString("alias", ""),
                favorite = obj.optBoolean("favorite", false),
                lanOnly = obj.optBoolean("lan_only", false),
                customResolution = obj.optString("custom_resolution", ""),
                customFps = obj.optInt("custom_fps", 0),
                customBitrate = obj.optInt("custom_bitrate", 0),
                lastConnectedAt = obj.optLong("last_connected_at", 0L),
                tailscaleIp = obj.optString("tailscale_ip", ""),
            )
        }
    }
}

data class RecentConnection(
    val id: String,
    val name: String,
    val mode: String, // "lan" or "tailscale"
    val address: String,
    val port: Int,
    val pin: String,
    val token: String,
    val connectedAtMs: Long,
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("id", id)
        put("name", name)
        put("mode", mode)
        put("address", address)
        put("port", port)
        put("pin", pin)
        put("token", token)
        put("connected_at", connectedAtMs)
    }

    companion object {
        fun fromJson(obj: JSONObject): RecentConnection? {
            val id = obj.optString("id", "")
            val addr = obj.optString("address", "")
            if (id.isEmpty() || addr.isEmpty()) return null
            return RecentConnection(
                id = id,
                name = obj.optString("name", addr),
                mode = obj.optString("mode", "lan"),
                address = addr,
                port = obj.optInt("port", BriocheProtocol.DEFAULT_PORT),
                pin = obj.optString("pin", ""),
                token = obj.optString("token", ""),
                connectedAtMs = obj.optLong("connected_at", 0L),
            )
        }
    }
}

data class PeerMeta(
    val alias: String = "",
    val favorite: Boolean = false,
    val lanOnly: Boolean = false,
    val customResolution: String = "",
    val customFps: Int = 0,
    val customBitrate: Int = 0,
)

class DeviceRepository(context: Context) {
    private val prefs = context.getSharedPreferences("brioche_devices_v1", Context.MODE_PRIVATE)

    fun clientId(): String {
        val existing = prefs.getString("client_id", null)
        if (!existing.isNullOrEmpty() && existing.length == 32) return existing
        val created = BriocheProtocol.randomHex(16)
        prefs.edit().putString("client_id", created).apply()
        return created
    }

    fun resolution(): String = prefs.getString("resolution", "1080p") ?: "1080p"
    fun fps(): Int = prefs.getInt("fps", 60)
    fun bitrateMbps(): Int = prefs.getInt("bitrate_mbps", 16)
    fun audioEnabled(): Boolean = prefs.getBoolean("audio_enabled", true)
    fun defaultTrackpadMode(): Boolean = prefs.getBoolean("trackpad_mode", true)
    fun themeMode(): String = prefs.getString("theme_mode", "system") ?: "system"
    fun transferRateBps(): Long = prefs.getLong("transfer_rate_bps", 20L * 1024L * 1024L)

    fun saveStreamConfig(resolution: String, fps: Int, bitrateMbps: Int) {
        prefs.edit()
            .putString("resolution", resolution)
            .putInt("fps", fps)
            .putInt("bitrate_mbps", bitrateMbps)
            .apply()
    }

    fun setAudioEnabled(enabled: Boolean) {
        prefs.edit().putBoolean("audio_enabled", enabled).apply()
    }

    fun setDefaultTrackpadMode(trackpad: Boolean) {
        prefs.edit().putBoolean("trackpad_mode", trackpad).apply()
    }

    fun setThemeMode(mode: String) {
        prefs.edit().putString("theme_mode", mode).apply()
    }

    fun setTransferRateBps(bps: Long) {
        prefs.edit().putLong("transfer_rate_bps", bps).apply()
    }

    fun effectiveConfigFor(device: SavedDevice?): BriocheProtocol.StreamConfig {
        val res = device?.customResolution?.takeIf { it.isNotEmpty() } ?: resolution()
        val f = device?.customFps?.takeIf { it > 0 } ?: fps()
        val mbps = device?.customBitrate?.takeIf { it > 0 } ?: bitrateMbps()
        return BriocheProtocol.StreamConfig(res, f, mbps)
    }

    fun effectiveConfigForPeer(tsIp: String): BriocheProtocol.StreamConfig {
        val meta = getPeerMeta(tsIp)
        val res = meta.customResolution.ifEmpty { resolution() }
        val f = if (meta.customFps > 0) meta.customFps else fps()
        val mbps = if (meta.customBitrate > 0) meta.customBitrate else bitrateMbps()
        return BriocheProtocol.StreamConfig(res, f, mbps)
    }

    fun getPeerMeta(tsIp: String): PeerMeta {
        val all = JSONObject(prefs.getString("ts_peer_meta_json", "{}") ?: "{}")
        val o = all.optJSONObject(tsIp) ?: return PeerMeta()
        return PeerMeta(
            alias = o.optString("alias", ""),
            favorite = o.optBoolean("favorite", false),
            lanOnly = o.optBoolean("lan_only", false),
            customResolution = o.optString("custom_resolution", ""),
            customFps = o.optInt("custom_fps", 0),
            customBitrate = o.optInt("custom_bitrate", 0),
        )
    }

    fun savePeerMeta(tsIp: String, meta: PeerMeta) {
        val all = JSONObject(prefs.getString("ts_peer_meta_json", "{}") ?: "{}")
        all.put(tsIp, JSONObject().apply {
            put("alias", meta.alias)
            put("favorite", meta.favorite)
            put("lan_only", meta.lanOnly)
            put("custom_resolution", meta.customResolution)
            put("custom_fps", meta.customFps)
            put("custom_bitrate", meta.customBitrate)
        })
        prefs.edit().putString("ts_peer_meta_json", all.toString()).apply()
    }

    fun listDevices(): List<SavedDevice> {
        val raw = prefs.getString("devices_json", "[]") ?: "[]"
        val arr = try {
            JSONArray(raw)
        } catch (_: Exception) {
            JSONArray()
        }
        val out = mutableListOf<SavedDevice>()
        for (i in 0 until arr.length()) {
            val obj = arr.optJSONObject(i) ?: continue
            SavedDevice.fromJson(obj)?.let { out.add(it) }
        }
        return out.sortedWith(compareByDescending<SavedDevice> { it.favorite }.thenByDescending { it.lastConnectedAt }.thenBy { it.displayName.lowercase() })
    }

    fun upsert(device: SavedDevice) {
        val current = listDevices().toMutableList()
        val idx = current.indexOfFirst { it.pin == device.pin || (it.ip == device.ip && it.port == device.port) }
        if (idx >= 0) {
            val prev = current[idx]
            current[idx] = device.copy(
                token = device.token.ifEmpty { prev.token },
                name = device.name.ifEmpty { prev.name },
                alias = device.alias.ifEmpty { prev.alias },
                favorite = device.favorite || prev.favorite,
                lanOnly = device.lanOnly || prev.lanOnly,
                customResolution = device.customResolution.ifEmpty { prev.customResolution },
                customFps = if (device.customFps > 0) device.customFps else prev.customFps,
                customBitrate = if (device.customBitrate > 0) device.customBitrate else prev.customBitrate,
                lastConnectedAt = maxOf(device.lastConnectedAt, prev.lastConnectedAt),
                tailscaleIp = device.tailscaleIp.ifEmpty { prev.tailscaleIp },
            )
        } else {
            current.add(0, device)
        }
        persist(current)
    }

    fun replaceExact(device: SavedDevice) {
        val current = listDevices().toMutableList()
        val idx = current.indexOfFirst { it.pin == device.pin || (it.ip == device.ip && it.port == device.port) }
        if (idx >= 0) {
            current[idx] = device
        } else {
            current.add(0, device)
        }
        persist(current)
    }

    fun remove(ip: String, port: Int) {
        val filtered = listDevices().filterNot { it.ip == ip && it.port == port }
        persist(filtered)
    }

    fun clearAllDevices() {
        prefs.edit().remove("devices_json").apply()
    }

    fun recordRecent(entry: RecentConnection) {
        val current = listRecents().toMutableList()
        current.removeAll { it.id == entry.id }
        current.add(0, entry)
        while (current.size > 20) current.removeAt(current.lastIndex)
        val arr = JSONArray()
        current.forEach { arr.put(it.toJson()) }
        prefs.edit().putString("recents_json", arr.toString()).apply()
    }

    fun listRecents(): List<RecentConnection> {
        val raw = prefs.getString("recents_json", "[]") ?: "[]"
        val arr = try {
            JSONArray(raw)
        } catch (_: Exception) {
            JSONArray()
        }
        val out = mutableListOf<RecentConnection>()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            RecentConnection.fromJson(o)?.let { out.add(it) }
        }
        return out
    }

    fun clearRecents() {
        prefs.edit().remove("recents_json").apply()
    }

    private fun persist(devices: List<SavedDevice>) {
        val arr = JSONArray()
        devices.forEach { arr.put(it.toJson()) }
        prefs.edit().putString("devices_json", arr.toString()).apply()
    }
}
