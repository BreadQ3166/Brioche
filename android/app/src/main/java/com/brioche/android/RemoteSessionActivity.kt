package com.brioche.android

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.view.WindowInsets
import android.view.WindowInsetsController
import android.view.inputmethod.InputMethodManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.HorizontalScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import kotlin.math.abs

class RemoteSessionActivity : AppCompatActivity(), SurfaceHolder.Callback, BriocheClient.Listener {
    companion object {
        const val EXTRA_HOST = "host"
        const val EXTRA_PORT = "port"
        const val EXTRA_PIN = "pin"
        const val EXTRA_TOKEN = "token"
        const val EXTRA_NAME = "name"
        const val EXTRA_TAILSCALE_IP = "tailscale_ip"
        const val EXTRA_TUNNEL_ID = "tunnel_id"
        const val EXTRA_PATH_LABEL = "path_label"
        const val EXTRA_RESOLUTION = "resolution"
        const val EXTRA_FPS = "fps"
        const val EXTRA_BITRATE = "bitrate"
        const val EXTRA_AUDIO = "audio"
        const val EXTRA_TRACKPAD = "trackpad"
    }

    private lateinit var videoSurface: SurfaceView
    private lateinit var tvStatus: TextView
    private lateinit var tvHudStats: TextView
    private lateinit var tvDetailedHud: TextView
    private lateinit var toolbarScroll: HorizontalScrollView
    private lateinit var btnExpandBar: Button
    private lateinit var btnTouchMode: Button
    private lateinit var btnMonitor: Button
    private lateinit var btnAudio: Button
    private lateinit var btnFiles: Button
    private lateinit var btnScaleMode: Button
    private lateinit var hiddenInput: EditText

    private var client: BriocheClient? = null
    @Volatile
    private var currentSurface: Surface? = null
    private var videoWidth = 1920
    private var videoHeight = 1080
    private var monitors: List<BriocheProtocol.MonitorInfo> = emptyList()
    private var activeMonitor = 1
    private var streamInfo: StreamReadyInfo? = null
    private var peerName = "远端电脑"
    private var tailscaleIp = ""
    private var pathLabel = "局域网直连"
    private var showDetailedHud = false
    private var scaleMode = 0 // 0=适应屏幕, 1=填满屏幕, 2=1:1 原始

    // Dual touch mode: trackpad (relative precision) vs direct touch
    private var trackpadMode = true
    private var cursorX = 0.5f
    private var cursorY = 0.5f
    private var lastTouchX = 0f
    private var lastTouchY = 0f
    private var downTouchX = 0f
    private var downTouchY = 0f
    private var downTimeMs = 0L
    private var maxPointers = 0
    private var scrollAccumY = 0f
    private var moved = false
    private var ignoreTextChange = false

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_remote_session)
        enterImmersive()

        videoSurface = findViewById(R.id.videoSurface)
        tvStatus = findViewById(R.id.tvSessionStatus)
        tvHudStats = findViewById(R.id.tvHudStats)
        tvDetailedHud = findViewById(R.id.tvDetailedHud)
        toolbarScroll = findViewById(R.id.toolbarScroll)
        btnExpandBar = findViewById(R.id.btnExpandBar)
        btnTouchMode = findViewById(R.id.btnTouchMode)
        btnMonitor = findViewById(R.id.btnMonitor)
        btnAudio = findViewById(R.id.btnAudio)
        btnFiles = findViewById(R.id.btnFiles)
        btnScaleMode = findViewById(R.id.btnScaleMode)
        hiddenInput = findViewById(R.id.hiddenKeyboardInput)

        trackpadMode = intent.getBooleanExtra(EXTRA_TRACKPAD, true)
        btnTouchMode.text = if (trackpadMode) "触控板" else "直触"
        peerName = intent.getStringExtra(EXTRA_NAME) ?: "远端电脑"
        tailscaleIp = intent.getStringExtra(EXTRA_TAILSCALE_IP) ?: ""
        pathLabel = intent.getStringExtra(EXTRA_PATH_LABEL) ?: if (tailscaleIp.isNotEmpty()) "Tailscale 外网" else "局域网直连"

        videoSurface.holder.addCallback(this)
        setupToolbar()
        setupTouchControl()
        setupSoftKeyboardBridge()

        val host = intent.getStringExtra(EXTRA_HOST) ?: return finish()
        val port = intent.getIntExtra(EXTRA_PORT, BriocheProtocol.DEFAULT_PORT)
        val pin = intent.getStringExtra(EXTRA_PIN) ?: return finish()
        val token = intent.getStringExtra(EXTRA_TOKEN) ?: return finish()
        val tunnelId = intent.getStringExtra(EXTRA_TUNNEL_ID) ?: ""
        val audioOn = intent.getBooleanExtra(EXTRA_AUDIO, true)
        val cfg = BriocheProtocol.StreamConfig(
            resolution = intent.getStringExtra(EXTRA_RESOLUTION) ?: "1080p",
            fps = intent.getIntExtra(EXTRA_FPS, 60),
            bitrateMbps = intent.getIntExtra(EXTRA_BITRATE, 16),
        )

        client = BriocheClient(
            appContext = applicationContext,
            initialHost = host,
            initialPort = port,
            initialPin = pin,
            initialToken = token,
            tailscaleIp = tailscaleIp,
            initialTunnelId = tunnelId,
            config = cfg,
            audioEnabled = audioOn,
            surfaceProvider = { currentSurface },
            listener = this,
        ).also { it.start() }
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) {
            enterImmersive()
        }
    }

    private fun enterImmersive() {
        if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.R) {
            window.insetsController?.let { ctl ->
                ctl.hide(WindowInsets.Type.statusBars() or WindowInsets.Type.navigationBars())
                ctl.systemBarsBehavior = WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
            }
        } else {
            @Suppress("DEPRECATION")
            window.decorView.systemUiVisibility = (
                View.SYSTEM_UI_FLAG_FULLSCREEN
                    or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                    or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                )
        }
    }

    private fun setupToolbar() {
        findViewById<Button>(R.id.btnToggleHud).setOnClickListener {
            showDetailedHud = !showDetailedHud
            tvDetailedHud.visibility = if (showDetailedHud) View.VISIBLE else View.GONE
        }
        btnAudio.setOnClickListener {
            val c = client ?: return@setOnClickListener
            val nextMuted = !c.isAudioMuted()
            c.setAudioMuted(nextMuted)
            btnAudio.text = if (nextMuted) "🔇 静音" else "🔊 音频"
            Toast.makeText(this, if (nextMuted) "已静音远端声音" else "已开启远端声音", Toast.LENGTH_SHORT).show()
        }
        btnFiles.setOnClickListener {
            val info = streamInfo
            if (info == null) {
                Toast.makeText(this, "正在建立连接，请稍候", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            val i = Intent(this, FileBrowserActivity::class.java).apply {
                putExtra(FileBrowserActivity.EXTRA_NAME, peerName)
                putExtra(FileBrowserActivity.EXTRA_HOST, info.activeHost)
                putExtra(FileBrowserActivity.EXTRA_PORT, info.activePort)
                putExtra(FileBrowserActivity.EXTRA_PIN, info.activePin)
                putExtra(FileBrowserActivity.EXTRA_TOKEN, info.activeToken)
                putExtra(FileBrowserActivity.EXTRA_TAILSCALE_IP, tailscaleIp)
            }
            startActivity(i)
        }
        btnScaleMode.setOnClickListener {
            scaleMode = (scaleMode + 1) % 3
            btnScaleMode.text = when (scaleMode) {
                0 -> "适应"
                1 -> "填满"
                else -> "1:1"
            }
            updateSurfaceAspect()
        }
        btnTouchMode.setOnClickListener {
            trackpadMode = !trackpadMode
            btnTouchMode.text = if (trackpadMode) "触控板" else "直触"
            Toast.makeText(
                this,
                if (trackpadMode) "触控板模式：单指滑动、轻点左键、双指右键/滚动" else "直触模式：点击即定位左键",
                Toast.LENGTH_SHORT,
            ).show()
        }
        btnMonitor.setOnClickListener {
            if (monitors.size <= 1) {
                Toast.makeText(this, "远端当前仅有 1 块显示器", Toast.LENGTH_SHORT).show()
                return@setOnClickListener
            }
            val labels = monitors.map { m ->
                if (m.id == activeMonitor) "● ${m.name}" else m.name
            }.toTypedArray()
            AlertDialog.Builder(this)
                .setTitle("切换远端显示器")
                .setItems(labels) { _, which ->
                    val chosen = monitors[which]
                    if (chosen.id != activeMonitor) {
                        client?.sendSwitchMonitor(chosen.id)
                        tvStatus.text = "正在切换到 ${chosen.name}…"
                        tvStatus.visibility = View.VISIBLE
                    }
                }
                .show()
        }
        findViewById<Button>(R.id.btnKeyboard).setOnClickListener {
            hiddenInput.requestFocus()
            val imm = getSystemService(Context.INPUT_METHOD_SERVICE) as? InputMethodManager
            imm?.showSoftInput(hiddenInput, InputMethodManager.SHOW_IMPLICIT)
        }
        findViewById<Button>(R.id.btnShortcuts).setOnClickListener {
            val items = listOf(
                "鼠标右键" to {
                    client?.sendMouseButton("right", true)
                    client?.sendMouseButton("right", false)
                },
                "鼠标中键" to {
                    client?.sendMouseButton("middle", true)
                    client?.sendMouseButton("middle", false)
                },
                "Esc" to { client?.sendCombo(listOf("esc")) },
                "Tab" to { client?.sendCombo(listOf("tab")) },
                "Ctrl + C（复制）" to { client?.sendCombo(listOf("ctrl", "c")) },
                "Ctrl + V（粘贴）" to { client?.sendCombo(listOf("ctrl", "v")) },
                "Ctrl + A（全选）" to { client?.sendCombo(listOf("ctrl", "a")) },
                "Ctrl + Z（撤销）" to { client?.sendCombo(listOf("ctrl", "z")) },
                "Ctrl + S（保存）" to { client?.sendCombo(listOf("ctrl", "s")) },
                "Alt + Tab（切换窗口）" to { client?.sendCombo(listOf("alt", "tab")) },
                "Alt + F4（关闭窗口）" to { client?.sendCombo(listOf("alt", "f4")) },
                "Win / Cmd 键" to { client?.sendCombo(listOf("cmd")) },
                "Win + D（显示桌面）" to { client?.sendCombo(listOf("cmd", "d")) },
                "Ctrl + Shift + Esc（任务管理器）" to { client?.sendCombo(listOf("ctrl", "shift", "esc")) },
                "Ctrl + Alt + Delete" to { client?.sendCombo(listOf("ctrl", "alt", "delete")) },
                "F5（刷新）" to { client?.sendCombo(listOf("f5")) },
                "F11（全屏）" to { client?.sendCombo(listOf("f11")) },
            )
            AlertDialog.Builder(this)
                .setTitle("桌面快捷按键")
                .setItems(items.map { it.first }.toTypedArray()) { _, which ->
                    items[which].second.invoke()
                }
                .show()
        }
        findViewById<Button>(R.id.btnClipboard).setOnClickListener {
            val cm = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            val text = cm?.primaryClip?.getItemAt(0)?.coerceToText(this)?.toString().orEmpty()
            if (text.isEmpty()) {
                Toast.makeText(this, "手机剪贴板为空", Toast.LENGTH_SHORT).show()
            } else {
                client?.sendClipboard(text)
                Toast.makeText(this, "已发送剪贴板到电脑", Toast.LENGTH_SHORT).show()
            }
        }
        findViewById<Button>(R.id.btnHideBar).setOnClickListener {
            toolbarScroll.visibility = View.GONE
            btnExpandBar.visibility = View.VISIBLE
        }
        btnExpandBar.setOnClickListener {
            btnExpandBar.visibility = View.GONE
            toolbarScroll.visibility = View.VISIBLE
        }
        findViewById<Button>(R.id.btnDisconnect).setOnClickListener {
            finish()
        }
    }

    private fun setupSoftKeyboardBridge() {
        hiddenInput.setOnKeyListener { _, keyCode, event ->
            val down = event.action == KeyEvent.ACTION_DOWN
            val up = event.action == KeyEvent.ACTION_UP
            if (!down && !up) return@setOnKeyListener false
            val mapped = when (keyCode) {
                KeyEvent.KEYCODE_DEL -> "backspace"
                KeyEvent.KEYCODE_ENTER -> "enter"
                KeyEvent.KEYCODE_TAB -> "tab"
                KeyEvent.KEYCODE_ESCAPE -> "esc"
                KeyEvent.KEYCODE_DPAD_UP -> "up"
                KeyEvent.KEYCODE_DPAD_DOWN -> "down"
                KeyEvent.KEYCODE_DPAD_LEFT -> "left"
                KeyEvent.KEYCODE_DPAD_RIGHT -> "right"
                else -> null
            }
            if (mapped != null) {
                client?.sendKey(mapped, down)
                return@setOnKeyListener true
            }
            false
        }
        hiddenInput.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                if (ignoreTextChange || s.isNullOrEmpty()) return
                val added = s.subSequence(start, (start + count).coerceAtMost(s.length)).toString()
                for (ch in added) {
                    if (ch == '\n') {
                        client?.sendCombo(listOf("enter"))
                    } else if (!ch.isISOControl()) {
                        client?.sendCombo(listOf(ch.toString()))
                    }
                }
                ignoreTextChange = true
                hiddenInput.setText("")
                ignoreTextChange = false
            }
            override fun afterTextChanged(s: Editable?) {}
        })
    }

    private fun setupTouchControl() {
        videoSurface.setOnTouchListener { view, event ->
            val w = view.width.coerceAtLeast(1).toFloat()
            val h = view.height.coerceAtLeast(1).toFloat()
            when (event.actionMasked) {
                MotionEvent.ACTION_DOWN -> {
                    downTouchX = event.x
                    downTouchY = event.y
                    lastTouchX = event.x
                    lastTouchY = event.y
                    downTimeMs = System.currentTimeMillis()
                    maxPointers = 1
                    scrollAccumY = 0f
                    moved = false
                    if (!trackpadMode) {
                        cursorX = (event.x / w).coerceIn(0f, 1f)
                        cursorY = (event.y / h).coerceIn(0f, 1f)
                        client?.sendMouseMove(cursorX, cursorY)
                        client?.sendMouseButton("left", true)
                    }
                    true
                }
                MotionEvent.ACTION_POINTER_DOWN -> {
                    maxPointers = maxOf(maxPointers, event.pointerCount)
                    lastTouchX = event.x
                    lastTouchY = event.y
                    true
                }
                MotionEvent.ACTION_MOVE -> {
                    val dx = event.x - lastTouchX
                    val dy = event.y - lastTouchY
                    lastTouchX = event.x
                    lastTouchY = event.y
                    if (abs(event.x - downTouchX) > 12f || abs(event.y - downTouchY) > 12f) {
                        moved = true
                    }
                    if (!trackpadMode) {
                        cursorX = (event.x / w).coerceIn(0f, 1f)
                        cursorY = (event.y / h).coerceIn(0f, 1f)
                        client?.sendMouseMove(cursorX, cursorY)
                    } else if (event.pointerCount >= 2) {
                        scrollAccumY += dy
                        val steps = (scrollAccumY / 36f).toInt()
                        if (steps != 0) {
                            scrollAccumY -= steps * 36f
                            client?.sendScroll(0, steps)
                        }
                    } else {
                        val gain = 1.45f
                        cursorX = (cursorX + (dx * gain) / w).coerceIn(0f, 1f)
                        cursorY = (cursorY + (dy * gain) / h).coerceIn(0f, 1f)
                        client?.sendMouseMove(cursorX, cursorY)
                    }
                    true
                }
                MotionEvent.ACTION_UP -> {
                    val elapsed = System.currentTimeMillis() - downTimeMs
                    if (!trackpadMode) {
                        client?.sendMouseButton("left", false)
                    } else if (!moved && elapsed < 320L) {
                        val btn = if (maxPointers >= 2) "right" else "left"
                        client?.sendMouseButton(btn, true)
                        client?.sendMouseButton(btn, false)
                    }
                    true
                }
                else -> false
            }
        }
    }

    private fun updateSurfaceAspect() {
        val root = findViewById<FrameLayout>(R.id.rootSession)
        val rw = root.width
        val rh = root.height
        if (rw <= 0 || rh <= 0 || videoWidth <= 0 || videoHeight <= 0) return
        val videoRatio = videoWidth.toFloat() / videoHeight.toFloat()
        val screenRatio = rw.toFloat() / rh.toFloat()
        val lp = videoSurface.layoutParams
        when (scaleMode) {
            1 -> { // Fill screen
                lp.width = rw
                lp.height = rh
            }
            2 -> { // 1:1 native pixel size clamped cleanly
                lp.width = videoWidth.coerceAtMost(rw * 2)
                lp.height = videoHeight.coerceAtMost(rh * 2)
            }
            else -> { // Fit aspect ratio
                if (videoRatio > screenRatio) {
                    lp.width = rw
                    lp.height = (rw / videoRatio).toInt()
                } else {
                    lp.height = rh
                    lp.width = (rh * videoRatio).toInt()
                }
            }
        }
        videoSurface.layoutParams = lp
    }

    override fun surfaceCreated(holder: SurfaceHolder) {
        currentSurface = holder.surface
        updateSurfaceAspect()
    }

    override fun surfaceChanged(holder: SurfaceHolder, format: Int, width: Int, height: Int) {
        currentSurface = holder.surface
    }

    override fun surfaceDestroyed(holder: SurfaceHolder) {
        currentSurface = null
    }

    override fun onStatus(message: String) {
        runOnUiThread {
            tvStatus.text = message
            tvStatus.visibility = if (message.startsWith("已连接")) View.GONE else View.VISIBLE
        }
    }

    override fun onStreamReady(info: StreamReadyInfo) {
        runOnUiThread {
            this.streamInfo = info
            this.videoWidth = info.width
            this.videoHeight = info.height
            this.monitors = info.monitors
            this.activeMonitor = info.activeMonitor
            btnMonitor.text = if (monitors.size > 1) "屏幕 $activeMonitor/${monitors.size}" else "屏幕"
            btnAudio.visibility = if (info.audioActive) View.VISIBLE else View.GONE
            btnFiles.visibility = if (info.filesAllowed) View.VISIBLE else View.GONE
            tvStatus.visibility = View.GONE
            updateSurfaceAspect()
        }
    }

    override fun onStats(stats: SessionStats) {
        runOnUiThread {
            tvHudStats.text = String.format("HW · %d FPS · %dms · %.1fM", stats.renderFps, stats.rttMs, stats.mbps)
            val info = streamInfo
            if (showDetailedHud && info != null) {
                tvDetailedHud.text = buildString {
                    append("$peerName · $pathLabel\n")
                    append("画面: ${info.width}×${info.height} · ${info.codec.uppercase()} (${info.encoder})\n")
                    append(String.format("帧率: 渲染 %d / 编码 %.1f FPS\n", stats.renderFps, stats.encodedFps))
                    append(String.format("延迟: RTT %dms · 编采 %.1fms · 发送 %.1fms\n", stats.rttMs, stats.captureEncodeMs, stats.sendMs))
                    append(String.format("带宽: %.2f Mbps · 音频: %s", stats.mbps, if (info.audioActive) (if (client?.isAudioMuted() == true) "已静音" else "Opus 48kHz") else "关闭"))
                }
            }
        }
    }

    override fun onClipboard(text: String) {
        runOnUiThread {
            val cm = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
            cm?.setPrimaryClip(ClipData.newPlainText("Brioche", text))
            Toast.makeText(this, "已同步电脑剪贴板", Toast.LENGTH_SHORT).show()
        }
    }

    override fun onFatalError(message: String) {
        runOnUiThread {
            AlertDialog.Builder(this)
                .setTitle("连接已结束")
                .setMessage(message)
                .setPositiveButton("返回") { _, _ -> finish() }
                .setCancelable(false)
                .show()
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        client?.stop()
        client = null
    }
}
