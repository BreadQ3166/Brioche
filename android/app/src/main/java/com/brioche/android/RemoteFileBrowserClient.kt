package com.brioche.android

import org.json.JSONArray
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.IOException
import javax.net.ssl.SSLSocket

data class RemoteRoot(
    val name: String,
    val path: String,
)

data class RemoteEntry(
    val name: String,
    val path: String,
    val isDir: Boolean,
    val size: Long,
    val mtime: Long,
)

data class RemoteListing(
    val path: String,
    val parent: String,
    val entries: List<RemoteEntry>,
)

/**
 * Persistent authenticated TLS client for Brioche's `file_browser` protocol
 * (`fb_list`, `fb_mkdir`, `fb_rename`, `fb_delete`, `fb_bye`).
 */
class RemoteFileBrowserClient(
    private val host: String,
    private val port: Int,
    private val pin: String,
    private val token: String,
) {
    private var socket: SSLSocket? = null
    private var out: BufferedOutputStream? = null
    private var inp: DataInputStream? = null
    private val lock = Any()

    var remotePlatform: String = "Desktop"
        private set
    var roots: List<RemoteRoot> = emptyList()
        private set

    fun connect() {
        synchronized(lock) {
            val (sock, _) = BriocheProtocol.openPinnedTls(host, port, pin, connectTimeoutMs = 8000, readTimeoutMs = 15000)
            socket = sock
            out = BufferedOutputStream(sock.outputStream)
            inp = DataInputStream(BufferedInputStream(sock.inputStream, 65536))
            val req = JSONObject().apply {
                put("type", "file_browser")
                put("version", BriocheProtocol.VERSION)
                put("token", token)
            }
            sendControl(req)
            val reply = readControl()
            if (reply.optString("type") != "fb_ready") {
                close()
                throw IOException(reply.optString("message", "对方未开启远端文件浏览权限"))
            }
            remotePlatform = reply.optString("platform", "Desktop")
            roots = parseRoots(reply.optJSONArray("roots"))
        }
    }

    fun listDir(path: String = ""): RemoteListing {
        val resp = call(JSONObject().apply {
            put("type", "fb_list")
            put("path", path)
        })
        val entries = mutableListOf<RemoteEntry>()
        val arr = resp.optJSONArray("entries")
        if (arr != null) {
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                entries.add(
                    RemoteEntry(
                        name = o.optString("name", ""),
                        path = o.optString("path", ""),
                        isDir = o.optBoolean("is_dir", false),
                        size = o.optLong("size", 0L),
                        mtime = o.optLong("mtime", 0L),
                    )
                )
            }
        }
        return RemoteListing(
            path = resp.optString("path", path),
            parent = resp.optString("parent", ""),
            entries = entries,
        )
    }

    fun mkdir(parent: String, name: String): String {
        val resp = call(JSONObject().apply {
            put("type", "fb_mkdir")
            put("parent", parent)
            put("name", name)
        })
        return resp.optString("path", "")
    }

    fun rename(path: String, newName: String): String {
        val resp = call(JSONObject().apply {
            put("type", "fb_rename")
            put("path", path)
            put("new_name", newName)
        })
        return resp.optString("path", "")
    }

    fun delete(path: String) {
        call(JSONObject().apply {
            put("type", "fb_delete")
            put("path", path)
        })
    }

    fun close() {
        synchronized(lock) {
            try {
                out?.let {
                    it.write(BriocheProtocol.packControl(JSONObject().apply { put("type", "fb_bye") }))
                    it.flush()
                }
            } catch (_: Exception) {
            }
            try {
                socket?.close()
            } catch (_: Exception) {
            }
            socket = null
            out = null
            inp = null
        }
    }

    private fun call(payload: JSONObject): JSONObject {
        synchronized(lock) {
            sendControl(payload)
            val resp = readControl()
            val t = resp.optString("type")
            if (t == "fb_error" || t == "error") {
                throw IOException(resp.optString("message", "目录操作失败"))
            }
            return resp
        }
    }

    private fun sendControl(obj: JSONObject) {
        val o = out ?: throw IOException("文件浏览连接已断开")
        o.write(BriocheProtocol.packControl(obj))
        o.flush()
    }

    private fun readControl(): JSONObject {
        val i = inp ?: throw IOException("文件浏览连接已断开")
        val frame = BriocheProtocol.readFrame(i)
        if (frame.kind != BriocheProtocol.CONTROL) {
            throw IOException("无效目录响应")
        }
        return JSONObject(String(frame.payload, Charsets.UTF_8))
    }

    private fun parseRoots(arr: JSONArray?): List<RemoteRoot> {
        if (arr == null) return emptyList()
        val list = mutableListOf<RemoteRoot>()
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i) ?: continue
            val name = o.optString("name", "")
            val path = o.optString("path", "")
            if (name.isNotEmpty() && path.isNotEmpty()) {
                list.add(RemoteRoot(name, path))
            }
        }
        return list
    }
}
