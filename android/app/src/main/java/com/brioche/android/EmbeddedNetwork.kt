package com.brioche.android

import android.content.Context
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedReader
import java.io.DataInputStream
import java.io.File
import java.io.InputStreamReader
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger

data class EasyTierPeer(
    val ip: String,
    val name: String,
    val online: Boolean,
    val reachable: Boolean,
    val sharing: Boolean,
    val busy: Boolean,
    val path: String,
)

data class EasyTierStatus(
    val ready: Boolean,
    val state: String,
    val ips: List<String>,
    val peers: List<EasyTierPeer>,
    val networkName: String,
    val message: String,
)

data class EasyTierEndpoint(
    val localHost: String,
    val localPort: Int,
    val pin: String,
    val token: String,
    val tunnelId: String,
)

private class LocalSocksBridge(
    private val socksPort: Int,
    private val targetIp: String,
    private val targetPort: Int,
) {
    private val stopped = AtomicBoolean(false)
    private val server = ServerSocket(0, 16, InetAddress.getByName("127.0.0.1"))
    val localPort: Int = server.localPort

    init {
        Thread({
            while (!stopped.get()) {
                val client = try {
                    server.accept()
                } catch (_: Exception) {
                    break
                }
                Thread({ relay(client) }, "brioche-et-relay").apply {
                    isDaemon = true
                    start()
                }
            }
        }, "brioche-et-accept-$localPort").apply {
            isDaemon = true
            start()
        }
    }

    private fun relay(a: Socket) {
        var b: Socket? = null
        try {
            a.tcpNoDelay = true
            a.keepAlive = true
            b = EmbeddedNetwork.socks5Connect(socksPort, targetIp, targetPort, timeoutMs = 8000)
            b.tcpNoDelay = true
            b.keepAlive = true
            val upstream = b
            val t1 = Thread({ copyStream(a, upstream) }, "brioche-et-up").apply { isDaemon = true; start() }
            val t2 = Thread({ copyStream(upstream, a) }, "brioche-et-dn").apply { isDaemon = true; start() }
            t1.join()
            t2.join()
        } catch (_: Exception) {
        } finally {
            try { a.close() } catch (_: Exception) {}
            try { b?.close() } catch (_: Exception) {}
        }
    }

    private fun copyStream(src: Socket, dst: Socket) {
        val buf = ByteArray(65536)
        try {
            val inp = src.getInputStream()
            val out = dst.getOutputStream()
            while (true) {
                val n = inp.read(buf)
                if (n <= 0) break
                out.write(buf, 0, n)
                out.flush()
            }
        } catch (_: Exception) {
        } finally {
            try { dst.shutdownOutput() } catch (_: Exception) {}
        }
    }

    fun close() {
        stopped.set(true)
        try { server.close() } catch (_: Exception) {}
    }
}

/**
 * Embedded EasyTier v2.6.4 userspace P2P mesh controller (zero root, zero TUN, zero VPN permission).
 * Connects directly to Brioche desktop hosts via P2P UDP/QUIC/KCP or low-latency community/custom relays.
 */
class EmbeddedNetwork private constructor(private val appContext: Context) {
    companion object {
        const val DISCOVERY_PORT = 47984
        const val STREAM_PORT = 47985

        val DEFAULT_PEERS = listOf(
            "tcp://183.230.36.171:11010",
            "tcp://easytier.weiai.org.cn:11010",
            "tcp://et.sumnight.top:11010",
            "tcp://225284.xyz:11010",
        )

        private val PEER_URL_REGEX = Regex("^(tcp|udp|quic|ws|wss|wg|faketcp)://\\S+$", RegexOption.IGNORE_CASE)
        private val PIN_REGEX = Regex("^[a-f0-9]{64}$")
        private val TOKEN_REGEX = Regex("^[A-Za-z0-9_-]{43}$")

        @Volatile
        private var instance: EmbeddedNetwork? = null

        fun get(context: Context): EmbeddedNetwork {
            return instance ?: synchronized(this) {
                instance ?: EmbeddedNetwork(context.applicationContext).also { instance = it }
            }
        }

        fun parsePeerUrls(raw: String?): List<String> {
            val list = raw.orEmpty()
                .split(Regex("[\\s,;]+"))
                .map { it.trim() }
                .filter { PEER_URL_REGEX.matches(it) }
                .distinct()
            return if (list.isEmpty()) DEFAULT_PEERS else list
        }

        fun deriveNodeIps(machineId: String): Pair<String, String> {
            val digest = MessageDigest.getInstance("SHA-256").digest(machineId.toByteArray(Charsets.UTF_8))
            val a = ((digest[0].toInt() and 0xFF) % 250) + 1
            val b = ((digest[1].toInt() and 0xFF) % 250) + 1
            return Pair("10.144.$a.$b", "10.145.$a.$b")
        }

        fun sanitizeHostname(raw: String): String {
            val cleaned = raw.lowercase()
                .replace(Regex("[^a-z0-9-]+"), "-")
                .trim('-')
                .take(32)
            return cleaned.ifEmpty { "android" }
        }

        fun socks5Connect(socksPort: Int, dstIp: String, dstPort: Int, timeoutMs: Int = 6000): Socket {
            val sock = Socket()
            sock.connect(InetSocketAddress("127.0.0.1", socksPort), timeoutMs)
            sock.soTimeout = timeoutMs
            try {
                val out = sock.getOutputStream()
                val inp = DataInputStream(BufferedInputStream(sock.getInputStream()))
                out.write(byteArrayOf(0x05, 0x01, 0x00))
                out.flush()
                val v = inp.readByte().toInt()
                val m = inp.readByte().toInt()
                if (v != 0x05 || m != 0x00) {
                    throw IllegalStateException("EasyTier SOCKS5 握手失败")
                }
                val ipBytes = InetAddress.getByName(dstIp).address
                val req = ByteArray(10)
                req[0] = 0x05; req[1] = 0x01; req[2] = 0x00; req[3] = 0x01
                System.arraycopy(ipBytes, 0, req, 4, 4)
                req[8] = ((dstPort shr 8) and 0xFF).toByte()
                req[9] = (dstPort and 0xFF).toByte()
                out.write(req)
                out.flush()
                val head = ByteArray(4)
                inp.readFully(head)
                if (head[1].toInt() != 0) {
                    throw IllegalStateException("EasyTier 目标不可达 (code=${head[1].toInt()})")
                }
                when (head[3].toInt()) {
                    1 -> inp.readFully(ByteArray(6))
                    3 -> {
                        val len = inp.readUnsignedByte()
                        inp.readFully(ByteArray(len + 2))
                    }
                    4 -> inp.readFully(ByteArray(18))
                }
                sock.soTimeout = 0
                return sock
            } catch (ex: Exception) {
                try { sock.close() } catch (_: Exception) {}
                throw ex
            }
        }

        fun socks5HttpGetJson(socksPort: Int, dstIp: String, dstPort: Int, path: String, timeoutMs: Int = 2500): Pair<Int, JSONObject> {
            val sock = socks5Connect(socksPort, dstIp, dstPort, timeoutMs)
            sock.use { s ->
                s.soTimeout = timeoutMs
                val req = "GET $path HTTP/1.1\r\nHost: $dstIp:$dstPort\r\nConnection: close\r\n\r\n"
                s.getOutputStream().write(req.toByteArray(Charsets.US_ASCII))
                s.getOutputStream().flush()
                val raw = s.getInputStream().readBytes().toString(Charsets.UTF_8)
                val idx = raw.indexOf("\r\n\r\n")
                if (idx < 0) return Pair(0, JSONObject())
                val statusLine = raw.substring(0, idx).lineSequence().firstOrNull().orEmpty()
                val code = statusLine.split(" ").getOrNull(1)?.toIntOrNull() ?: 0
                val body = raw.substring(idx + 4)
                if (code != 200) return Pair(code, JSONObject())
                return Pair(code, JSONObject(body))
            }
        }
    }

    private val prefs = appContext.getSharedPreferences("brioche_easytier_v1", Context.MODE_PRIVATE)
    private val lock = Any()
    private val bridges = ConcurrentHashMap<String, LocalSocksBridge>()
    private val bridgeSeq = AtomicInteger(0)
    private var process: Process? = null
    private var rpcPort: Int = 0
    private var socksPort: Int = 0
    private var virtualIp: String = ""
    @Volatile
    private var lastStderr: String = ""

    val isEnabled: Boolean
        get() = networkName().isNotEmpty() && networkSecret().isNotEmpty()

    fun networkName(): String = prefs.getString("network_name", "") ?: ""
    fun networkSecret(): String = prefs.getString("network_secret", "") ?: ""
    fun peersCsv(): String = prefs.getString("peers_csv", DEFAULT_PEERS.joinToString(", ")) ?: DEFAULT_PEERS.joinToString(", ")

    private fun machineId(): String {
        val existing = prefs.getString("machine_id", null)
        if (!existing.isNullOrBlank()) return existing
        val created = UUID.randomUUID().toString()
        prefs.edit().putString("machine_id", created).apply()
        return created
    }

    fun coreBinaryFile(): File = File(appContext.applicationInfo.nativeLibraryDir, "libeasytier_core.so")
    fun cliBinaryFile(): File = File(appContext.applicationInfo.nativeLibraryDir, "libeasytier_cli.so")

    fun isBinaryAvailable(): Boolean {
        val core = coreBinaryFile()
        val cli = cliBinaryFile()
        return core.isFile && core.canExecute() && cli.isFile && cli.canExecute()
    }

    private fun findFreeTcpPort(): Int {
        ServerSocket(0, 1, InetAddress.getByName("127.0.0.1")).use { return it.localPort }
    }

    private fun ensureStarted() {
        val existing = process
        if (existing != null && existing.isAlive) return
        val netName = networkName()
        val netSecret = networkSecret()
        if (netName.isEmpty() || netSecret.isEmpty()) {
            throw IllegalStateException("尚未登录 EasyTier 网络")
        }
        if (!isBinaryAvailable()) {
            throw IllegalStateException("当前架构未包含内置 EasyTier 组件（${Build.SUPPORTED_ABIS.firstOrNull() ?: "unknown"}）")
        }
        rpcPort = findFreeTcpPort()
        socksPort = findFreeTcpPort()
        val mid = machineId()
        val (vip, _) = deriveNodeIps(mid)
        virtualIp = vip
        val hostName = sanitizeHostname("android-${Build.MODEL}")
        val cmd = mutableListOf(
            coreBinaryFile().absolutePath,
            "--no-tun",
            "--use-smoltcp",
            "-i", "$vip/16",
            "--hostname", "brioche-$hostName",
            "--machine-id", mid,
            "--network-name", netName,
            "--network-secret", netSecret,
            "--latency-first",
            "--enable-kcp-proxy",
            "--socks5", socksPort.toString(),
            "--rpc-portal", "127.0.0.1:$rpcPort",
            "--disable-env-parsing",
        )
        parsePeerUrls(peersCsv()).forEach { peerUrl ->
            cmd.add("-p")
            cmd.add(peerUrl)
        }
        val pb = ProcessBuilder(cmd)
        pb.redirectErrorStream(true)
        val env = pb.environment()
        env.keys.filter { it.startsWith("ET_") }.forEach { env.remove(it) }
        env["HOME"] = appContext.filesDir.absolutePath
        env["TMPDIR"] = appContext.cacheDir.absolutePath

        lastStderr = ""
        val proc = pb.start()
        process = proc
        Thread({
            try {
                BufferedReader(InputStreamReader(proc.inputStream, Charsets.UTF_8)).use { reader ->
                    val lines = ArrayDeque<String>()
                    while (true) {
                        val line = reader.readLine() ?: break
                        val t = line.trim()
                        if (t.isNotEmpty()) {
                            lines.addLast(t)
                            while (lines.size > 4) lines.removeFirst()
                            lastStderr = lines.joinToString(" | ").take(240)
                        }
                    }
                }
            } catch (_: Exception) {
            }
        }, "brioche-et-log").apply {
            isDaemon = true
            start()
        }

        val deadline = System.currentTimeMillis() + 5000L
        while (System.currentTimeMillis() < deadline) {
            if (!proc.isAlive) {
                val code = proc.exitValue()
                terminateLocked()
                throw IllegalStateException("EasyTier 启动失败 (exit=$code): $lastStderr")
            }
            try {
                Socket().use { s ->
                    s.connect(InetSocketAddress("127.0.0.1", rpcPort), 200)
                }
                return
            } catch (_: Exception) {
                Thread.sleep(80)
            }
        }
    }

    private fun runCliJson(subCmd: String): JSONArray {
        val pb = ProcessBuilder(
            cliBinaryFile().absolutePath,
            "-p", "127.0.0.1:$rpcPort",
            "-o", "json",
            subCmd,
        )
        pb.redirectErrorStream(false)
        val proc = pb.start()
        val stdout = proc.inputStream.bufferedReader(Charsets.UTF_8).readText()
        val ok = proc.waitFor(5, TimeUnit.SECONDS)
        if (!ok) {
            proc.destroyForcibly()
            throw IllegalStateException("EasyTier CLI 查询超时")
        }
        if (proc.exitValue() != 0) {
            val err = proc.errorStream.bufferedReader(Charsets.UTF_8).readText().trim()
            throw IllegalStateException(err.ifEmpty { "EasyTier CLI 返回 ${proc.exitValue()}" })
        }
        return JSONArray(stdout.trim().ifEmpty { "[]" })
    }

    fun login(networkName: String, networkSecret: String, peersRaw: String): EasyTierStatus {
        val cleanName = networkName.trim().take(64)
        val cleanSecret = networkSecret.trim().take(64)
        if (cleanName.length < 2 || cleanSecret.length < 4) {
            throw IllegalArgumentException("请填写网络名称（至少 2 位）和网络密钥（至少 4 位）")
        }
        val cleanPeers = parsePeerUrls(peersRaw).joinToString(", ")
        synchronized(lock) {
            terminateLocked()
            prefs.edit()
                .putString("network_name", cleanName)
                .putString("network_secret", cleanSecret)
                .putString("peers_csv", cleanPeers)
                .apply()
            ensureStarted()
        }
        return status(probe = true)
    }

    fun status(probe: Boolean = false): EasyTierStatus {
        if (!isBinaryAvailable()) {
            return EasyTierStatus(
                ready = false,
                state = "Unavailable",
                ips = emptyList(),
                peers = emptyList(),
                networkName = "",
                message = "内置 EasyTier 仅支持 arm64 / x86_64 设备",
            )
        }
        if (!isEnabled) {
            return EasyTierStatus(
                ready = false,
                state = "NeedsLogin",
                ips = emptyList(),
                peers = emptyList(),
                networkName = "",
                message = "点击“登录 EasyTier”，输入网络名与密钥即可全球直连",
            )
        }
        return try {
            val sp: Int
            val vip: String
            synchronized(lock) {
                ensureStarted()
                sp = socksPort
                vip = virtualIp
            }
            val routes = runCliJson("route")
            val peerRows = runCliJson("peer")
            val peerByV4 = HashMap<String, JSONObject>()
            for (i in 0 until peerRows.length()) {
                val o = peerRows.optJSONObject(i) ?: continue
                val ip = o.optString("ipv4", "").substringBefore('/')
                if (ip.isNotEmpty()) peerByV4[ip] = o
            }

            val peers = mutableListOf<EasyTierPeer>()
            for (i in 0 until routes.length()) {
                val r = routes.optJSONObject(i) ?: continue
                val hostname = r.optString("hostname", "")
                if (r.optString("next_hop_hostname") == "Local" || !hostname.startsWith("brioche-")) continue
                val proxyCidrs = r.optString("proxy_cidrs", "")
                val targetIp = proxyCidrs.split(Regex("[\\s,]+"))
                    .map { it.substringBefore('/').trim() }
                    .firstOrNull { it.startsWith("10.145.") }
                    ?: continue
                val v4 = r.optString("ipv4", "").substringBefore('/')
                val prow = peerByV4[v4]
                val cost = prow?.optString("cost", "")?.lowercase().orEmpty()
                val lat = prow?.optString("lat_ms", "")?.trim().orEmpty()
                val proto = prow?.optString("tunnel_proto", "")?.trim().orEmpty()
                val isP2p = cost == "p2p" || r.optString("next_hop_hostname").equals("DIRECT", ignoreCase = true)
                val latTxt = if (lat.isNotEmpty() && lat != "-") " · ${lat}ms" else ""
                val protoTxt = if (proto.isNotEmpty() && proto != "-") " (${proto.uppercase()})" else ""
                val pathLabel = if (isP2p) "P2P 直连$latTxt$protoTxt" else "EasyTier 中继$latTxt"
                peers.add(
                    EasyTierPeer(
                        ip = targetIp,
                        name = hostname.removePrefix("brioche-").ifEmpty { targetIp },
                        online = true,
                        reachable = false,
                        sharing = false,
                        busy = false,
                        path = pathLabel,
                    )
                )
            }

            val finalPeers = if (probe && peers.isNotEmpty()) {
                val pool = Executors.newFixedThreadPool(minOf(6, peers.size))
                try {
                    val futures = peers.map { p ->
                        pool.submit<EasyTierPeer> {
                            try {
                                val (code, json) = socks5HttpGetJson(sp, p.ip, DISCOVERY_PORT, "/presence", timeoutMs = 2000)
                                if (code == 200 && json.optString("product").startsWith("Brioche/")) {
                                    p.copy(
                                        reachable = true,
                                        sharing = json.optBoolean("sharing", false),
                                        busy = json.optBoolean("busy", false),
                                    )
                                } else p
                            } catch (_: Exception) {
                                p
                            }
                        }
                    }
                    futures.map { it.get(3, TimeUnit.SECONDS) }
                } catch (_: Exception) {
                    peers
                } finally {
                    pool.shutdownNow()
                }
            } else {
                peers
            }

            val netName = networkName()
            EasyTierStatus(
                ready = true,
                state = "Running",
                ips = listOf(vip),
                peers = finalPeers,
                networkName = netName,
                message = "EasyTier 已连接 · $netName ($vip)",
            )
        } catch (ex: Exception) {
            EasyTierStatus(
                ready = false,
                state = "Unavailable",
                ips = emptyList(),
                peers = emptyList(),
                networkName = networkName(),
                message = ex.message ?: "EasyTier 暂不可用，请重试",
            )
        }
    }

    fun openPeer(proxyIp: String): EasyTierEndpoint {
        val sp = synchronized(lock) {
            ensureStarted()
            socksPort
        }
        val (code, info) = try {
            socks5HttpGetJson(sp, proxyIp, DISCOVERY_PORT, "/info", timeoutMs = 6000)
        } catch (ex: Exception) {
            throw IllegalStateException("无法联系该 EasyTier 设备（${ex.message ?: "请确认对方已开启外网共享"}）")
        }
        if (code == 409) throw IllegalStateException("对方尚未开启 Brioche 外网共享")
        if (code != 200 || !info.optString("product").startsWith("Brioche/")) {
            throw IllegalStateException("对方不是兼容的 Brioche 共享服务")
        }
        val pin = info.optString("pin", "")
        val token = info.optString("token", "")
        val remotePort = info.optInt("port", STREAM_PORT)
        if (!PIN_REGEX.matches(pin) || !TOKEN_REGEX.matches(token)) {
            throw IllegalStateException("对方返回的设备凭据无效")
        }
        val (localPort, tid) = openTunnel(proxyIp, remotePort)
        return EasyTierEndpoint("127.0.0.1", localPort, pin, token, tid)
    }

    fun openTunnel(proxyIp: String, remotePort: Int = STREAM_PORT): Pair<Int, String> {
        val sp = synchronized(lock) {
            ensureStarted()
            socksPort
        }
        val bridge = LocalSocksBridge(sp, proxyIp, remotePort)
        val tid = "et-${bridgeSeq.incrementAndGet()}"
        bridges[tid] = bridge
        return Pair(bridge.localPort, tid)
    }

    fun closeTunnel(tunnelId: String) {
        if (tunnelId.isEmpty()) return
        bridges.remove(tunnelId)?.close()
    }

    fun logout() {
        synchronized(lock) {
            bridges.values.forEach { it.close() }
            bridges.clear()
            terminateLocked()
            prefs.edit()
                .remove("network_name")
                .remove("network_secret")
                .apply()
        }
    }

    private fun terminateLocked() {
        try {
            process?.destroy()
        } catch (_: Exception) {
        }
        process = null
    }
}
