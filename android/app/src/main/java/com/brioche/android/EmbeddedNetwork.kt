package com.brioche.android

import android.content.Context
import android.net.ConnectivityManager
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.File
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.URI
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

data class TailscalePeer(
    val ip: String,
    val name: String,
    val online: Boolean,
    val reachable: Boolean,
    val sharing: Boolean,
    val busy: Boolean,
    val path: String,
)

data class TailscaleStatus(
    val ready: Boolean,
    val state: String,
    val ips: List<String>,
    val peers: List<TailscalePeer>,
    val authUrl: String,
    val message: String,
)

data class TailscaleEndpoint(
    val localHost: String,
    val localPort: Int,
    val pin: String,
    val token: String,
    val tunnelId: String,
)

/**
 * Private stdin/stdout IPC manager for the bundled Go tsnet binary (libbrioche_network.so).
 * Provides zero-config Tailscale WAN discovery and encrypted TCP/UDP tunnels without VPN permissions.
 */
class EmbeddedNetwork private constructor(private val appContext: Context) {
    companion object {
        @Volatile
        private var instance: EmbeddedNetwork? = null

        fun get(context: Context): EmbeddedNetwork {
            return instance ?: synchronized(this) {
                instance ?: EmbeddedNetwork(context.applicationContext).also { instance = it }
            }
        }

        private val ERRORS = mapOf(
            "different_account" to "仅允许连接同一 Tailscale 账号的设备，且设备不能使用标签身份",
            "peer_unavailable" to "无法联系这台设备，请确认另一端 Brioche 已启动并登录",
            "peer_not_sharing" to "对方尚未开启 Brioche 外网共享，或账号无访问权限",
            "invalid_peer" to "对方不是兼容的 Brioche 共享服务",
            "login_failed" to "无法发起登录，请检查网络后重试",
            "status_unavailable" to "内置网络暂不可用，请稍后刷新",
        )

        fun isValidLoginUrl(value: String?): Boolean {
            if (value.isNullOrBlank()) return false
            return try {
                val uri = URI(value)
                uri.scheme == "https" &&
                    uri.host == "login.tailscale.com" &&
                    (uri.port == -1 || uri.port == 443) &&
                    uri.userInfo == null
            } catch (_: Exception) {
                false
            }
        }

        fun sanitizeHostname(raw: String): String {
            val cleaned = raw.lowercase()
                .replace(Regex("[^a-z0-9-]+"), "-")
                .trim('-')
                .take(32)
            return cleaned.ifEmpty { "android" }
        }
    }

    private val folder = File(appContext.filesDir, "network")
    private val enabledMarker = File(folder, "enabled")
    private val stateDir = File(folder, "state")
    private val lock = Any()
    private val serial = AtomicInteger(0)
    private var process: Process? = null
    private var writer: BufferedWriter? = null
    private var responses = LinkedBlockingQueue<JSONObject>()

    val isEnabled: Boolean
        get() = enabledMarker.exists()

    fun binaryFile(): File {
        return File(appContext.applicationInfo.nativeLibraryDir, "libbrioche_network.so")
    }

    fun isBinaryAvailable(): Boolean {
        val bin = binaryFile()
        return bin.isFile && bin.canExecute()
    }

    private fun resolveAndroidDnsCsv(): String {
        val out = LinkedHashSet<String>()
        try {
            val cm = appContext.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager
            val net = cm?.activeNetwork
            val props = if (net != null) cm.getLinkProperties(net) else null
            props?.dnsServers?.forEach { addr ->
                val host = addr.hostAddress
                if (!host.isNullOrBlank() && !host.contains('%')) {
                    out.add(host)
                }
            }
        } catch (_: Exception) {
        }
        // Always append reliable fallback DNS servers for Android networks without /etc/resolv.conf
        out.add("223.5.5.5")
        out.add("119.29.29.29")
        out.add("1.1.1.1")
        out.add("8.8.8.8")
        return out.joinToString(",")
    }

    private fun ensureStarted() {
        val existing = process
        if (existing != null && existing.isAlive) return
        val bin = binaryFile()
        if (!bin.isFile || !bin.canExecute()) {
            throw IllegalStateException("当前架构未包含内置 Tailscale 组件（${Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown"}）")
        }
        stateDir.mkdirs()
        val hostName = sanitizeHostname("android-${Build.MODEL}")
        val dnsCsv = resolveAndroidDnsCsv()
        val pb = ProcessBuilder(
            bin.absolutePath,
            "--state-dir", stateDir.absolutePath,
            "--hostname", hostName,
            "--dns", dnsCsv,
        )
        pb.redirectErrorStream(false)
        val env = pb.environment()
        env.keys.filter { it.startsWith("TS_") || it.startsWith("TSNET_") || it.startsWith("TAILSCALE_") }
            .forEach { env.remove(it) }
        env["HOME"] = folder.absolutePath
        env["TMPDIR"] = appContext.cacheDir.absolutePath

        val proc = pb.start()
        val queue = LinkedBlockingQueue<JSONObject>()
        process = proc
        writer = BufferedWriter(OutputStreamWriter(proc.outputStream, Charsets.UTF_8))
        responses = queue

        Thread({
            try {
                BufferedReader(InputStreamReader(proc.inputStream, Charsets.UTF_8)).use { reader ->
                    while (true) {
                        val line = reader.readLine() ?: break
                        if (line.length > 1024 * 1024) break
                        try {
                            queue.put(JSONObject(line))
                        } catch (_: Exception) {
                        }
                    }
                }
            } catch (_: Exception) {
            } finally {
                queue.put(JSONObject().apply { put("__eof", true) })
            }
        }, "brioche-network-ipc").apply {
            isDaemon = true
            start()
        }
    }

    private fun request(op: String, extra: Map<String, Any> = emptyMap()): JSONObject {
        synchronized(lock) {
            ensureStarted()
            val reqId = serial.incrementAndGet()
            val waitMs = if (op == "devices") 35_000L else 25_000L
            val payload = JSONObject().apply {
                put("id", reqId)
                put("op", op)
                extra.forEach { (k, v) -> put(k, v) }
            }
            try {
                val w = writer ?: throw IllegalStateException("内置网络未就绪")
                w.write(payload.toString())
                w.write("\n")
                w.flush()
                val deadline = System.currentTimeMillis() + waitMs
                while (true) {
                    val remaining = deadline - System.currentTimeMillis()
                    if (remaining <= 0) {
                        if (op == "devices" || op == "status") {
                            throw IllegalStateException("内置网络状态轮询超时，保持现有连接")
                        }
                        terminateLocked()
                        throw IllegalStateException("内置网络响应超时，请检查网络后重试")
                    }
                    val resp = responses.poll(remaining, TimeUnit.MILLISECONDS)
                        ?: continue
                    if (resp.optBoolean("__eof", false)) {
                        terminateLocked()
                        throw IllegalStateException("内置网络模块已退出，请重试")
                    }
                    val cid = resp.optInt("id", -1)
                    if (cid == reqId) {
                        val err = resp.optString("error", "")
                        if (err.isNotEmpty()) {
                            throw IllegalStateException(ERRORS[err] ?: "外网操作失败（$err）")
                        }
                        return resp.optJSONObject("result") ?: JSONObject()
                    }
                    if (cid in 1 until reqId) {
                        continue // discard stale response from earlier poll
                    }
                }
            } catch (ex: IllegalStateException) {
                throw ex
            } catch (ex: Exception) {
                terminateLocked()
                throw IllegalStateException("内置网络通信中断：${ex.message ?: "请稍后重试"}")
            }
        }
    }

    fun status(probe: Boolean = false): TailscaleStatus {
        if (!isBinaryAvailable()) {
            return TailscaleStatus(
                ready = false,
                state = "Unavailable",
                ips = emptyList(),
                peers = emptyList(),
                authUrl = "",
                message = "内置 Tailscale 仅支持 arm64 / x86_64 设备",
            )
        }
        if (!isEnabled) {
            return TailscaleStatus(
                ready = false,
                state = "NeedsLogin",
                ips = emptyList(),
                peers = emptyList(),
                authUrl = "",
                message = "点击“登录 Tailscale”，无需另外安装客户端",
            )
        }
        return try {
            val raw = request(if (probe) "devices" else "status")
            val state = raw.optString("state", "Starting")
            val ips = parseStringArray(raw.optJSONArray("ips")).filter { !it.contains(':') }
            val ready = state == "Running" && ips.isNotEmpty()
            val peers = parsePeers(raw.optJSONArray("peers"))
            val authUrl = raw.optString("auth_url", "")
            val msg = when {
                ready -> "内置 Tailscale 已连接 · ${ips.first()}"
                state == "NeedsMachineAuth" -> "请在 Tailscale 管理页批准这台设备"
                state == "NeedsLogin" -> "请点击登录，完成浏览器授权"
                state == "Starting" -> "正在连接 Tailscale，请稍后刷新"
                else -> "内置网络正在启动（$state）"
            }
            TailscaleStatus(ready, state, ips, peers, authUrl, msg)
        } catch (ex: Exception) {
            TailscaleStatus(
                ready = false,
                state = "Unavailable",
                ips = emptyList(),
                peers = emptyList(),
                authUrl = "",
                message = ex.message ?: "内置网络暂不可用，请刷新",
            )
        }
    }

    fun login(): TailscaleStatus {
        folder.mkdirs()
        if (!enabledMarker.exists()) {
            enabledMarker.writeText("1\n")
        }
        val cur = status(probe = false)
        if (cur.ready) return cur
        request("login")
        return status(probe = false)
    }

    fun openPeer(ip: String): TailscaleEndpoint {
        val result = request("connect", mapOf("ip" to ip))
        val port = result.getInt("port")
        val pin = result.getString("pin")
        val token = result.getString("token")
        val tunnel = result.optString("tunnel", "")
        return TailscaleEndpoint("127.0.0.1", port, pin, token, tunnel)
    }

    fun openTunnel(ip: String, port: Int = 47995): Pair<Int, String> {
        val result = request("tunnel", mapOf("ip" to ip, "port" to port))
        return Pair(result.getInt("port"), result.optString("tunnel", ""))
    }

    fun closeTunnel(tunnelId: String) {
        if (tunnelId.isEmpty()) return
        try {
            request("close_tunnel", mapOf("tunnel" to tunnelId))
        } catch (_: Exception) {
        }
    }

    fun logout() {
        synchronized(lock) {
            if (isEnabled) {
                try {
                    request("logout")
                } catch (_: Exception) {
                }
                enabledMarker.delete()
            }
            terminateLocked()
        }
    }

    private fun terminateLocked() {
        try {
            writer?.close()
        } catch (_: Exception) {
        }
        writer = null
        try {
            process?.destroy()
        } catch (_: Exception) {
        }
        process = null
    }

    private fun parseStringArray(arr: JSONArray?): List<String> {
        if (arr == null) return emptyList()
        val out = mutableListOf<String>()
        for (i in 0 until arr.length()) {
            val s = arr.optString(i, "")
            if (s.isNotEmpty()) out.add(s)
        }
        return out
    }

    private fun parsePeers(arr: JSONArray?): List<TailscalePeer> {
        if (arr == null) return emptyList()
        val out = mutableListOf<TailscalePeer>()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val ip = o.optString("ip", "")
            val name = o.optString("name", ip)
            if (ip.isEmpty()) continue
            out.add(
                TailscalePeer(
                    ip = ip,
                    name = name,
                    online = o.optBoolean("online", false),
                    reachable = o.optBoolean("reachable", false),
                    sharing = o.optBoolean("sharing", false),
                    busy = o.optBoolean("busy", false),
                    path = o.optString("path", "待检测"),
                )
            )
        }
        return out
    }
}
