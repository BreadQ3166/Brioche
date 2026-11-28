package com.brioche.android

import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.text.Editable
import android.text.TextWatcher
import android.view.LayoutInflater
import android.view.View
import android.view.inputmethod.EditorInfo
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import com.google.android.material.button.MaterialButton
import com.google.android.material.materialswitch.MaterialSwitch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

class MainActivity : AppCompatActivity() {
    private lateinit var repo: DeviceRepository
    private lateinit var discovery: LanDiscovery
    private val io = Executors.newCachedThreadPool()

    // Pages & bottom tabs
    private lateinit var pageDevices: ScrollView
    private lateinit var pageRecents: ScrollView
    private lateinit var pageTransfers: ScrollView
    private lateinit var pageSettings: ScrollView
    private lateinit var tabDevices: MaterialButton
    private lateinit var tabRecents: MaterialButton
    private lateinit var tabTransfers: MaterialButton
    private lateinit var tabSettings: MaterialButton

    // Devices page views
    private lateinit var etLanIp: EditText
    private lateinit var etSearch: EditText
    private lateinit var btnQuality: Button
    private lateinit var tvStatus: TextView
    private lateinit var tvTsBadge: TextView
    private lateinit var tvTsStatus: TextView
    private lateinit var btnTsAction: MaterialButton
    private lateinit var tsPeersContainer: LinearLayout
    private lateinit var tvNoTsPeers: TextView
    private lateinit var tvTsSectionTitle: TextView
    private lateinit var nearbyContainer: LinearLayout
    private lateinit var tvNoNearby: TextView
    private lateinit var tvNearbyTitle: TextView
    private lateinit var savedContainer: LinearLayout
    private lateinit var tvNoSaved: TextView
    private lateinit var tvSavedTitle: TextView

    // Recents & Transfers & Settings views
    private lateinit var recentsContainer: LinearLayout
    private lateinit var tvNoRecents: TextView
    private lateinit var transfersContainer: LinearLayout
    private lateinit var tvNoTransfers: TextView
    private lateinit var btnTransferRate: MaterialButton
    private lateinit var btnSettingTheme: MaterialButton
    private lateinit var btnSettingQuality: MaterialButton
    private lateinit var swDefaultAudio: MaterialSwitch
    private lateinit var swDefaultTrackpad: MaterialSwitch
    private lateinit var tvSettingTsDetail: TextView

    // Filter state
    private var activeFilter = "all" // all | online | wan | lan | fav
    private var searchQuery = ""
    private var latestNearby: List<NearbyLanDevice> = emptyList()
    private var latestTsStatus: EasyTierStatus? = null
    private val timeFmt = SimpleDateFormat("MM-dd HH:mm", Locale.getDefault())

    private val transferListener: () -> Unit = {
        runOnUiThread { renderTransfers() }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        repo = DeviceRepository(this)
        applySavedTheme(repo.themeMode())
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        bindViews()
        setupTabs()
        setupDeviceControls()
        setupFilters()
        setupSettingsControls()

        discovery = LanDiscovery(this) { items ->
            latestNearby = items
            runOnUiThread { renderAllDeviceLists() }
        }

        TransferStore.addListener(transferListener)
        updateAllLabels()
        renderAllDeviceLists()
        renderRecents()
        renderTransfers()
        refreshTailscale(probe = true)
        handleDeepLink(intent)
    }

    private fun bindViews() {
        pageDevices = findViewById(R.id.pageDevices)
        pageRecents = findViewById(R.id.pageRecents)
        pageTransfers = findViewById(R.id.pageTransfers)
        pageSettings = findViewById(R.id.pageSettings)
        tabDevices = findViewById(R.id.tabDevices)
        tabRecents = findViewById(R.id.tabRecents)
        tabTransfers = findViewById(R.id.tabTransfers)
        tabSettings = findViewById(R.id.tabSettings)

        etLanIp = findViewById(R.id.etLanIp)
        etSearch = findViewById(R.id.etSearch)
        btnQuality = findViewById(R.id.btnQuality)
        tvStatus = findViewById(R.id.tvStatus)
        tvTsBadge = findViewById(R.id.tvTsBadge)
        tvTsStatus = findViewById(R.id.tvTsStatus)
        btnTsAction = findViewById(R.id.btnTsAction)
        tsPeersContainer = findViewById(R.id.tsPeersContainer)
        tvNoTsPeers = findViewById(R.id.tvNoTsPeers)
        tvTsSectionTitle = findViewById(R.id.tvTsSectionTitle)
        nearbyContainer = findViewById(R.id.nearbyContainer)
        tvNoNearby = findViewById(R.id.tvNoNearby)
        tvNearbyTitle = findViewById(R.id.tvNearbyTitle)
        savedContainer = findViewById(R.id.savedContainer)
        tvNoSaved = findViewById(R.id.tvNoSaved)
        tvSavedTitle = findViewById(R.id.tvSavedTitle)

        recentsContainer = findViewById(R.id.recentsContainer)
        tvNoRecents = findViewById(R.id.tvNoRecents)
        transfersContainer = findViewById(R.id.transfersContainer)
        tvNoTransfers = findViewById(R.id.tvNoTransfers)
        btnTransferRate = findViewById(R.id.btnTransferRate)
        btnSettingTheme = findViewById(R.id.btnSettingTheme)
        btnSettingQuality = findViewById(R.id.btnSettingQuality)
        swDefaultAudio = findViewById(R.id.swDefaultAudio)
        swDefaultTrackpad = findViewById(R.id.swDefaultTrackpad)
        tvSettingTsDetail = findViewById(R.id.tvSettingTsDetail)
    }

    private fun setupTabs() {
        fun showPage(index: Int) {
            pageDevices.visibility = if (index == 0) View.VISIBLE else View.GONE
            pageRecents.visibility = if (index == 1) View.VISIBLE else View.GONE
            pageTransfers.visibility = if (index == 2) View.VISIBLE else View.GONE
            pageSettings.visibility = if (index == 3) View.VISIBLE else View.GONE

            val tabs = listOf(tabDevices, tabRecents, tabTransfers, tabSettings)
            tabs.forEachIndexed { idx, btn ->
                btn.setTextColor(getColor(if (idx == index) R.color.brioche_accent else R.color.brioche_muted))
            }
            if (index == 1) renderRecents()
            if (index == 2) renderTransfers()
        }
        tabDevices.setOnClickListener { showPage(0) }
        tabRecents.setOnClickListener { showPage(1) }
        tabTransfers.setOnClickListener { showPage(2) }
        tabSettings.setOnClickListener { showPage(3) }
        showPage(0)
    }

    private fun setupDeviceControls() {
        btnQuality.setOnClickListener { showQualityPicker(onPicked = { r, f, b ->
            repo.saveStreamConfig(r, f, b)
            updateAllLabels()
        }) }

        btnTsAction.setOnClickListener { startTailscaleLogin() }
        findViewById<MaterialButton>(R.id.btnTsRefresh).setOnClickListener {
            Toast.makeText(this, "正在刷新外网与局域网设备…", Toast.LENGTH_SHORT).show()
            refreshTailscale(probe = true)
        }

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

        etSearch.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(s: CharSequence?, start: Int, count: Int, after: Int) {}
            override fun onTextChanged(s: CharSequence?, start: Int, before: Int, count: Int) {
                searchQuery = s?.toString()?.trim()?.lowercase().orEmpty()
                renderAllDeviceLists()
            }
            override fun afterTextChanged(s: Editable?) {}
        })

        findViewById<MaterialButton>(R.id.btnClearRecents).setOnClickListener {
            repo.clearRecents()
            renderRecents()
        }
        findViewById<MaterialButton>(R.id.btnClearTransfers).setOnClickListener {
            TransferStore.clearFinished()
        }
        btnTransferRate.setOnClickListener { showTransferRatePicker() }
    }

    private fun setupFilters() {
        val map = listOf(
            "all" to findViewById<MaterialButton>(R.id.filterAll),
            "online" to findViewById<MaterialButton>(R.id.filterOnline),
            "wan" to findViewById<MaterialButton>(R.id.filterWan),
            "lan" to findViewById<MaterialButton>(R.id.filterLan),
            "fav" to findViewById<MaterialButton>(R.id.filterFav),
        )
        fun updateFilterUI() {
            map.forEach { (key, btn) ->
                val active = key == activeFilter
                btn.setTextColor(getColor(if (active) R.color.brioche_accent else R.color.brioche_muted))
            }
            renderAllDeviceLists()
        }
        map.forEach { (key, btn) ->
            btn.setOnClickListener {
                activeFilter = key
                updateFilterUI()
            }
        }
    }

    private fun setupSettingsControls() {
        swDefaultAudio.isChecked = repo.audioEnabled()
        swDefaultAudio.setOnCheckedChangeListener { _, isChecked ->
            repo.setAudioEnabled(isChecked)
        }
        swDefaultTrackpad.isChecked = repo.defaultTrackpadMode()
        swDefaultTrackpad.setOnCheckedChangeListener { _, isChecked ->
            repo.setDefaultTrackpadMode(isChecked)
        }
        btnSettingTheme.setOnClickListener {
            val labels = arrayOf("跟随系统", "浅色 Warm Paper", "深色 Charcoal Dark")
            val keys = arrayOf("system", "light", "dark")
            AlertDialog.Builder(this)
                .setTitle("选择界面主题")
                .setItems(labels) { _, which ->
                    repo.setThemeMode(keys[which])
                    applySavedTheme(keys[which])
                    updateAllLabels()
                }
                .show()
        }
        btnSettingQuality.setOnClickListener {
            showQualityPicker(onPicked = { r, f, b ->
                repo.saveStreamConfig(r, f, b)
                updateAllLabels()
            })
        }
        findViewById<MaterialButton>(R.id.btnSettingTsLogin).setOnClickListener {
            startTailscaleLogin()
        }
        findViewById<MaterialButton>(R.id.btnSettingTsLogout).setOnClickListener {
            AlertDialog.Builder(this)
                .setTitle("退出内置 EasyTier 网络")
                .setMessage("退出后将无法连接外网设备，直到重新输入网络名称与密钥登录。")
                .setPositiveButton("退出") { _, _ ->
                    io.execute {
                        EmbeddedNetwork.get(this).logout()
                        refreshTailscale(probe = false)
                    }
                }
                .setNegativeButton("取消", null)
                .show()
        }
        findViewById<MaterialButton>(R.id.btnClearPaired).setOnClickListener {
            AlertDialog.Builder(this)
                .setTitle("清空已配对设备")
                .setMessage("清除手机本地保存的所有局域网配对凭据？下次连接将重新核对 6 位配对码。")
                .setPositiveButton("清空") { _, _ ->
                    repo.clearAllDevices()
                    renderAllDeviceLists()
                }
                .setNegativeButton("取消", null)
                .show()
        }
    }

    private fun applySavedTheme(mode: String) {
        val flag = when (mode) {
            "light" -> AppCompatDelegate.MODE_NIGHT_NO
            "dark" -> AppCompatDelegate.MODE_NIGHT_YES
            else -> AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM
        }
        if (AppCompatDelegate.getDefaultNightMode() != flag) {
            AppCompatDelegate.setDefaultNightMode(flag)
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleDeepLink(intent)
    }

    override fun onResume() {
        super.onResume()
        renderAllDeviceLists()
        renderRecents()
        renderTransfers()
        discovery.start()
        if (EmbeddedNetwork.get(this).isEnabled) {
            refreshTailscale(probe = true)
        }
    }

    override fun onPause() {
        super.onPause()
        discovery.stop()
    }

    override fun onDestroy() {
        super.onDestroy()
        TransferStore.removeListener(transferListener)
        discovery.stop()
        io.shutdownNow()
    }

    private fun handleDeepLink(intent: Intent?) {
        val raw = intent?.dataString ?: return
        if (raw.startsWith("brioche://")) {
            connectByUri(raw)
        }
    }

    private fun updateAllLabels() {
        val q = "${repo.resolution()} · ${repo.fps()}FPS · ${repo.bitrateMbps()}M"
        btnQuality.text = q
        btnSettingQuality.text = q
        btnSettingTheme.text = when (repo.themeMode()) {
            "light" -> "浅色"
            "dark" -> "深色"
            else -> "跟随系统"
        }
        btnTransferRate.text = when (repo.transferRateBps()) {
            0L -> "不限速"
            20L * 1024L * 1024L -> "20 MiB/s"
            8L * 1024L * 1024L -> "8 MiB/s"
            else -> "2 MiB/s"
        }
    }

    private fun showTransferRatePicker() {
        val labels = arrayOf("极速 · 不限速", "高速 · 20 MiB/s（推荐）", "均衡 · 8 MiB/s", "低干扰 · 2 MiB/s")
        val values = longArrayOf(0L, 20L * 1024L * 1024L, 8L * 1024L * 1024L, 2L * 1024L * 1024L)
        AlertDialog.Builder(this)
            .setTitle("文件传输限速策略")
            .setItems(labels) { _, which ->
                repo.setTransferRateBps(values[which])
                updateAllLabels()
            }
            .show()
    }

    private fun showQualityPicker(title: String = "选择远程画质预设", onPicked: (String, Int, Int) -> Unit) {
        val options = arrayOf(
            "流畅 · 720p / 60 FPS / 8 Mbps",
            "均衡 · 1080p / 60 FPS / 16 Mbps（推荐）",
            "高刷 · 1080p / 120 FPS / 24 Mbps",
            "超清 · 1440p / 60 FPS / 24 Mbps",
            "极致 · 1440p / 120 FPS / 35 Mbps",
            "原生 · 最高 4K / 60 FPS / 40 Mbps",
        )
        val presets = arrayOf(
            Triple("720p", 60, 8),
            Triple("1080p", 60, 16),
            Triple("1080p", 120, 24),
            Triple("1440p", 60, 24),
            Triple("1440p", 120, 35),
            Triple("native", 60, 40),
        )
        AlertDialog.Builder(this)
            .setTitle(title)
            .setItems(options) { _, which ->
                val (res, fps, mbps) = presets[which]
                onPicked(res, fps, mbps)
            }
            .show()
    }

    private fun refreshTailscale(probe: Boolean) {
        io.execute {
            val st = EmbeddedNetwork.get(this).status(probe = probe)
            latestTsStatus = st
            runOnUiThread {
                updateTailscaleUI(st)
                renderAllDeviceLists()
            }
        }
    }

    private fun startTailscaleLogin() {
        val net = EmbeddedNetwork.get(this)
        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 24, 48, 8)
        }
        val etName = EditText(this).apply {
            hint = "网络名称（相当于账号 / 房间名，如 bread-home）"
            setText(net.networkName())
            isSingleLine = true
        }
        val etSecret = EditText(this).apply {
            hint = "网络密钥（两端需一致，至少 4 位）"
            setText(net.networkSecret())
            isSingleLine = true
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        val etPeers = EditText(this).apply {
            hint = "引导节点（默认公共节点，支持自定义 VPS）"
            setText(net.peersCsv())
            minLines = 2
            maxLines = 4
            textSize = 12f
        }
        container.addView(TextView(this).apply { text = "网络名称"; textSize = 12f })
        container.addView(etName)
        container.addView(TextView(this).apply { text = "网络密钥（端到端加密密码）"; textSize = 12f; setPadding(0, 14, 0, 0) })
        container.addView(etSecret)
        container.addView(TextView(this).apply { text = "公共 / 自建引导节点（逗号分隔）"; textSize = 12f; setPadding(0, 14, 0, 0) })
        container.addView(etPeers)

        AlertDialog.Builder(this)
            .setTitle("登录 EasyTier 全球直连网络")
            .setMessage("手机与电脑填入相同的网络名称和密钥即可自动 P2P 直连（免客户端、低延迟）。")
            .setView(container)
            .setPositiveButton("连接 EasyTier") { _, _ ->
                val name = etName.text.toString().trim()
                val secret = etSecret.text.toString().trim()
                val peers = etPeers.text.toString().trim()
                tvTsStatus.text = "正在加入 EasyTier 网络「$name」…"
                io.execute {
                    try {
                        val st = net.login(name, secret, peers)
                        latestTsStatus = st
                        runOnUiThread {
                            updateTailscaleUI(st)
                            renderAllDeviceLists()
                            Toast.makeText(this, st.message, Toast.LENGTH_LONG).show()
                        }
                    } catch (ex: Exception) {
                        runOnUiThread {
                            tvTsStatus.text = ex.message ?: "启动 EasyTier 失败"
                            Toast.makeText(this, tvTsStatus.text, Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun updateTailscaleUI(st: EasyTierStatus) {
        tvTsStatus.text = st.message
        tvSettingTsDetail.text = if (st.ready) {
            "已连接 · 网络: ${st.networkName} · 虚拟 IP: ${st.ips.firstOrNull() ?: "-"} · 发现 ${st.peers.size} 台 Brioche 设备"
        } else {
            st.message
        }
        when {
            st.ready -> {
                tvTsBadge.text = "已连接"
                tvTsBadge.setTextColor(getColor(R.color.brioche_online))
                btnTsAction.text = "切换网络 / 节点"
            }
            else -> {
                tvTsBadge.text = if (st.state == "Unavailable") "异常" else "未登录"
                tvTsBadge.setTextColor(getColor(if (st.state == "Unavailable") R.color.brioche_danger else R.color.brioche_muted))
                btnTsAction.text = "登录 EasyTier"
            }
        }
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
            launchLanSession(
                host = ep.host,
                port = ep.port,
                pin = ep.pin,
                token = ep.token,
                name = ep.host,
                cfg = repo.effectiveConfigFor(null),
            )
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
            launchLanSession(
                host = existing.ip,
                port = existing.port,
                pin = existing.pin,
                token = existing.token,
                name = existing.displayName,
                cfg = repo.effectiveConfigFor(existing),
            )
            return
        }
        discoverAndPairThenConnect(ip, port, existing?.pin)
    }

    private fun connectTailscalePeer(peer: EasyTierPeer, openFilesOnly: Boolean = false) {
        val meta = repo.getPeerMeta(peer.ip)
        val display = meta.alias.ifBlank { peer.name }
        if (openFilesOnly) {
            openFileBrowser(display, "127.0.0.1", EmbeddedNetwork.STREAM_PORT, "", "", tailscaleIp = peer.ip)
            return
        }
        if (meta.lanOnly) {
            Toast.makeText(this, "「$display」已锁定仅局域网直连，请在设备配置中关闭锁定或使用局域网 IP", Toast.LENGTH_LONG).show()
            return
        }
        showStatus("正在通过内置 EasyTier 连接 $display（${peer.ip}）…")
        io.execute {
            try {
                val ep = EmbeddedNetwork.get(this).openPeer(peer.ip)
                runOnUiThread {
                    tvStatus.visibility = View.GONE
                    repo.recordRecent(
                        RecentConnection(
                            id = "et:${peer.ip}",
                            name = display,
                            mode = "tailscale",
                            address = peer.ip,
                            port = EmbeddedNetwork.STREAM_PORT,
                            pin = ep.pin,
                            token = ep.token,
                            connectedAtMs = System.currentTimeMillis(),
                        )
                    )
                    val cfg = repo.effectiveConfigForPeer(peer.ip)
                    val intent = Intent(this, RemoteSessionActivity::class.java).apply {
                        putExtra(RemoteSessionActivity.EXTRA_HOST, ep.localHost)
                        putExtra(RemoteSessionActivity.EXTRA_PORT, ep.localPort)
                        putExtra(RemoteSessionActivity.EXTRA_PIN, ep.pin)
                        putExtra(RemoteSessionActivity.EXTRA_TOKEN, ep.token)
                        putExtra(RemoteSessionActivity.EXTRA_NAME, display)
                        putExtra(RemoteSessionActivity.EXTRA_TAILSCALE_IP, peer.ip)
                        putExtra(RemoteSessionActivity.EXTRA_TUNNEL_ID, ep.tunnelId)
                        putExtra(RemoteSessionActivity.EXTRA_PATH_LABEL, "EasyTier · ${peer.path}")
                        putExtra(RemoteSessionActivity.EXTRA_RESOLUTION, cfg.resolution)
                        putExtra(RemoteSessionActivity.EXTRA_FPS, cfg.fps)
                        putExtra(RemoteSessionActivity.EXTRA_BITRATE, cfg.bitrateMbps)
                        putExtra(RemoteSessionActivity.EXTRA_AUDIO, repo.audioEnabled())
                        putExtra(RemoteSessionActivity.EXTRA_TRACKPAD, repo.defaultTrackpadMode())
                    }
                    startActivity(intent)
                }
            } catch (ex: Exception) {
                runOnUiThread {
                    showStatus("EasyTier 连接失败：${ex.message}")
                }
            }
        }
    }

    private fun discoverAndPairThenConnect(ip: String, port: Int, expectedPin: String?, openFilesOnly: Boolean = false) {
        showStatus("正在通过局域网探测 $ip:$port…")
        io.execute {
            try {
                val discovered = BriocheProtocol.discoverLanHost(ip, port, expectedPin)
                val savedByPin = repo.listDevices().firstOrNull { it.pin == discovered.pin && it.token.isNotEmpty() }
                if (savedByPin != null) {
                    val updated = discovered.copy(token = savedByPin.token, lastConnectedAt = System.currentTimeMillis())
                    repo.upsert(updated)
                    val fresh = repo.listDevices().firstOrNull { it.pin == discovered.pin } ?: updated
                    runOnUiThread {
                        tvStatus.visibility = View.GONE
                        renderAllDeviceLists()
                        if (openFilesOnly) {
                            openFileBrowser(fresh.displayName, fresh.ip, fresh.port, fresh.pin, fresh.token)
                        } else {
                            launchLanSession(fresh.ip, fresh.port, fresh.pin, fresh.token, fresh.displayName, repo.effectiveConfigFor(fresh))
                        }
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
                val paired = discovered.copy(token = token, lastConnectedAt = System.currentTimeMillis())
                repo.upsert(paired)
                runOnUiThread {
                    pairDialog?.dismiss()
                    tvStatus.visibility = View.GONE
                    renderAllDeviceLists()
                    if (openFilesOnly) {
                        openFileBrowser(paired.displayName, paired.ip, paired.port, paired.pin, paired.token)
                    } else {
                        launchLanSession(paired.ip, paired.port, paired.pin, paired.token, paired.displayName, repo.effectiveConfigFor(paired))
                    }
                }
            } catch (ex: Exception) {
                runOnUiThread {
                    showStatus("连接失败：${ex.message ?: "请确认电脑端已开启局域网共享"}")
                }
            }
        }
    }

    private fun openFileBrowser(
        name: String,
        host: String,
        port: Int,
        pin: String,
        token: String,
        tailscaleIp: String = "",
    ) {
        val intent = Intent(this, FileBrowserActivity::class.java).apply {
            putExtra(FileBrowserActivity.EXTRA_NAME, name)
            putExtra(FileBrowserActivity.EXTRA_HOST, host)
            putExtra(FileBrowserActivity.EXTRA_PORT, port)
            putExtra(FileBrowserActivity.EXTRA_PIN, pin)
            putExtra(FileBrowserActivity.EXTRA_TOKEN, token)
            putExtra(FileBrowserActivity.EXTRA_TAILSCALE_IP, tailscaleIp)
        }
        startActivity(intent)
    }

    private fun launchLanSession(
        host: String,
        port: Int,
        pin: String,
        token: String,
        name: String,
        cfg: BriocheProtocol.StreamConfig,
    ) {
        repo.recordRecent(
            RecentConnection(
                id = "lan:$host:$port",
                name = name,
                mode = "lan",
                address = host,
                port = port,
                pin = pin,
                token = token,
                connectedAtMs = System.currentTimeMillis(),
            )
        )
        val intent = Intent(this, RemoteSessionActivity::class.java).apply {
            putExtra(RemoteSessionActivity.EXTRA_HOST, host)
            putExtra(RemoteSessionActivity.EXTRA_PORT, port)
            putExtra(RemoteSessionActivity.EXTRA_PIN, pin)
            putExtra(RemoteSessionActivity.EXTRA_TOKEN, token)
            putExtra(RemoteSessionActivity.EXTRA_NAME, name)
            putExtra(RemoteSessionActivity.EXTRA_PATH_LABEL, "局域网直连")
            putExtra(RemoteSessionActivity.EXTRA_RESOLUTION, cfg.resolution)
            putExtra(RemoteSessionActivity.EXTRA_FPS, cfg.fps)
            putExtra(RemoteSessionActivity.EXTRA_BITRATE, cfg.bitrateMbps)
            putExtra(RemoteSessionActivity.EXTRA_AUDIO, repo.audioEnabled())
            putExtra(RemoteSessionActivity.EXTRA_TRACKPAD, repo.defaultTrackpadMode())
        }
        startActivity(intent)
    }

    private fun matchesQuery(vararg fields: String): Boolean {
        if (searchQuery.isEmpty()) return true
        return fields.any { it.lowercase().contains(searchQuery) }
    }

    private fun renderAllDeviceLists() {
        val showWan = activeFilter in setOf("all", "online", "wan", "fav")
        val showLan = activeFilter in setOf("all", "online", "lan", "fav")

        tvTsSectionTitle.visibility = if (showWan) View.VISIBLE else View.GONE
        tsPeersContainer.visibility = if (showWan) View.VISIBLE else View.GONE
        tvNearbyTitle.visibility = if (showLan && activeFilter != "fav") View.VISIBLE else View.GONE
        nearbyContainer.visibility = if (showLan && activeFilter != "fav") View.VISIBLE else View.GONE
        tvSavedTitle.visibility = if (showLan) View.VISIBLE else View.GONE
        savedContainer.visibility = if (showLan) View.VISIBLE else View.GONE

        renderTsPeers(showWan)
        renderNearby(showLan && activeFilter != "fav")
        renderSaved(showLan)
    }

    private fun renderTsPeers(visible: Boolean) {
        tsPeersContainer.removeAllViews()
        if (!visible) {
            tvNoTsPeers.visibility = View.GONE
            return
        }
        val peers = (latestTsStatus?.peers ?: emptyList()).filter { p ->
            val meta = repo.getPeerMeta(p.ip)
            val okFilter = when (activeFilter) {
                "online" -> p.reachable && p.sharing
                "fav" -> meta.favorite
                else -> true
            }
            okFilter && matchesQuery(p.name, meta.alias, p.ip)
        }.sortedWith(compareByDescending<EasyTierPeer> { repo.getPeerMeta(it.ip).favorite }.thenByDescending { it.sharing }.thenBy { it.name.lowercase() })

        tvNoTsPeers.visibility = if (peers.isEmpty()) View.VISIBLE else View.GONE
        val inflater = LayoutInflater.from(this)
        for (peer in peers) {
            val meta = repo.getPeerMeta(peer.ip)
            val card = inflater.inflate(R.layout.item_device_card, tsPeersContainer, false)
            val star = if (meta.favorite) "★ " else ""
            val lockTag = if (meta.lanOnly) " · 🔒仅LAN" else ""
            card.findViewById<TextView>(R.id.tvDeviceName).text = "$star${meta.alias.ifBlank { peer.name }}"
            card.findViewById<TextView>(R.id.tvDeviceMeta).text = "EasyTier · ${peer.ip} · ${peer.path}$lockTag"
            val badge = card.findViewById<TextView>(R.id.tvDeviceBadge)
            when {
                peer.busy -> {
                    badge.text = "使用中"
                    badge.setTextColor(getColor(R.color.brioche_warn))
                }
                peer.reachable && peer.sharing -> {
                    badge.text = "在线可连"
                    badge.setTextColor(getColor(R.color.brioche_online))
                }
                peer.reachable -> {
                    badge.text = "未共享"
                    badge.setTextColor(getColor(R.color.brioche_muted))
                }
                else -> {
                    badge.text = if (peer.online) "待唤醒" else "离线"
                    badge.setTextColor(getColor(R.color.brioche_muted))
                }
            }
            val btnFiles = card.findViewById<MaterialButton>(R.id.btnDeviceFiles)
            btnFiles.visibility = if (peer.sharing || peer.online) View.VISIBLE else View.GONE
            btnFiles.setOnClickListener { connectTailscalePeer(peer, openFilesOnly = true) }

            val btnMore = card.findViewById<MaterialButton>(R.id.btnDeviceMore)
            btnMore.visibility = View.VISIBLE
            btnMore.setOnClickListener { showTsPeerConfigDialog(peer) }
            card.setOnClickListener { connectTailscalePeer(peer) }
            card.setOnLongClickListener {
                showTsPeerConfigDialog(peer)
                true
            }
            tsPeersContainer.addView(card)
        }
    }

    private fun renderNearby(visible: Boolean) {
        nearbyContainer.removeAllViews()
        if (!visible) {
            tvNoNearby.visibility = View.GONE
            return
        }
        val items = latestNearby.filter { matchesQuery(it.name, it.ip, it.platform) }
        tvNoNearby.visibility = if (items.isEmpty()) View.VISIBLE else View.GONE
        val inflater = LayoutInflater.from(this)
        for (item in items) {
            val card = inflater.inflate(R.layout.item_device_card, nearbyContainer, false)
            card.findViewById<TextView>(R.id.tvDeviceName).text = item.name
            card.findViewById<TextView>(R.id.tvDeviceMeta).text = "${item.platform} · ${item.ip}:${item.port}"
            card.findViewById<TextView>(R.id.tvDeviceBadge).text = if (item.busy) "使用中" else "LAN 发现"
            val btnFiles = card.findViewById<MaterialButton>(R.id.btnDeviceFiles)
            btnFiles.visibility = View.VISIBLE
            btnFiles.setOnClickListener {
                discoverAndPairThenConnect(item.ip, item.port, null, openFilesOnly = true)
            }
            card.setOnClickListener {
                etLanIp.setText(if (item.port == BriocheProtocol.DEFAULT_PORT) item.ip else "${item.ip}:${item.port}")
                discoverAndPairThenConnect(item.ip, item.port, null)
            }
            nearbyContainer.addView(card)
        }
    }

    private fun renderSaved(visible: Boolean) {
        savedContainer.removeAllViews()
        if (!visible) {
            tvNoSaved.visibility = View.GONE
            return
        }
        val devices = repo.listDevices().filter { d ->
            val okFilter = when (activeFilter) {
                "fav" -> d.favorite
                "online" -> latestNearby.any { it.ip == d.ip && it.port == d.port }
                else -> true
            }
            okFilter && matchesQuery(d.name, d.alias, d.ip, d.platform)
        }
        tvNoSaved.visibility = if (devices.isEmpty()) View.VISIBLE else View.GONE
        val inflater = LayoutInflater.from(this)
        for (dev in devices) {
            val card = inflater.inflate(R.layout.item_device_card, savedContainer, false)
            val star = if (dev.favorite) "★ " else ""
            val customQ = if (dev.customResolution.isNotEmpty()) " · ${dev.customResolution}/${dev.customFps}FPS" else ""
            card.findViewById<TextView>(R.id.tvDeviceName).text = "$star${dev.displayName}"
            card.findViewById<TextView>(R.id.tvDeviceMeta).text = "${dev.platform} · ${dev.ip}:${dev.port}$customQ"
            card.findViewById<TextView>(R.id.tvDeviceBadge).text = if (dev.token.isNotEmpty()) "LAN 已配对" else "待配对"

            val btnFiles = card.findViewById<MaterialButton>(R.id.btnDeviceFiles)
            btnFiles.visibility = View.VISIBLE
            btnFiles.setOnClickListener {
                if (dev.token.isNotEmpty()) {
                    openFileBrowser(dev.displayName, dev.ip, dev.port, dev.pin, dev.token)
                } else {
                    discoverAndPairThenConnect(dev.ip, dev.port, dev.pin, openFilesOnly = true)
                }
            }
            val btnMore = card.findViewById<MaterialButton>(R.id.btnDeviceMore)
            btnMore.visibility = View.VISIBLE
            btnMore.setOnClickListener { showLanDeviceConfigDialog(dev) }

            card.setOnClickListener {
                if (dev.token.isNotEmpty()) {
                    launchLanSession(dev.ip, dev.port, dev.pin, dev.token, dev.displayName, repo.effectiveConfigFor(dev))
                } else {
                    discoverAndPairThenConnect(dev.ip, dev.port, dev.pin)
                }
            }
            card.setOnLongClickListener {
                showLanDeviceConfigDialog(dev)
                true
            }
            savedContainer.addView(card)
        }
    }

    private fun showLanDeviceConfigDialog(dev: SavedDevice) {
        val actions = arrayOf(
            if (dev.favorite) "取消星标收藏" else "★ 设为星标收藏",
            "自定义备注名（当前：${dev.displayName}）",
            "设置专属画质/帧率（当前：${dev.customResolution.ifEmpty { "跟随全局" }}）",
            "打开远端文件管理器",
            "移除此设备",
        )
        AlertDialog.Builder(this)
            .setTitle("设备配置 · ${dev.displayName}")
            .setItems(actions) { _, which ->
                when (which) {
                    0 -> {
                        repo.replaceExact(dev.copy(favorite = !dev.favorite))
                        renderAllDeviceLists()
                    }
                    1 -> promptRenameAlias(dev.alias.ifEmpty { dev.name }) { alias ->
                        repo.replaceExact(dev.copy(alias = alias))
                        renderAllDeviceLists()
                    }
                    2 -> showQualityPicker("专属画质 · ${dev.displayName}") { r, f, b ->
                        repo.replaceExact(dev.copy(customResolution = r, customFps = f, customBitrate = b))
                        renderAllDeviceLists()
                    }
                    3 -> {
                        if (dev.token.isNotEmpty()) {
                            openFileBrowser(dev.displayName, dev.ip, dev.port, dev.pin, dev.token)
                        } else {
                            discoverAndPairThenConnect(dev.ip, dev.port, dev.pin, openFilesOnly = true)
                        }
                    }
                    4 -> {
                        repo.remove(dev.ip, dev.port)
                        renderAllDeviceLists()
                    }
                }
            }
            .show()
    }

    private fun showTsPeerConfigDialog(peer: EasyTierPeer) {
        val meta = repo.getPeerMeta(peer.ip)
        val display = meta.alias.ifBlank { peer.name }
        val actions = arrayOf(
            if (meta.favorite) "取消星标收藏" else "★ 设为星标收藏",
            "自定义备注名（当前：$display）",
            "设置专属画质/帧率（当前：${meta.customResolution.ifEmpty { "跟随全局" }}）",
            if (meta.lanOnly) "解除「仅局域网直连」锁定" else "🔒 锁定仅局域网直连（禁用外网）",
            "打开远端文件管理器",
        )
        AlertDialog.Builder(this)
            .setTitle("外网设备配置 · $display")
            .setItems(actions) { _, which ->
                when (which) {
                    0 -> {
                        repo.savePeerMeta(peer.ip, meta.copy(favorite = !meta.favorite))
                        renderAllDeviceLists()
                    }
                    1 -> promptRenameAlias(display) { alias ->
                        repo.savePeerMeta(peer.ip, meta.copy(alias = alias))
                        renderAllDeviceLists()
                    }
                    2 -> showQualityPicker("专属画质 · $display") { r, f, b ->
                        repo.savePeerMeta(peer.ip, meta.copy(customResolution = r, customFps = f, customBitrate = b))
                        renderAllDeviceLists()
                    }
                    3 -> {
                        repo.savePeerMeta(peer.ip, meta.copy(lanOnly = !meta.lanOnly))
                        renderAllDeviceLists()
                    }
                    4 -> connectTailscalePeer(peer, openFilesOnly = true)
                }
            }
            .show()
    }

    private fun promptRenameAlias(initial: String, onSave: (String) -> Unit) {
        val input = EditText(this).apply {
            setText(initial)
            setPadding(40, 28, 40, 28)
        }
        AlertDialog.Builder(this)
            .setTitle("设置设备备注名")
            .setView(input)
            .setPositiveButton("保存") { _, _ ->
                onSave(input.text.toString().trim())
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun renderRecents() {
        val recents = repo.listRecents()
        recentsContainer.removeAllViews()
        tvNoRecents.visibility = if (recents.isEmpty()) View.VISIBLE else View.GONE
        val inflater = LayoutInflater.from(this)
        for (r in recents) {
            val card = inflater.inflate(R.layout.item_device_card, recentsContainer, false)
            card.findViewById<TextView>(R.id.tvDeviceName).text = r.name
            val ts = if (r.connectedAtMs > 0) timeFmt.format(Date(r.connectedAtMs)) else ""
            val modeLabel = if (r.mode == "tailscale") "EasyTier 全球直连" else "局域网直连"
            card.findViewById<TextView>(R.id.tvDeviceMeta).text = "$modeLabel · ${r.address} · $ts"
            card.findViewById<TextView>(R.id.tvDeviceBadge).text = "一键重连"
            val btnFiles = card.findViewById<MaterialButton>(R.id.btnDeviceFiles)
            btnFiles.visibility = View.VISIBLE
            btnFiles.setOnClickListener {
                if (r.mode == "tailscale") {
                    connectTailscalePeer(
                        EasyTierPeer(r.address, r.name, online = true, reachable = true, sharing = true, busy = false, path = "外网"),
                        openFilesOnly = true
                    )
                } else {
                    openFileBrowser(r.name, r.address, r.port, r.pin, r.token)
                }
            }
            card.setOnClickListener {
                if (r.mode == "tailscale") {
                    connectTailscalePeer(
                        EasyTierPeer(r.address, r.name, online = true, reachable = true, sharing = true, busy = false, path = "外网")
                    )
                } else {
                    launchLanSession(r.address, r.port, r.pin, r.token, r.name, repo.effectiveConfigFor(null))
                }
            }
            recentsContainer.addView(card)
        }
    }

    private fun renderTransfers() {
        val list = TransferStore.snapshot()
        val activeCount = list.count { !it.finished }
        tabTransfers.text = if (activeCount > 0) "传输 ($activeCount)" else "传输"
        transfersContainer.removeAllViews()
        tvNoTransfers.visibility = if (list.isEmpty()) View.VISIBLE else View.GONE
        val inflater = LayoutInflater.from(this)
        for (item in list) {
            val card = inflater.inflate(R.layout.item_transfer_card, transfersContainer, false)
            val arrow = if (item.direction == "upload") "⬆ 上传到" else "⬇ 下载自"
            card.findViewById<TextView>(R.id.tvTransferTitle).text = "$arrow ${item.peerName}"
            card.findViewById<TextView>(R.id.tvTransferFile).text = item.currentFile
            val pb = card.findViewById<ProgressBar>(R.id.pbTransfer)
            val pct = if (item.totalBytes > 0) ((item.completedBytes * 100) / item.totalBytes).toInt().coerceIn(0, 100) else if (item.finished) 100 else 0
            pb.progress = pct
            val speedMb = item.speedBps / (1024.0 * 1024.0)
            card.findViewById<TextView>(R.id.tvTransferStatus).text = if (item.finished) {
                item.status
            } else {
                String.format(Locale.US, "%s · %d%% · %.1f MiB/s", item.status, pct, speedMb)
            }
            val btnCancel = card.findViewById<MaterialButton>(R.id.btnTransferCancel)
            if (!item.finished && item.cancelFn != null) {
                btnCancel.visibility = View.VISIBLE
                btnCancel.setOnClickListener { item.cancelFn.invoke() }
            }
            transfersContainer.addView(card)
        }
    }
}
