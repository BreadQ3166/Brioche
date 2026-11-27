package com.brioche.android

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {
    private lateinit var repo: DeviceRepository
    private lateinit var discovery: LanDiscovery
    private val io = Executors.newCachedThreadPool()

    private lateinit var etLanIp: EditText
    private lateinit var btnQuality: Button
    private lateinit var tvStatus: TextView
    private lateinit var nearbyContainer: LinearLayout
    private lateinit var tvNoNearby: TextView
    private lateinit var savedContainer: LinearLayout
    private lateinit var tvNoSaved: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        repo = DeviceRepository(this)
        etLanIp = findViewById(R.id.etLanIp)
        btnQuality = findViewById(R.id.btnQuality)
        tvStatus = findViewById(R.id.tvStatus)
        nearbyContainer = findViewById(R.id.nearbyContainer)
        tvNoNearby = findViewById(R.id.tvNoNearby)
        savedContainer = findViewById(R.id.savedContainer)
        tvNoSaved = findViewById(R.id.tvNoSaved)

        updateQualityLabel()
        btnQuality.setOnClickListener { showQualityPicker() }

        findViewById<Button>(R.id.btnLanConnect).setOnClickListener {
            connectByLanAddress(etLanIp.text.toString())
        }
        etLanIp.setOnEditorActionListener { _, actionId, _ ->
            if (actionId == EditorInfo.IME_ACTION_GO) {
                connectByLanAddress(etLanIp.text.toString())
                true
            } else {
                false
            }
        }
        findViewById<Button>(R.id.btnUriConnect).setOnClickListener {
            showUriDialog()
        }

        discovery = LanDiscovery(this) { items ->
            runOnUiThread { renderNearby(items) }
        }

        renderSaved()
        handleDeepLink(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDeepLink(intent)
    }

    override fun onResume() {
        super.onResume()
        renderSaved()
        discovery.start()
    }

    override fun onPause() {
        super.onPause()
        discovery.stop()
    }

    override fun onDestroy() {
        super.onDestroy()
        discovery.stop()
        io.shutdownNow()
    }

    private fun handleDeepLink(intent: Intent?) {
        val raw = intent?.dataString ?: return
        if (raw.startsWith("brioche://")) {
            connectByUri(raw)
        }
    }

    private fun updateQualityLabel() {
        btnQuality.text = "${repo.resolution()} · ${repo.fps()}FPS · ${repo.bitrateMbps()}M"
    }

    private fun showQualityPicker() {
        val options = arrayOf(
            "流畅 · 720p / 60 FPS / 8 Mbps",
            "均衡 · 1080p / 60 FPS / 16 Mbps（推荐）",
            "高刷 · 1080p / 120 FPS / 24 Mbps",
            "超清 · 1440p / 60 FPS / 24 Mbps",
            "极致 · 1440p / 120 FPS / 35 Mbps",
        )
        val presets = arrayOf(
            Triple("720p", 60, 8),
            Triple("1080p", 60, 16),
            Triple("1080p", 120, 24),
            Triple("1440p", 60, 24),
            Triple("1440p", 120, 35),
        )
        AlertDialog.Builder(this)
            .setTitle("选择远程画质预设")
            .setItems(options) { _, which ->
                val (res, fps, mbps) = presets[which]
                repo.saveStreamConfig(res, fps, mbps)
                updateQualityLabel()
            }
            .show()
    }

    private fun showStatus(msg: String) {
        tvStatus.text = msg
        tvStatus.visibility = View.VISIBLE
    }

    private fun showUriDialog() {
        val input = EditText(this).apply {
            hint = "粘贴 brioche://host:port#pin.token"
            setPadding(40, 28, 40, 28)
        }
        AlertDialog.Builder(this)
            .setTitle("粘贴 Brioche 连接凭据")
            .setView(input)
            .setPositiveButton("连接") { _, _ ->
                connectByUri(input.text.toString())
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun connectByUri(raw: String) {
        try {
            val ep = BriocheProtocol.parseUri(raw)
            launchSession(ep.host, ep.port, ep.pin, ep.token, ep.host)
        } catch (ex: Exception) {
            Toast.makeText(this, ex.message ?: "连接凭据无效", Toast.LENGTH_LONG).show()
        }
    }

    private fun connectByLanAddress(raw: String) {
        val (ip, port) = try {
            BriocheProtocol.parseLanTarget(raw)
        } catch (ex: Exception) {
            showStatus(ex.message ?: "请输入有效的局域网 IP")
            return
        }
        val existing = repo.listDevices().firstOrNull { it.ip == ip && it.port == port }
        if (existing != null && existing.token.isNotEmpty()) {
            launchSession(existing.ip, existing.port, existing.pin, existing.token, existing.name)
            return
        }
        discoverAndPairThenConnect(ip, port, existing?.pin)
    }

    private fun discoverAndPairThenConnect(ip: String, port: Int, expectedPin: String?) {
        showStatus("正在通过局域网探测 $ip:$port…")
        io.execute {
            try {
                val discovered = BriocheProtocol.discoverLanHost(ip, port, expectedPin)
                val savedByPin = repo.listDevices().firstOrNull { it.pin == discovered.pin && it.token.isNotEmpty() }
                if (savedByPin != null) {
                    val updated = discovered.copy(token = savedByPin.token)
                    repo.upsert(updated)
                    runOnUiThread {
                        renderSaved()
                        launchSession(updated.ip, updated.port, updated.pin, updated.token, updated.name)
                    }
                    return@execute
                }
                var pairDialog: AlertDialog? = null
                val myName = "${Build.MANUFACTURER} ${Build.MODEL}".trim().ifEmpty { "Android 设备" }
                val token = BriocheProtocol.pairLanHost(
                    host = discovered.ip,
                    port = discovered.port,
                    expectedPin = discovered.pin,
                    clientId = repo.clientId(),
                    deviceName = myName,
                ) { code ->
                    runOnUiThread {
                        pairDialog = AlertDialog.Builder(this)
                            .setTitle("首次局域网配对")
                            .setMessage("配对码：$code\n\n请在电脑端（${discovered.name}）核对相同的六位配对码，并点击“是”允许配对。")
                            .setCancelable(true)
                            .show()
                    }
                }
                val paired = discovered.copy(token = token)
                repo.upsert(paired)
                runOnUiThread {
                    pairDialog?.dismiss()
                    tvStatus.visibility = View.GONE
                    renderSaved()
                    launchSession(paired.ip, paired.port, paired.pin, paired.token, paired.name)
                }
            } catch (ex: Exception) {
                runOnUiThread {
                    showStatus("连接失败：${ex.message ?: "请确认电脑端已开启局域网共享"}")
                }
            }
        }
    }

    private fun renderNearby(items: List<NearbyLanDevice>) {
        nearbyContainer.removeAllViews()
        tvNoNearby.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
        val inflater = LayoutInflater.from(this)
        for (item in items) {
            val card = inflater.inflate(R.layout.item_device_card, nearbyContainer, false)
            card.findViewById<TextView>(R.id.tvDeviceName).text = item.name
            card.findViewById<TextView>(R.id.tvDeviceMeta).text =
                "${item.platform} · ${item.ip}:${item.port}"
            card.findViewById<TextView>(R.id.tvDeviceBadge).text =
                if (item.busy) "使用中" else "LAN 发现"
            card.setOnClickListener {
                etLanIp.setText(if (item.port == BriocheProtocol.DEFAULT_PORT) item.ip else "${item.ip}:${item.port}")
                discoverAndPairThenConnect(item.ip, item.port, null)
            }
            nearbyContainer.addView(card)
        }
    }

    private fun renderSaved() {
        val devices = repo.listDevices()
        savedContainer.removeAllViews()
        tvNoSaved.visibility = if (devices.isEmpty()) View.VISIBLE else View.GONE
        val inflater = LayoutInflater.from(this)
        for (dev in devices) {
            val card = inflater.inflate(R.layout.item_device_card, savedContainer, false)
            card.findViewById<TextView>(R.id.tvDeviceName).text = dev.name
            card.findViewById<TextView>(R.id.tvDeviceMeta).text =
                "${dev.platform} · ${dev.ip}:${dev.port}"
            card.findViewById<TextView>(R.id.tvDeviceBadge).text =
                if (dev.token.isNotEmpty()) "LAN 已配对" else "待配对"
            card.setOnClickListener {
                if (dev.token.isNotEmpty()) {
                    launchSession(dev.ip, dev.port, dev.pin, dev.token, dev.name)
                } else {
                    discoverAndPairThenConnect(dev.ip, dev.port, dev.pin)
                }
            }
            card.setOnLongClickListener {
                AlertDialog.Builder(this)
                    .setTitle("移除设备")
                    .setMessage("从已配对列表中移除 ${dev.name}（${dev.ip}）？")
                    .setPositiveButton("移除") { _, _ ->
                        repo.remove(dev.ip, dev.port)
                        renderSaved()
                    }
                    .setNegativeButton("取消", null)
                    .show()
                true
            }
            savedContainer.addView(card)
        }
    }

    private fun launchSession(host: String, port: Int, pin: String, token: String, name: String) {
        val intent = Intent(this, RemoteSessionActivity::class.java).apply {
            putExtra(RemoteSessionActivity.EXTRA_HOST, host)
            putExtra(RemoteSessionActivity.EXTRA_PORT, port)
            putExtra(RemoteSessionActivity.EXTRA_PIN, pin)
            putExtra(RemoteSessionActivity.EXTRA_TOKEN, token)
            putExtra(RemoteSessionActivity.EXTRA_NAME, name)
            putExtra(RemoteSessionActivity.EXTRA_RESOLUTION, repo.resolution())
            putExtra(RemoteSessionActivity.EXTRA_FPS, repo.fps())
            putExtra(RemoteSessionActivity.EXTRA_BITRATE, repo.bitrateMbps())
        }
        startActivity(intent)
    }
}
