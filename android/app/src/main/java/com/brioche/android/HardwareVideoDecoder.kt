package com.brioche.android

import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import android.view.Surface
import java.util.concurrent.atomic.AtomicInteger

/**
 * Zero-copy Android hardware MediaCodec H.264/HEVC decoder bound directly to a SurfaceView.
 * Decoded frames go straight from the SoC video decoder to GPU display composition.
 */
class HardwareVideoDecoder(
    private val surface: Surface,
    codecName: String,
    width: Int,
    height: Int,
) {
    private val mimeType = if (codecName.equals("hevc", ignoreCase = true)) {
        MediaFormat.MIMETYPE_VIDEO_HEVC
    } else {
        MediaFormat.MIMETYPE_VIDEO_AVC
    }

    private var codec: MediaCodec? = null
    private val bufferInfo = MediaCodec.BufferInfo()
    val decodedFrames = AtomicInteger(0)
    private var ptsUs = 0L

    init {
        val format = MediaFormat.createVideoFormat(mimeType, width, height).apply {
            setInteger(MediaFormat.KEY_PRIORITY, 0)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                setInteger(MediaFormat.KEY_LOW_LATENCY, 1)
            }
            setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 2 * 1024 * 1024)
        }
        val mc = MediaCodec.createDecoderByType(mimeType)
        mc.configure(format, surface, null, 0)
        mc.start()
        codec = mc
    }

    @Synchronized
    fun decode(accessUnit: ByteArray) {
        val mc = codec ?: return
        val inIdx = mc.dequeueInputBuffer(10_000)
        if (inIdx >= 0) {
            val inputBuf = mc.getInputBuffer(inIdx)
            if (inputBuf != null) {
                inputBuf.clear()
                if (accessUnit.size <= inputBuf.capacity()) {
                    inputBuf.put(accessUnit)
                    ptsUs += 16_666L
                    mc.queueInputBuffer(inIdx, 0, accessUnit.size, ptsUs, 0)
                } else {
                    mc.queueInputBuffer(inIdx, 0, 0, 0, 0)
                }
            }
        }
        while (true) {
            val outIdx = mc.dequeueOutputBuffer(bufferInfo, 0)
            if (outIdx >= 0) {
                // render = true -> zero-copy direct presentation on SurfaceView!
                mc.releaseOutputBuffer(outIdx, true)
                decodedFrames.incrementAndGet()
            } else {
                break
            }
        }
    }

    @Synchronized
    fun release() {
        val mc = codec ?: return
        codec = null
        try {
            mc.stop()
        } catch (_: Exception) {
        }
        try {
            mc.release()
        } catch (_: Exception) {
        }
    }
}
