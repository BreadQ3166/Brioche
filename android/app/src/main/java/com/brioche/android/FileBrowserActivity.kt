package com.brioche.android

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import com.google.android.material.button.MaterialButton
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.concurrent.Executors

class FileBrowserActivity : AppCompatActivity() {
    companion object {
        const val EXTRA_NAME = "name"
        const val EXTRA_HOST = "host"
        const val EXTRA_PORT = "port"
        const val EXTRA_PIN = "pin"
        const val EXTRA_TOKEN = "token"
        const val EXTRA_TAILSCALE_IP = "tailscale_ip"
    }

    private val io = Executors.newSingleThreadExecutor()
    private lateinit var repo: DeviceRepository
    private lateinit var tvTitle: TextView
    private lateinit var tvPath: TextView
    private lateinit var tvBanner: TextView
    private lateinit var rootsContainer: LinearLayout
    private lateinit var entriesContainer: LinearLayout

    private var peerName = ""
    private var host = ""
    private var port = BriocheProtocol.DEFAULT_PORT
    private var pin = ""
    private var token = ""
    private var tailscaleIp = ""
    private var browserTunnelId = ""

    private var browser: RemoteFileBrowserClient? = null
    private var currentPath = ""
    private var parentPath = ""
    private val dateFmt = SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault())

    private val pickFilesLauncher = registerForActivityResult(
        ActivityResultContracts.OpenMultipleDocuments()
    ) { uris ->
        if (uris.isNullOrEmpty()) return@registerForActivityResult
        startUpload(uris)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_file_browser)
        repo = DeviceRepository(this)

        tvTitle = findViewById(R.id.tvFbTitle)
        tvPath = findViewById(R.id.tvFbPath)
        tvBanner = findViewById(R.id.tvFbBanner)
        rootsContainer = findViewById(R.id.fbRootsContainer)
        entriesContainer = findViewById(R.id.fbEntriesContainer)

        peerName = intent.getStringExtra(EXTRA_NAME) ?: "远端电脑"
        host = intent.getStringExtra(EXTRA_HOST) ?: ""
        port = intent.getIntExtra(EXTRA_PORT, BriocheProtocol.DEFAULT_PORT)
        pin = intent.getStringExtra(EXTRA_PIN) ?: ""
        token = intent.getStringExtra(EXTRA_TOKEN) ?: ""
        tailscaleIp = intent.getStringExtra(EXTRA_TAILSCALE_IP) ?: ""

        tvTitle.text = "文件 · $peerName"

        findViewById<MaterialButton>(R.id.btnFbBack).setOnClickListener { finish() }
        findViewById<MaterialButton>(R.id.btnFbUp).setOnClickListener {
            if (parentPath.isNotEmpty()) loadDir(parentPath)
        }
        findViewById<MaterialButton>(R.id.btnFbRefresh).setOnClickListener {
            loadDir(currentPath)
        }
        findViewById<MaterialButton>(R.id.btnFbUpload).setOnClickListener {
            pickFilesLauncher.launch(arrayOf("*/*"))
        }
        findViewById<MaterialButton>(R.id.btnFbMkdir).setOnClickListener {
            promptMkdir()
        }

        connectBrowser()
    }

    private fun showBanner(msg: String?) {
        runOnUiThread {
            if (msg.isNullOrBlank()) {
                tvBanner.visibility = View.GONE
            } else {
                tvBanner.text = msg
                tvBanner.visibility = View.VISIBLE
            }
        }
    }

    private fun connectBrowser() {
        showBanner("正在连接 $peerName 的远端文件服务…")
        io.execute {
            try {
                var targetHost = host
                var targetPort = port
                var targetPin = pin
                var targetToken = token
                if (tailscaleIp.isNotEmpty()) {
                    val ep = EmbeddedNetwork.get(this).openPeer(tailscaleIp)
                    targetHost = ep.localHost
                    targetPort = ep.localPort
                    targetPin = ep.pin
                    targetToken = ep.token
                    browserTunnelId = ep.tunnelId
                    pin = targetPin
                    token = targetToken
                }
                val client = RemoteFileBrowserClient(targetHost, targetPort, targetPin, targetToken)
                client.connect()
                browser = client
                val listing = client.listDir("")
                runOnUiThread {
                    renderRoots(client.roots)
                    renderListing(listing)
                    showBanner(null)
                }
            } catch (ex: Exception) {
                runOnUiThread {
                    showBanner("无法连接远端文件管理器：${ex.message ?: "请确认电脑端已开启文件互传与目录浏览"}")
                }
            }
        }
    }

    private fun loadDir(path: String) {
        val client = browser ?: return
        showBanner("正在读取目录…")
        io.execute {
            try {
                val listing = client.listDir(path)
                runOnUiThread {
                    renderListing(listing)
                    showBanner(null)
                }
            } catch (ex: Exception) {
                runOnUiThread {
                    showBanner("读取目录失败：${ex.message}")
                }
            }
        }
    }

    private fun renderRoots(roots: List<RemoteRoot>) {
        rootsContainer.removeAllViews()
        for (root in roots) {
            val btn = MaterialButton(this, null, com.google.android.material.R.attr.materialButtonOutlinedStyle).apply {
                text = root.name
                textSize = 11f
                insetTop = 0
                insetBottom = 0
                minimumWidth = 0
                minWidth = 0
                setPadding(24, 6, 24, 6)
                setOnClickListener { loadDir(root.path) }
            }
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                (32 * resources.displayMetrics.density).toInt()
            ).apply {
                marginEnd = (6 * resources.displayMetrics.density).toInt()
            }
            rootsContainer.addView(btn, lp)
        }
    }

    private fun renderListing(listing: RemoteListing) {
        currentPath = listing.path
        parentPath = listing.parent
        tvPath.text = listing.path
        entriesContainer.removeAllViews()
        if (listing.entries.isEmpty()) {
            val empty = TextView(this).apply {
                text = "此文件夹为空"
                setPadding(16, 48, 16, 48)
                setTextColor(getColor(R.color.brioche_muted))
            }
            entriesContainer.addView(empty)
            return
        }
        val inflater = LayoutInflater.from(this)
        for (entry in listing.entries) {
            val row = inflater.inflate(R.layout.item_file_entry, entriesContainer, false)
            row.findViewById<TextView>(R.id.tvEntryIcon).text = if (entry.isDir) "📁" else "📄"
            row.findViewById<TextView>(R.id.tvEntryName).text = entry.name
            val timeStr = if (entry.mtime > 0) dateFmt.format(Date(entry.mtime * 1000L)) else ""
            row.findViewById<TextView>(R.id.tvEntryMeta).text = if (entry.isDir) {
                "文件夹 · $timeStr"
            } else {
                "${formatSize(entry.size)} · $timeStr"
            }
            val btnAction = row.findViewById<MaterialButton>(R.id.btnEntryAction)
            if (entry.isDir) {
                btnAction.text = "下载目录"
                row.setOnClickListener { loadDir(entry.path) }
                btnAction.setOnClickListener { startDownload(listOf(entry.path), entry.name) }
            } else {
                btnAction.text = "下载"
                row.setOnClickListener { startDownload(listOf(entry.path), entry.name) }
                btnAction.setOnClickListener { startDownload(listOf(entry.path), entry.name) }
            }
            row.setOnLongClickListener {
                showEntryActions(entry)
                true
            }
            entriesContainer.addView(row)
        }
    }

    private fun showEntryActions(entry: RemoteEntry) {
        val options = arrayOf("下载到手机 Download/Brioche/", "重命名", "删除")
        AlertDialog.Builder(this)
            .setTitle(entry.name)
            .setItems(options) { _, which ->
                when (which) {
                    0 -> startDownload(listOf(entry.path), entry.name)
                    1 -> promptRename(entry)
                    2 -> confirmDelete(entry)
                }
            }
            .show()
    }

    private fun promptMkdir() {
        val input = EditText(this).apply {
            hint = "输入新文件夹名称"
            setPadding(40, 28, 40, 28)
        }
        AlertDialog.Builder(this)
            .setTitle("新建文件夹")
            .setView(input)
            .setPositiveButton("创建") { _, _ ->
                val name = input.text.toString().trim()
                if (name.isEmpty()) return@setPositiveButton
                io.execute {
                    try {
                        browser?.mkdir(currentPath, name)
                        runOnUiThread {
                            Toast.makeText(this, "已创建：$name", Toast.LENGTH_SHORT).show()
                            loadDir(currentPath)
                        }
                    } catch (ex: Exception) {
                        runOnUiThread {
                            Toast.makeText(this, ex.message ?: "新建失败", Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun promptRename(entry: RemoteEntry) {
        val input = EditText(this).apply {
            setText(entry.name)
            setPadding(40, 28, 40, 28)
        }
        AlertDialog.Builder(this)
            .setTitle("重命名")
            .setView(input)
            .setPositiveButton("确定") { _, _ ->
                val newName = input.text.toString().trim()
                if (newName.isEmpty() || newName == entry.name) return@setPositiveButton
                io.execute {
                    try {
                        browser?.rename(entry.path, newName)
                        runOnUiThread {
                            Toast.makeText(this, "已重命名", Toast.LENGTH_SHORT).show()
                            loadDir(currentPath)
                        }
                    } catch (ex: Exception) {
                        runOnUiThread {
                            Toast.makeText(this, ex.message ?: "重命名失败", Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun confirmDelete(entry: RemoteEntry) {
        AlertDialog.Builder(this)
            .setTitle("确认删除")
            .setMessage("确定要删除电脑上的「${entry.name}」吗？（文件夹仅允许删除空目录）")
            .setPositiveButton("删除") { _, _ ->
                io.execute {
                    try {
                        browser?.delete(entry.path)
                        runOnUiThread {
                            Toast.makeText(this, "已删除", Toast.LENGTH_SHORT).show()
                            loadDir(currentPath)
                        }
                    } catch (ex: Exception) {
                        runOnUiThread {
                            Toast.makeText(this, ex.message ?: "删除失败", Toast.LENGTH_LONG).show()
                        }
                    }
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun startUpload(uris: List<android.net.Uri>) {
        showBanner("正在准备上传 ${uris.size} 个文件到当前目录…（可在主界面「传输」页查看进度）")
        Thread({
            var tid = ""
            try {
                val (tHost, tPort) = resolveTransferEndpoint().also { tid = it.third }
                val transfer = FileTransferClient(
                    context = applicationContext,
                    peerName = peerName,
                    host = tHost,
                    port = tPort,
                    pin = pin,
                    token = token,
                    rateLimitBps = repo.transferRateBps(),
                )
                transfer.uploadUris(uris, currentPath) { ok, msg ->
                    if (tid.isNotEmpty()) EmbeddedNetwork.get(this).closeTunnel(tid)
                    runOnUiThread {
                        showBanner(msg)
                        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
                        if (ok) loadDir(currentPath)
                    }
                }
            } catch (ex: Exception) {
                if (tid.isNotEmpty()) EmbeddedNetwork.get(this).closeTunnel(tid)
                runOnUiThread {
                    showBanner("上传失败：${ex.message}")
                }
            }
        }, "brioche-fb-up-init").start()
    }

    private fun startDownload(paths: List<String>, label: String) {
        showBanner("正在下载「$label」到手机 Download/Brioche/…（可在「传输」页查看进度）")
        Thread({
            var tid = ""
            try {
                val (tHost, tPort) = resolveTransferEndpoint().also { tid = it.third }
                val transfer = FileTransferClient(
                    context = applicationContext,
                    peerName = peerName,
                    host = tHost,
                    port = tPort,
                    pin = pin,
                    token = token,
                    rateLimitBps = repo.transferRateBps(),
                )
                transfer.downloadRemotePaths(paths) { _, msg ->
                    if (tid.isNotEmpty()) EmbeddedNetwork.get(this).closeTunnel(tid)
                    runOnUiThread {
                        showBanner(msg)
                        Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (ex: Exception) {
                if (tid.isNotEmpty()) EmbeddedNetwork.get(this).closeTunnel(tid)
                runOnUiThread {
                    showBanner("下载失败：${ex.message}")
                }
            }
        }, "brioche-fb-dl-init").start()
    }

    private fun resolveTransferEndpoint(): Triple<String, Int, String> {
        return if (tailscaleIp.isNotEmpty()) {
            val (localPort, tid) = EmbeddedNetwork.get(this).openTunnel(tailscaleIp, EmbeddedNetwork.STREAM_PORT)
            Triple("127.0.0.1", localPort, tid)
        } else {
            Triple(host, port, "")
        }
    }

    private fun formatSize(bytes: Long): String {
        if (bytes < 1024) return "$bytes B"
        val kb = bytes / 1024.0
        if (kb < 1024) return String.format(Locale.US, "%.1f KB", kb)
        val mb = kb / 1024.0
        if (mb < 1024) return String.format(Locale.US, "%.1f MB", mb)
        return String.format(Locale.US, "%.2f GB", mb / 1024.0)
    }

    override fun onDestroy() {
        super.onDestroy()
        io.execute {
            browser?.close()
            if (browserTunnelId.isNotEmpty()) {
                EmbeddedNetwork.get(this).closeTunnel(browserTunnelId)
            }
        }
        io.shutdown()
    }
}
