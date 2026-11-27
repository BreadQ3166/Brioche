package com.brioche.android

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.provider.MediaStore
import android.provider.OpenableColumns
import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.io.OutputStream
import java.security.MessageDigest
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicBoolean
import javax.net.ssl.SSLSocket

data class TransferItemRecord(
    val id: String,
    val peerName: String,
    val direction: String, // "upload" or "download"
    val currentFile: String,
    val completedBytes: Long,
    val totalBytes: Long,
    val speedBps: Long,
    val status: String,
    val finished: Boolean,
    val cancelFn: (() -> Unit)? = null,
)

/** Singleton live transfer board shared across FileBrowserActivity, RemoteSessionActivity, and MainActivity. */
object TransferStore {
    private val items = CopyOnWriteArrayList<TransferItemRecord>()
    private val listeners = CopyOnWriteArrayList<() -> Unit>()

    fun addListener(fn: () -> Unit) {
        listeners.add(fn)
    }

    fun removeListener(fn: () -> Unit) {
        listeners.remove(fn)
    }

    fun snapshot(): List<TransferItemRecord> = items.toList()

    fun upsert(record: TransferItemRecord) {
        val idx = items.indexOfFirst { it.id == record.id }
        if (idx >= 0) {
            items[idx] = record
        } else {
            items.add(0, record)
            while (items.size > 50) {
                items.removeAt(items.lastIndex)
            }
        }
        listeners.forEach { it() }
    }

    fun clearFinished() {
        items.removeAll { it.finished }
        listeners.forEach { it() }
    }
}

/**
 * Authenticated Brioche `file_transfer` client (upload & download over pinned TLS,
 * FILE kind=3 chunks, SHA-256 verification, optional rate limiting, and cancellation).
 */
class FileTransferClient(
    private val context: Context,
    private val peerName: String,
    private val host: String,
    private val port: Int,
    private val pin: String,
    private val token: String,
    private val rateLimitBps: Long = 20L * 1024L * 1024L,
) {
    private val cancelled = AtomicBoolean(false)
    @Volatile
    private var activeSocket: SSLSocket? = null

    fun cancel() {
        cancelled.set(true)
        try {
            activeSocket?.close()
        } catch (_: Exception) {
        }
    }

    fun uploadUris(uris: List<Uri>, targetDir: String, onDone: (Boolean, String) -> Unit = { _, _ -> }) {
        val jobId = BriocheProtocol.randomHex(8)
        Thread({
            val startedAt = System.currentTimeMillis()
            fun emit(file: String, done: Long, total: Long, status: String, finished: Boolean = false) {
                val elapsedSec = ((System.currentTimeMillis() - startedAt) / 1000.0).coerceAtLeast(0.05)
                val speed = if (done > 0) (done / elapsedSec).toLong() else 0L
                TransferStore.upsert(
                    TransferItemRecord(
                        id = jobId,
                        peerName = peerName,
                        direction = "upload",
                        currentFile = file,
                        completedBytes = done,
                        totalBytes = total,
                        speedBps = speed,
                        status = status,
                        finished = finished,
                        cancelFn = if (finished) null else { { cancel() } },
                    )
                )
            }

            try {
                emit("准备上传…", 0L, 0L, "正在连接远端目录…")
                val entries = uris.map { queryUriMeta(it) }
                if (entries.isEmpty()) throw IOException("未选择任何文件")
                val totalBytes = entries.sumOf { it.second }
                val filesJson = JSONArray()
                entries.forEach { (name, size, _) ->
                    filesJson.put(JSONObject().apply {
                        put("name", name)
                        put("size", size)
                    })
                }
                val (sock, _) = BriocheProtocol.openPinnedTls(host, port, pin, connectTimeoutMs = 10_000, readTimeoutMs = 130_000)
                activeSocket = sock
                sock.use { s ->
                    val out = BufferedOutputStream(s.outputStream, 128 * 1024)
                    val inp = DataInputStream(BufferedInputStream(s.inputStream, 65536))
                    val req = JSONObject().apply {
                        put("type", "file_transfer")
                        put("version", BriocheProtocol.VERSION)
                        put("token", token)
                        put("direction", "upload")
                        put("files", filesJson)
                        if (targetDir.isNotEmpty()) put("target_dir", targetDir)
                    }
                    out.write(BriocheProtocol.packControl(req))
                    out.flush()
                    expectControl(inp, "file_ready")

                    var transferred = 0L
                    var lastEmitMs = 0L
                    val buf = ByteArray(128 * 1024)
                    for ((name, size, uri) in entries) {
                        if (cancelled.get()) throw IOException("已取消上传")
                        val sha = MessageDigest.getInstance("SHA-256")
                        var remaining = size
                        context.contentResolver.openInputStream(uri)?.use { stream ->
                            while (remaining > 0) {
                                if (cancelled.get()) throw IOException("已取消上传")
                                val toRead = minOf(buf.size.toLong(), remaining).toInt()
                                val n = stream.read(buf, 0, toRead)
                                if (n <= 0) throw IOException("源文件读取提前结束：$name")
                                sha.update(buf, 0, n)
                                val chunk = if (n == buf.size) buf else buf.copyOfRange(0, n)
                                out.write(BriocheProtocol.packFrame(BriocheProtocol.FILE, chunk))
                                remaining -= n
                                transferred += n
                                val now = System.currentTimeMillis()
                                if (now - lastEmitMs >= 120L) {
                                    lastEmitMs = now
                                    emit(name, transferred, totalBytes, "上传中")
                                }
                                if (rateLimitBps > 0) {
                                    val expectedMs = (transferred * 1000L) / rateLimitBps
                                    val actualMs = now - startedAt
                                    if (expectedMs > actualMs + 10L) {
                                        Thread.sleep((expectedMs - actualMs).coerceAtMost(200L))
                                    }
                                }
                            }
                        } ?: throw IOException("无法读取手机文件：$name")
                        val hex = sha.digest().joinToString("") { "%02x".format(it) }
                        out.write(BriocheProtocol.packControl(JSONObject().apply {
                            put("type", "file_end")
                            put("sha256", hex)
                        }))
                        out.flush()
                        expectControl(inp, "file_saved")
                    }
                    out.write(BriocheProtocol.packControl(JSONObject().apply { put("type", "file_batch_done") }))
                    out.flush()
                    expectControl(inp, "file_batch_ack")
                    emit("共 ${entries.size} 个文件", totalBytes, totalBytes, "上传完成 · SHA256 校验通过", finished = true)
                    onDone(true, "已上传 ${entries.size} 个文件")
                }
            } catch (ex: Exception) {
                val msg = if (cancelled.get()) "已取消上传" else (ex.message ?: "上传失败")
                emit(msg, 0L, 0L, msg, finished = true)
                onDone(false, msg)
            } finally {
                activeSocket = null
            }
        }, "brioche-upload-$jobId").apply {
            isDaemon = true
            start()
        }
    }

    fun downloadRemotePaths(remotePaths: List<String>, onDone: (Boolean, String) -> Unit = { _, _ -> }) {
        val jobId = BriocheProtocol.randomHex(8)
        Thread({
            val startedAt = System.currentTimeMillis()
            fun emit(file: String, done: Long, total: Long, status: String, finished: Boolean = false) {
                val elapsedSec = ((System.currentTimeMillis() - startedAt) / 1000.0).coerceAtLeast(0.05)
                val speed = if (done > 0) (done / elapsedSec).toLong() else 0L
                TransferStore.upsert(
                    TransferItemRecord(
                        id = jobId,
                        peerName = peerName,
                        direction = "download",
                        currentFile = file,
                        completedBytes = done,
                        totalBytes = total,
                        speedBps = speed,
                        status = status,
                        finished = finished,
                        cancelFn = if (finished) null else { { cancel() } },
                    )
                )
            }

            try {
                emit("准备下载…", 0L, 0L, "正在连接远端主机…")
                val (sock, _) = BriocheProtocol.openPinnedTls(host, port, pin, connectTimeoutMs = 10_000, readTimeoutMs = 130_000)
                activeSocket = sock
                sock.use { s ->
                    val out = BufferedOutputStream(s.outputStream)
                    val inp = DataInputStream(BufferedInputStream(s.inputStream, 128 * 1024))
                    val req = JSONObject().apply {
                        put("type", "file_transfer")
                        put("version", BriocheProtocol.VERSION)
                        put("token", token)
                        put("direction", "download")
                        put("remote_paths", JSONArray(remotePaths))
                    }
                    out.write(BriocheProtocol.packControl(req))
                    out.flush()
                    val offer = expectControl(inp, "file_offer")
                    val filesArr = offer.optJSONArray("files") ?: throw IOException("远端未提供文件清单")
                    val manifest = mutableListOf<Triple<String, Long, String>>()
                    for (i in 0 until filesArr.length()) {
                        val o = filesArr.optJSONObject(i) ?: continue
                        val name = o.optString("name", "")
                        val size = o.optLong("size", -1L)
                        val rel = o.optString("relpath", name)
                        if (name.isEmpty() || size < 0) throw IOException("远端文件清单无效")
                        manifest.add(Triple(name, size, rel))
                    }
                    val totalBytes = manifest.sumOf { it.second }
                    out.write(BriocheProtocol.packControl(JSONObject().apply { put("type", "file_ready") }))
                    out.flush()

                    var transferred = 0L
                    var lastEmitMs = 0L
                    val savedNames = mutableListOf<String>()
                    for ((name, size, rel) in manifest) {
                        if (cancelled.get()) throw IOException("已取消下载")
                        val sha = MessageDigest.getInstance("SHA-256")
                        val tempFile = File(context.cacheDir, ".brioche-dl-${BriocheProtocol.randomHex(8)}.part")
                        try {
                            var received = 0L
                            FileOutputStream(tempFile).use { fos ->
                                while (received < size) {
                                    if (cancelled.get()) throw IOException("已取消下载")
                                    val frame = BriocheProtocol.readFrame(inp)
                                    if (frame.kind != BriocheProtocol.FILE) {
                                        throw IOException("文件传输提前中断")
                                    }
                                    fos.write(frame.payload)
                                    sha.update(frame.payload)
                                    received += frame.payload.size
                                    transferred += frame.payload.size
                                    val now = System.currentTimeMillis()
                                    if (now - lastEmitMs >= 120L) {
                                        lastEmitMs = now
                                        emit(rel, transferred, totalBytes, "下载中")
                                    }
                                }
                                fos.flush()
                            }
                            val endMsg = expectControl(inp, "file_end")
                            val expectedSha = endMsg.optString("sha256", "")
                            val actualSha = sha.digest().joinToString("") { "%02x".format(it) }
                            if (expectedSha.isEmpty() || !expectedSha.equals(actualSha, ignoreCase = true)) {
                                throw IOException("SHA256 校验失败：$name")
                            }
                            val savedLabel = saveToPublicDownloads(tempFile, name)
                            savedNames.add(savedLabel)
                            out.write(BriocheProtocol.packControl(JSONObject().apply {
                                put("type", "file_saved")
                                put("name", savedLabel)
                            }))
                            out.flush()
                        } finally {
                            tempFile.delete()
                        }
                    }
                    expectControl(inp, "file_batch_done")
                    out.write(BriocheProtocol.packControl(JSONObject().apply { put("type", "file_batch_ack") }))
                    out.flush()
                    emit("已保存到 Download/Brioche（${savedNames.size} 个）", totalBytes, totalBytes, "下载完成 · SHA256 校验通过", finished = true)
                    onDone(true, "已保存到手机 Download/Brioche/（共 ${savedNames.size} 个文件）")
                }
            } catch (ex: Exception) {
                val msg = if (cancelled.get()) "已取消下载" else (ex.message ?: "下载失败")
                emit(msg, 0L, 0L, msg, finished = true)
                onDone(false, msg)
            } finally {
                activeSocket = null
            }
        }, "brioche-download-$jobId").apply {
            isDaemon = true
            start()
        }
    }

    private fun saveToPublicDownloads(sourceFile: File, displayName: String): String {
        val safe = displayName.replace(Regex("[/\\\\:*?\"<>|]"), "_").ifEmpty { "brioche_file" }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val values = ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, safe)
                put(MediaStore.Downloads.RELATIVE_PATH, "${Environment.DIRECTORY_DOWNLOADS}/Brioche")
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
            val resolver = context.contentResolver
            val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
                ?: throw IOException("无法创建下载文件：$safe")
            try {
                resolver.openOutputStream(uri)?.use { dst ->
                    sourceFile.inputStream().use { src -> src.copyTo(dst, 65536) }
                } ?: throw IOException("无法写入下载目录")
                values.clear()
                values.put(MediaStore.Downloads.IS_PENDING, 0)
                resolver.update(uri, values, null, null)
                return safe
            } catch (ex: Exception) {
                resolver.delete(uri, null, null)
                throw ex
            }
        } else {
            val dir = File(
                context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS) ?: context.filesDir,
                "Brioche"
            )
            dir.mkdirs()
            var target = File(dir, safe)
            var idx = 1
            while (target.exists()) {
                val dot = safe.lastIndexOf('.')
                val alt = if (dot > 0) "${safe.substring(0, dot)} ($idx)${safe.substring(dot)}" else "$safe ($idx)"
                target = File(dir, alt)
                idx++
            }
            sourceFile.inputStream().use { src ->
                FileOutputStream(target).use { dst -> src.copyTo(dst, 65536) }
            }
            return target.name
        }
    }

    private fun queryUriMeta(uri: Uri): Triple<String, Long, Uri> {
        var name = "upload_file"
        var size = -1L
        context.contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c ->
            if (c.moveToFirst()) {
                val ni = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                val si = c.getColumnIndex(OpenableColumns.SIZE)
                if (ni >= 0 && !c.isNull(ni)) name = c.getString(ni)
                if (si >= 0 && !c.isNull(si)) size = c.getLong(si)
            }
        }
        if (size < 0) {
            // Fallback: count bytes
            context.contentResolver.openInputStream(uri)?.use { s ->
                val tmp = ByteArray(32768)
                var total = 0L
                while (true) {
                    val n = s.read(tmp)
                    if (n <= 0) break
                    total += n
                }
                size = total
            }
        }
        val cleanName = name.replace(Regex("[/\\\\:*?\"<>|]"), "_").trim().ifEmpty { "upload_file" }
        return Triple(cleanName, size.coerceAtLeast(0L), uri)
    }

    private fun expectControl(inp: DataInputStream, expected: String): JSONObject {
        val frame = BriocheProtocol.readFrame(inp)
        if (frame.kind != BriocheProtocol.CONTROL) {
            throw IOException("无效文件传输响应")
        }
        val json = JSONObject(String(frame.payload, Charsets.UTF_8))
        val type = json.optString("type")
        if (type == "file_error" || type == "error") {
            throw IOException(json.optString("message", "远端拒绝文件传输"))
        }
        if (type != expected) {
            throw IOException("文件传输协议不匹配（期望 $expected，收到 $type）")
        }
        return json
    }
}
