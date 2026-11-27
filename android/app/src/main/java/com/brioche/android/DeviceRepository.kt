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
) {
    val key: String get() = "$ip:$port"

    fun toJson(): JSONObject = JSONObject().apply {
        put("ip", ip)
        put("port", port)
        put("name", name)
        put("platform", platform)
        put("pin", pin)
        put("token", token)
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
            )
        }
    }
}

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

    fun saveStreamConfig(resolution: String, fps: Int, bitrateMbps: Int) {
        prefs.edit()
            .putString("resolution", resolution)
            .putInt("fps", fps)
            .putInt("bitrate_mbps", bitrateMbps)
            .apply()
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
        return out
    }

    fun upsert(device: SavedDevice) {
        val current = listDevices().toMutableList()
        val idx = current.indexOfFirst { it.pin == device.pin || (it.ip == device.ip && it.port == device.port) }
        if (idx >= 0) {
            val prev = current[idx]
            current[idx] = device.copy(
                token = device.token.ifEmpty { prev.token },
                name = device.name.ifEmpty { prev.name },
            )
        } else {
            current.add(0, device)
        }
        persist(current)
    }

    fun remove(ip: String, port: Int) {
        val filtered = listDevices().filterNot { it.ip == ip && it.port == port }
        persist(filtered)
    }

    private fun persist(devices: List<SavedDevice>) {
        val arr = JSONArray()
        devices.forEach { arr.put(it.toJson()) }
        prefs.edit().putString("devices_json", arr.toString()).apply()
    }
}
