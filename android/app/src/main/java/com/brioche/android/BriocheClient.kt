package com.brioche.android

import android.content.Context
import android.view.Surface
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.IOException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import javax.net.ssl.SSLSocket

data class StreamReadyInfo(
    val width: Int,
    val height: Int,
    val codec: String,
    val encoder: String,
    val capture: String,
    val remotePlatform: String,
    val monitors: List<BriocheProtocol.MonitorInfo>,
    val activeMonitor: Int,
    val inputAllowed: Boolean,
    val filesAllowed: Boolean,
    val clipboardAllowed: Boolean,
    val audioActive: Boolean,
    val activeHost: String,
    val activePort: Int,
    val activePin: String,
    val activeToken: String,
)

data class SessionStats(
    val renderFps: Int,
    val encodedFps: Double,
    val rttMs: Int,
    val mbps: Double,
    val captureEncodeMs: Double,
    val sendMs: Double,
)

/**
 * Threaded Brioche v2 pinned-TLS streaming client with:
 * - Zero-copy hardware MediaCodec H.264/HEVC video decode
 * - Low-latency MediaCodec Opus audio decode + AudioTrack playback
 * - 2s ping/pong heartbeat, input coalescing, clipboard sync, monitor switch
 * - Seamless auto-reconnect (both direct LAN and Tailscale WAN tunnel re-dial)
 */
class BriocheClient(
    private val appContext: Context,
    private val initialHost: String,
    private val initialPort: Int,
    private val initialPin: String,
    private val initialToken: String,
    private val tailscaleIp: String = "",
    private val initialTunnelId: String = "",
    private val config: BriocheProtocol.StreamConfig,
    private val audioEnabled: Boolean = true,
    private val surfaceProvider: () -> Surface?,
    private val listener: Listener,
) {
    interface Listener {
        fun onStatus(message: String)
        fun onStreamReady(info: StreamReadyInfo)
        fun onStats(stats: SessionStats)
        fun onClipboard(text: String)
        fun onFatalError(message: String)
    }

    private val stopped = AtomicBoolean(false)
    private val muted = AtomicBoolean(false)
    private val outQueue = LinkedBlockingQueue<JSONObject>(128)
    @Volatile
    private var latestMove: JSONObject? = null
    @Volatile
    private var activeSocket: SSLSocket? = null
    @Volatile
    private var activeAudio: OpusAudioPlayer? = null
    @Volatile
    private var activeTunnelId: String = initialTunnelId
    @Volatile
    private var streamId: Int = 0
    private val requestSeq = AtomicInteger(1)
    private var thread: Thread? = null

    fun start() {
        thread = Thread({ runWithReconnect() }, "brioche-session").apply {
            isDaemon = true
            start()
        }
    }

    fun stop() {
        stopped.set(true)
        sendControl(JSONObject().apply { put("type", "bye") })
        try {
            activeSocket?.close()
        } catch (_: Exception) {
        }
        val tid = activeTunnelId
        activeTunnelId = ""
        if (tid.isNotEmpty()) {
            Thread({ EmbeddedNetwork.get(appContext).closeTunnel(tid) }, "brioche-close-tun").start()
        }
    }

    fun setAudioMuted(mute: Boolean) {
        muted.set(mute)
        activeAudio?.setMuted(mute)
    }

    fun isAudioMuted(): Boolean = muted.get()

    fun sendMouseMove(nx: Float, ny: Float) {
        if (stopped.get()) return
        val x = nx.coerceIn(0f, 1f).toDouble()
        val y = ny.coerceIn(0f, 1f).toDouble()
        latestMove = JSONObject().apply {
            put("type", "input")
            put("event", "move")
            put("x", x)
            put("y", y)
            put("stream_id", streamId)
        }
    }

    fun sendMouseButton(button: String, down: Boolean) {
        sendInput(JSONObject().apply {
            put("type", "input")
            put("event", "button")
            put("button", button)
            put("down", down)
            put("stream_id", streamId)
        })
    }

    fun sendScroll(dx: Int, dy: Int) {
        sendInput(JSONObject().apply {
            put("type", "input")
            put("event", "scroll")
            put("dx", dx.coerceIn(-10, 10))
            put("dy", dy.coerceIn(-10, 10))
            put("stream_id", streamId)
        })
    }

    fun sendKey(key: String, down: Boolean) {
        sendInput(JSONObject().apply {
            put("type", "input")
            put("event", "key")
            put("key", key)
            put("down", down)
            put("stream_id", streamId)
        })
    }

    fun sendCombo(keys: List<String>) {
        keys.forEach { sendKey(it, true) }
        keys.asReversed().forEach { sendKey(it, false) }
    }

    fun sendSwitchMonitor(monitorId: Int) {
        sendControl(JSONObject().apply {
            put("type", "switch_monitor")
            put("monitor", monitorId)
            put("request", requestSeq.getAndIncrement())
        })
    }

    fun sendClipboard(text: String) {
        if (text.isEmpty() || text.toByteArray(Charsets.UTF_8).size > 128 * 1024) return
        sendControl(JSONObject().apply {
            put("type", "clipboard")
            put("text", text)
        })
    }

    private fun sendInput(obj: JSONObject) {
        if (stopped.get()) return
        if (!outQueue.offer(obj)) {
            outQueue.clear()
            outQueue.offer(JSONObject().apply {
                put("type", "input")
                put("event", "release")
                put("stream_id", streamId)
            })
        }
    }

    private fun sendControl(obj: JSONObject) {
        if (stopped.get()) return
        outQueue.offer(obj)
    }

    private fun runWithReconnect() {
        val delaysSec = intArrayOf(1, 2, 3, 5, 8)
        var attempt = 0
        var connectedOnce = false
        var curHost = initialHost
        var curPort = initialPort
        var curPin = initialPin
        var curToken = initialToken

        while (!stopped.get()) {
            try {
                if (attempt > 0) {
                    val wait = delaysSec[(attempt - 1).coerceAtMost(delaysSec.lastIndex)]
                    listener.onStatus("连接中断，${wait} 秒后自动重连（第 $attempt 次）…")
                    Thread.sleep(wait * 1000L)
                    if (stopped.get()) break
                    if (tailscaleIp.isNotEmpty()) {
                        listener.onStatus("正在通过内置 EasyTier 重建外网隧道…")
                        val oldTid = activeTunnelId
                        if (oldTid.isNotEmpty()) {
                            EmbeddedNetwork.get(appContext).closeTunnel(oldTid)
                            activeTunnelId = ""
                        }
                        val ep = EmbeddedNetwork.get(appContext).openPeer(tailscaleIp)
                        curHost = ep.localHost
                        curPort = ep.localPort
                        curPin = ep.pin
                        curToken = ep.token
                        activeTunnelId = ep.tunnelId
                    }
                }
                connectOnce(
                    host = curHost,
                    port = curPort,
                    pin = curPin,
                    token = curToken,
                    isReconnect = connectedOnce || attempt > 0,
                )
                connectedOnce = true
                attempt = 1
            } catch (sec: SecurityException) {
                listener.onFatalError(sec.message ?: "证书校验失败")
                return
            } catch (ex: Exception) {
                val msg = ex.message ?: "网络连接中断"
                if (isFatalMessage(msg)) {
                    listener.onFatalError(msg)
                    return
                }
                attempt++
                if (attempt > 6) {
                    listener.onFatalError("重连失败：$msg")
                    return
                }
            }
        }
    }

    private fun isFatalMessage(msg: String): Boolean {
        return listOf("凭据", "证书", "配对", "权限", "版本不兼容", "已有活动连接", "不支持", "尚未开启").any { msg.contains(it) }
    }

    private fun connectOnce(
        host: String,
        port: Int,
        pin: String,
        token: String,
        isReconnect: Boolean,
    ) {
        val label = if (tailscaleIp.isNotEmpty()) "EasyTier $tailscaleIp" else "$host:$port"
        listener.onStatus("正在建立加密连接 $label…")
        val (socket, _) = BriocheProtocol.openPinnedTls(host, port, pin, connectTimeoutMs = 10_000, readTimeoutMs = 25_000)
        activeSocket = socket
        var decoder: HardwareVideoDecoder? = null
        var audioPlayer: OpusAudioPlayer? = null
        val senderStop = AtomicBoolean(false)
        var senderThread: Thread? = null
        try {
            val out = BufferedOutputStream(socket.outputStream)
            val inp = DataInputStream(BufferedInputStream(socket.inputStream, 65536))
            val writeLock = Any()

            fun writeControl(obj: JSONObject) {
                val bytes = BriocheProtocol.packControl(obj)
                synchronized(writeLock) {
                    out.write(bytes)
                    out.flush()
                }
            }

            val hello = JSONObject().apply {
                put("type", "hello")
                put("version", BriocheProtocol.VERSION)
                put("token", token)
                put("config", config.toJson())
                put("audio", audioEnabled)
                if (isReconnect) put("reconnect", true)
            }
            writeControl(hello)

            val welcomeFrame = BriocheProtocol.readFrame(inp)
            if (welcomeFrame.kind != BriocheProtocol.CONTROL) {
                throw IOException("无效握手响应")
            }
            val welcome = JSONObject(String(welcomeFrame.payload, Charsets.UTF_8))
            if (welcome.optString("type") != "welcome") {
                throw IOException(welcome.optString("message", "服务端拒绝连接"))
            }

            var width = welcome.getInt("width")
            var height = welcome.getInt("height")
            var codecName = welcome.optString("codec", "h264")
            var encoderName = welcome.optString("encoder", codecName)
            var captureName = welcome.optString("capture", "")
            val remotePlatform = welcome.optString("platform", "Desktop")
            streamId = welcome.optInt("stream_id", 0)
            var monitors = BriocheProtocol.parseMonitors(welcome.optJSONArray("monitors"))
            var activeMonitor = welcome.optInt("monitor", 1)
            val inputAllowed = welcome.optBoolean("input", true)
            val filesAllowed = welcome.optBoolean("files", false)
            val clipboardAllowed = welcome.optBoolean("clipboard", false)
            val audioActive = welcome.optBoolean("audio", false)

            if (audioActive && audioEnabled) {
                audioPlayer = OpusAudioPlayer().also {
                    it.setMuted(muted.get())
                    activeAudio = it
                }
            }

            listener.onStreamReady(
                StreamReadyInfo(
                    width = width,
                    height = height,
                    codec = codecName,
                    encoder = encoderName,
                    capture = captureName,
                    remotePlatform = remotePlatform,
                    monitors = monitors,
                    activeMonitor = activeMonitor,
                    inputAllowed = inputAllowed,
                    filesAllowed = filesAllowed,
                    clipboardAllowed = clipboardAllowed,
                    audioActive = audioActive && (audioPlayer?.isReady == true),
                    activeHost = host,
                    activePort = port,
                    activePin = pin,
                    activeToken = token,
                )
            )
            listener.onStatus("已连接 · 硬件解码就绪")

            // Wait briefly for SurfaceView to be ready
            var surface: Surface? = null
            for (i in 0 until 60) {
                surface = surfaceProvider()
                if (surface != null && surface.isValid) break
                Thread.sleep(50)
            }
            if (surface != null && surface.isValid) {
                decoder = HardwareVideoDecoder(surface, codecName, width, height)
            }

            senderThread = Thread({
                var lastPingMs = 0L
                while (!stopped.get() && !senderStop.get()) {
                    try {
                        val mv = latestMove
                        if (mv != null) {
                            latestMove = null
                            writeControl(mv)
                        }
                        var sent = 0
                        while (sent < 24) {
                            val msg = outQueue.poll() ?: break
                            writeControl(msg)
                            sent++
                        }
                        val nowMs = System.currentTimeMillis()
                        if (nowMs - lastPingMs >= 2000L) {
                            lastPingMs = nowMs
                            writeControl(JSONObject().apply {
                                put("type", "ping")
                                put("stamp", nowMs / 1000.0)
                            })
                        }
                        Thread.sleep(5)
                    } catch (_: Exception) {
                        break
                    }
                }
            }, "brioche-sender").apply {
                isDaemon = true
                start()
            }

            var bytesSec = 0L
            var statsSince = System.currentTimeMillis()
            var lastRttMs = 0
            var prevFrames = 0
            var encodedFps = 0.0
            var captureEncodeMs = 0.0
            var sendMs = 0.0

            while (!stopped.get()) {
                val frame = BriocheProtocol.readFrame(inp)
                when (frame.kind) {
                    BriocheProtocol.VIDEO -> {
                        bytesSec += frame.payload.size
                        decoder?.decode(frame.payload)
                        val nowMs = System.currentTimeMillis()
                        if (nowMs - statsSince >= 1000L) {
                            val curFrames = decoder?.decodedFrames?.get() ?: 0
                            val dtSec = ((nowMs - statsSince) / 1000.0).coerceAtLeast(0.001)
                            val fpsNow = ((curFrames - prevFrames) / dtSec).toInt()
                            val mbps = (bytesSec * 8.0) / (dtSec * 1_000_000.0)
                            prevFrames = curFrames
                            bytesSec = 0L
                            statsSince = nowMs
                            listener.onStats(
                                SessionStats(
                                    renderFps = fpsNow,
                                    encodedFps = encodedFps,
                                    rttMs = lastRttMs,
                                    mbps = mbps,
                                    captureEncodeMs = captureEncodeMs,
                                    sendMs = sendMs,
                                )
                            )
                        }
                    }
                    BriocheProtocol.AUDIO -> {
                        audioPlayer?.decodeAndPlay(frame.payload)
                    }
                    BriocheProtocol.CONTROL -> {
                        val json = JSONObject(String(frame.payload, Charsets.UTF_8))
                        when (json.optString("type")) {
                            "pong" -> {
                                val stampSec = json.optDouble("stamp", 0.0)
                                val rtt = (System.currentTimeMillis() - (stampSec * 1000.0)).toInt().coerceAtLeast(0)
                                lastRttMs = rtt
                            }
                            "stats" -> {
                                encodedFps = json.optDouble("encoded_fps", encodedFps)
                                captureEncodeMs = json.optDouble("capture_encode_ms", captureEncodeMs)
                                sendMs = json.optDouble("send_ms", sendMs)
                            }
                            "stream" -> {
                                width = json.getInt("width")
                                height = json.getInt("height")
                                codecName = json.optString("codec", "h264")
                                encoderName = json.optString("encoder", codecName)
                                captureName = json.optString("capture", captureName)
                                streamId = json.optInt("stream_id", streamId + 1)
                                monitors = BriocheProtocol.parseMonitors(json.optJSONArray("monitors"))
                                activeMonitor = json.optInt("monitor", activeMonitor)
                                decoder?.release()
                                val s = surfaceProvider()
                                decoder = if (s != null && s.isValid) {
                                    HardwareVideoDecoder(s, codecName, width, height)
                                } else null
                                prevFrames = 0
                                listener.onStreamReady(
                                    StreamReadyInfo(
                                        width = width,
                                        height = height,
                                        codec = codecName,
                                        encoder = encoderName,
                                        capture = captureName,
                                        remotePlatform = remotePlatform,
                                        monitors = monitors,
                                        activeMonitor = activeMonitor,
                                        inputAllowed = inputAllowed,
                                        filesAllowed = filesAllowed,
                                        clipboardAllowed = clipboardAllowed,
                                        audioActive = audioActive && (audioPlayer?.isReady == true),
                                        activeHost = host,
                                        activePort = port,
                                        activePin = pin,
                                        activeToken = token,
                                    )
                                )
                            }
                            "clipboard" -> {
                                val text = json.optString("text", "")
                                if (text.isNotEmpty()) listener.onClipboard(text)
                            }
                            "error" -> {
                                throw IOException(json.optString("message", "服务端断开连接"))
                            }
                        }
                    }
                }
            }
        } finally {
            senderStop.set(true)
            senderThread?.interrupt()
            decoder?.release()
            activeAudio = null
            audioPlayer?.release()
            try {
                socket.close()
            } catch (_: Exception) {
            }
            activeSocket = null
        }
    }
}
