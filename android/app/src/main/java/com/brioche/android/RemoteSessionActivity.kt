package com.brioche.android

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.Surface
import android.view.SurfaceHolder
import android.view.SurfaceView
import android.view.View
import android.view.ViewGroup
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
        const val EXTRA_RESOLUTION = "resolution"
        const val EXTRA_FPS = "fps"
        const val EXTRA_BITRATE = "bitrate"
    }

    private lateinit var videoSurface: SurfaceView
    private lateinit var tvStatus: TextView
    private lateinit var tvHudStats: TextView
    private lateinit var toolbarScroll: HorizontalScrollView
    private lateinit var btnExpandBar: Button
    private lateinit var btnTouchMode: Button
    private lateinit var btnMonitor: Button
    private lateinit var hiddenInput: EditText

    private var client: BriocheClient? = null
    @Volatile
    private var currentSurface: Surface? = null
    private var videoWidth = 1920
    private var videoHeight = 1080
    private var monitors: List<BriocheProtocol.MonitorInfo> = emptyList()
    private var activeMonitor = 1

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
        toolbarScroll = findViewById(R.id.toolbarScroll)
        btnExpandBar = findViewById(R.id.btnExpandBar)
        btnTouchMode = findViewById(R.id.btnTouchMode)
        btnMonitor = findViewById(R.id.btnMonitor)
        hiddenInput = findViewById(R.id.hiddenKeyboardInput)

        videoSurface.holder.addCallback(this)
        setupToolbar()
        setupTouchControl()
        setupSoftKeyboardBridge()

        val host = intent.getStringExtra(EXTRA_HOST) ?: return finish()
        val port = intent.getIntExtra(EXTRA_PORT, BriocheProtocol.DEFAULT_PORT)
        val pin = intent.getStringExtra(EXTRA_PIN) ?: return finish()
        val token = intent.getStringExtra(EXTRA_TOKEN) ?: return finish()
        val cfg = BriocheProtocol.StreamConfig(
            resolution = intent.getStringExtra(EXTRA_RESOLUTION) ?: "1080p",
            fps = intent.getIntExtra(EXTRA_FPS, 60),
            bitrateMbps = intent.getIntExtra(EXTRA_BITRATE, 16),
        )

        client = BriocheClient(
            host = host,
            port = port,
            pin = pin,
            token = token,
            config = cfg,
            surfaceProvider = { currentSurface },
            listener = this,
        ).also { it.start() }
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
            val names = arrayOf(
                "鼠标右键",
                "Esc",
                "Tab",
                "Ctrl + C（复制）",
                "Ctrl + V（粘贴）",
                "Alt + Tab（切窗口）",
                "Win / Cmd 键",
                "Ctrl + Alt + Delete",
            )
            AlertDialog.Builder(this)
                .setTitle("快捷按键")
                .setItems(names) { _, which ->
                    when (which) {
                        0 -> {
                            client?.sendMouseButton("right", true)
                            client?.sendMouseButton("right", false)
                        }
                        1 -> client?.sendCombo(listOf("esc"))
                        2 -> client?.sendCombo(listOf("tab"))
                        3 -> client?.sendCombo(listOf("ctrl", "c"))
                        4 -> client?.sendCombo(listOf("ctrl", "v"))
                        5 -> client?.sendCombo(listOf("alt", "tab"))
                        6 -> client?.sendCombo(listOf("cmd"))
                        7 -> client?.sendCombo(listOf("ctrl", "alt", "delete"))
                    }
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
        if (videoRatio > screenRatio) {
            lp.width = rw
            lp.height = (rw / videoRatio).toInt()
        } else {
            lp.height = rh
            lp.width = (rh * videoRatio).toInt()
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

    override fun onStreamReady(
        width: Int,
        height: Int,
        codec: String,
        monitors: List<BriocheProtocol.MonitorInfo>,
        activeMonitor: Int,
        inputAllowed: Boolean,
    ) {
        runOnUiThread {
            this.videoWidth = width
            this.videoHeight = height
            this.monitors = monitors
            this.activeMonitor = activeMonitor
            btnMonitor.text = if (monitors.size > 1) "屏幕 $activeMonitor/${monitors.size}" else "屏幕"
            tvStatus.visibility = View.GONE
            updateSurfaceAspect()
        }
    }

    override fun onStats(fps: Int, rttMs: Int, mbps: Double) {
        runOnUiThread {
            tvHudStats.text = String.format("HW · %d FPS · %dms · %.1fM", fps, rttMs, mbps)
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
