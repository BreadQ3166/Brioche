package com.brioche.android

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.EOFException
import java.io.IOException
import java.math.BigInteger
import java.net.InetSocketAddress
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSocket
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager

object BriocheProtocol {
    const val VERSION = 2
    const val CONTROL: Byte = 1
    const val VIDEO: Byte = 2
    const val FILE: Byte = 3
    const val AUDIO: Byte = 4
    const val DEFAULT_PORT = 47995

    const val MAX_CONTROL = 256 * 1024
    const val MAX_VIDEO = 8 * 1024 * 1024
    const val MAX_FILE_CHUNK = 256 * 1024
    const val MAX_AUDIO = 16 * 1024

    private val PIN_REGEX = Regex("^[a-f0-9]{64}$")
    private val TOKEN_REGEX = Regex("^[A-Za-z0-9_-]{43}$")
    private val IPV4_PRIVATE = listOf(
        Pair("10.", true),
        Pair("192.168.", true),
        Pair("127.", true),
    )

    data class AccessEndpoint(
        val host: String,
        val port: Int,
        val pin: String,
        val token: String,
    )

    data class StreamConfig(
        val resolution: String = "1080p",
        val fps: Int = 60,
        val bitrateMbps: Int = 16,
    ) {
        fun toJson(): JSONObject = JSONObject().apply {
            put("resolution", resolution)
            put("fps", fps)
            put("bitrate_mbps", bitrateMbps)
        }
    }

    data class MonitorInfo(
        val id: Int,
        val name: String,
        val width: Int,
        val height: Int,
    )

    data class WireMessage(
        val kind: Byte,
        val payload: ByteArray,
    )

    fun parseLanTarget(raw: String): Pair<String, Int> {
        val text = raw.trim()
        if (text.isEmpty() || text.count { it == ':' } > 1) {
            throw IllegalArgumentException("请填写局域网 IPv4，例如 192.168.1.8 或 10.27.56.8:47995")
        }
        val parts = text.split(':')
        val host = parts[0].trim()
        val port = if (parts.size == 2) {
            parts[1].trim().toIntOrNull()
                ?: throw IllegalArgumentException("端口无效")
        } else {
            DEFAULT_PORT
        }
        if (port !in 1..65535) {
            throw IllegalArgumentException("端口必须在 1–65535 之间")
        }
        val octets = host.split('.')
        if (octets.size != 4 || octets.any { it.toIntOrNull() !in 0..255 }) {
            throw IllegalArgumentException("请填写有效的 IPv4 地址")
        }
        val o0 = octets[0].toInt()
        val o1 = octets[1].toInt()
        val isLan = o0 == 10 || (o0 == 172 && o1 in 16..31) || (o0 == 192 && o1 == 168) || o0 == 127 || (o0 == 100 && o1 in 64..127)
        if (!isLan) {
            throw IllegalArgumentException("仅支持局域网私有 IPv4 或 Tailscale IP (100.64.0.0/10)")
        }
        return Pair(host, port)
    }

    fun parseUri(uri: String): AccessEndpoint {
        val trimmed = uri.trim()
        if (!trimmed.startsWith("brioche://") || trimmed.length > 2048) {
            throw IllegalArgumentException("连接凭据必须以 brioche:// 开头")
        }
        val withoutScheme = trimmed.removePrefix("brioche://")
        val hashIdx = withoutScheme.indexOf('#')
        if (hashIdx <= 0) {
            throw IllegalArgumentException("连接凭据缺少证书指纹与令牌")
        }
        val authority = withoutScheme.substring(0, hashIdx)
        val fragment = withoutScheme.substring(hashIdx + 1)
        val fragParts = fragment.split('.')
        if (fragParts.size != 2) {
            throw IllegalArgumentException("连接凭据格式错误")
        }
        val pin = fragParts[0].lowercase()
        val token = fragParts[1]
        if (!PIN_REGEX.matches(pin) || !TOKEN_REGEX.matches(token)) {
            throw IllegalArgumentException("连接凭据中的指纹或令牌无效")
        }
        val colonIdx = authority.lastIndexOf(':')
        if (colonIdx <= 0) {
            throw IllegalArgumentException("连接凭据缺少端口号")
        }
        val host = authority.substring(0, colonIdx).trim('[', ']')
        val port = authority.substring(colonIdx + 1).toIntOrNull()
            ?: throw IllegalArgumentException("端口号无效")
        if (host.isEmpty() || port !in 1..65535) {
            throw IllegalArgumentException("主机或端口无效")
        }
        return AccessEndpoint(host, port, pin, token)
    }

    fun formatUri(host: String, port: Int, pin: String, token: String): String {
        val h = if (host.contains(':')) "[$host]" else host
        return "brioche://$h:$port#$pin.$token"
    }

    /** Exact parity with Python `lan.py:pairing_code(pin, nonce, client_id)`. */
    fun pairingCode(pin: String, nonce: String, clientId: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest("Brioche-LAN-pair-v1:$pin:$nonce:$clientId".toByteArray(Charsets.UTF_8))
        val unsigned8 = BigInteger(1, digest.copyOfRange(0, 8))
        val code = unsigned8.mod(BigInteger.valueOf(1_000_000L)).toInt()
        return String.format("%06d", code)
    }

    fun randomHex(bytes: Int): String {
        val raw = ByteArray(bytes)
        SecureRandom().nextBytes(raw)
        return raw.joinToString("") { "%02x".format(it) }
    }

    fun packFrame(kind: Byte, payload: ByteArray): ByteArray {
        val buf = ByteBuffer.allocate(5 + payload.size).order(ByteOrder.BIG_ENDIAN)
        buf.put(kind)
        buf.putInt(payload.size)
        buf.put(payload)
        return buf.array()
    }

    fun packControl(obj: JSONObject): ByteArray {
        val bytes = obj.toString().toByteArray(Charsets.UTF_8)
        if (bytes.size > MAX_CONTROL) {
            throw IOException("控制消息过长")
        }
        return packFrame(CONTROL, bytes)
    }

    @Throws(IOException::class)
    fun readFrame(input: DataInputStream): WireMessage {
        val kind = input.readByte()
        val size = input.readInt()
        val limit = when (kind) {
            CONTROL -> MAX_CONTROL
            VIDEO -> MAX_VIDEO
            FILE -> MAX_FILE_CHUNK
            AUDIO -> MAX_AUDIO
            else -> 0
        }
        if (size <= 0 || size > limit) {
            throw IOException("无效消息类型或长度: kind=$kind size=$size")
        }
        val body = ByteArray(size)
        input.readFully(body)
        return WireMessage(kind, body)
    }

    fun openPinnedTls(
        host: String,
        port: Int,
        expectedPin: String?,
        connectTimeoutMs: Int = 8000,
        readTimeoutMs: Int = 25000,
    ): Pair<SSLSocket, String> {
        // CA validation is intentionally replaced by mandatory SHA-256 certificate pinning below.
        val trustAll = arrayOf<TrustManager>(object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
            override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {}
            override fun getAcceptedIssuers(): Array<X509Certificate> = emptyArray()
        })
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(null, trustAll, SecureRandom())
        val socket = ctx.socketFactory.createSocket() as SSLSocket
        socket.connect(InetSocketAddress(host, port), connectTimeoutMs)
        socket.tcpNoDelay = true
        socket.keepAlive = true
        socket.soTimeout = readTimeoutMs
        socket.startHandshake()

        val peerCert = socket.session.peerCertificates.firstOrNull()
            ?: run {
                socket.close()
                throw IOException("服务端未提供 TLS 证书")
            }
        val actualPin = MessageDigest.getInstance("SHA-256")
            .digest(peerCert.encoded)
            .joinToString("") { "%02x".format(it) }
        if (!expectedPin.isNullOrEmpty()) {
            val ok = MessageDigest.isEqual(
                actualPin.toByteArray(Charsets.US_ASCII),
                expectedPin.lowercase().toByteArray(Charsets.US_ASCII)
            )
            if (!ok) {
                socket.close()
                throw SecurityException("设备证书指纹不匹配，请重新配对核对配对码")
            }
        }
        return Pair(socket, actualPin)
    }

    fun discoverLanHost(host: String, port: Int, expectedPin: String? = null): SavedDevice {
        val (socket, pin) = openPinnedTls(host, port, expectedPin, connectTimeoutMs = 3500, readTimeoutMs = 4000)
        socket.use { sock ->
            val out = BufferedOutputStream(sock.outputStream)
            val inp = DataInputStream(BufferedInputStream(sock.inputStream))
            val req = JSONObject().apply {
                put("type", "discover")
                put("version", VERSION)
            }
            out.write(packControl(req))
            out.flush()
            val reply = readFrame(inp)
            if (reply.kind != CONTROL) {
                throw IOException("无效发现响应")
            }
            val json = JSONObject(String(reply.payload, Charsets.UTF_8))
            if (json.optString("type") != "device" || json.optString("product") != "Brioche") {
                throw IOException(json.optString("message", "对方未开启 Brioche 局域网共享"))
            }
            return SavedDevice(
                ip = host,
                port = port,
                name = json.optString("name", host),
                platform = json.optString("platform", "Desktop"),
                pin = pin,
                token = "",
            )
        }
    }

    fun pairLanHost(
        host: String,
        port: Int,
        expectedPin: String,
        clientId: String,
        deviceName: String,
        onCode: (String) -> Unit,
    ): String {
        val (socket, pin) = openPinnedTls(host, port, expectedPin, connectTimeoutMs = 5000, readTimeoutMs = 35000)
        socket.use { sock ->
            val out = BufferedOutputStream(sock.outputStream)
            val inp = DataInputStream(BufferedInputStream(sock.inputStream))
            val nonce = randomHex(16)
            onCode(pairingCode(pin, nonce, clientId))
            val req = JSONObject().apply {
                put("type", "pair")
                put("version", VERSION)
                put("client_id", clientId)
                put("nonce", nonce)
                put("name", deviceName.take(64))
            }
            out.write(packControl(req))
            out.flush()
            val reply = readFrame(inp)
            if (reply.kind != CONTROL) {
                throw IOException("无效配对响应")
            }
            val json = JSONObject(String(reply.payload, Charsets.UTF_8))
            if (json.optString("type") != "paired") {
                throw IOException(json.optString("message", "对方未允许配对或超时"))
            }
            val token = json.optString("token", "")
            if (!TOKEN_REGEX.matches(token)) {
                throw IOException("服务端返回了无效配对令牌")
            }
            return token
        }
    }

    fun parseMonitors(arr: JSONArray?): List<MonitorInfo> {
        if (arr == null) return emptyList()
        val list = mutableListOf<MonitorInfo>()
        for (i in 0 until arr.length()) {
            val obj = arr.optJSONObject(i) ?: continue
            val id = obj.optInt("id", i + 1)
            val w = obj.optInt("width", 1920)
            val h = obj.optInt("height", 1080)
            val label = obj.optString("label", "显示器 $id")
            list.add(MonitorInfo(id, "$label (${w}×${h})", w, h))
        }
        return list
    }
}
