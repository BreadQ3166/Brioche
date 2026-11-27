package com.brioche.android

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTrack
import android.media.MediaCodec
import android.media.MediaFormat
import android.os.Build
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Low-latency Opus audio decoder & playback using Android built-in MediaCodec (MIMETYPE_AUDIO_OPUS)
 * + AudioTrack (48kHz stereo PCM16). Matches Brioche desktop `src/brioche/audio.py` (48kHz/2ch/20ms).
 */
class OpusAudioPlayer(
    private val sampleRate: Int = 48_000,
    private val channels: Int = 2,
) {
    companion object {
        const val PRE_SKIP_SAMPLES = 312
        const val SEEK_PREROLL_NS = 80_000_000L

        /** Build standard 19-byte RFC 7845 OpusHead CSD-0 packet. */
        fun createOpusHead(sampleRate: Int = 48_000, channels: Int = 2, preSkip: Int = PRE_SKIP_SAMPLES): ByteArray {
            val buf = ByteBuffer.allocate(19).order(ByteOrder.LITTLE_ENDIAN)
            buf.put("OpusHead".toByteArray(Charsets.US_ASCII))
            buf.put(1.toByte()) // version
            buf.put(channels.toByte())
            buf.putShort(preSkip.toShort())
            buf.putInt(sampleRate)
            buf.putShort(0.toShort()) // output gain
            buf.put(0.toByte()) // mapping family 0 (mono/stereo)
            return buf.array()
        }

        fun longLittleEndian(value: Long): ByteArray {
            return ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putLong(value).array()
        }
    }

    private var codec: MediaCodec? = null
    private var track: AudioTrack? = null
    private val bufferInfo = MediaCodec.BufferInfo()
    private val muted = AtomicBoolean(false)
    private var ptsUs = 0L

    var isReady: Boolean = false
        private set

    init {
        try {
            val format = MediaFormat.createAudioFormat(MediaFormat.MIMETYPE_AUDIO_OPUS, sampleRate, channels).apply {
                setByteBuffer("csd-0", ByteBuffer.wrap(createOpusHead(sampleRate, channels)))
                val preSkipNs = (PRE_SKIP_SAMPLES * 1_000_000_000L) / sampleRate
                setByteBuffer("csd-1", ByteBuffer.wrap(longLittleEndian(preSkipNs)))
                setByteBuffer("csd-2", ByteBuffer.wrap(longLittleEndian(SEEK_PREROLL_NS)))
            }
            val mc = MediaCodec.createDecoderByType(MediaFormat.MIMETYPE_AUDIO_OPUS)
            mc.configure(format, null, null, 0)
            mc.start()
            codec = mc

            val channelMask = if (channels == 1) AudioFormat.CHANNEL_OUT_MONO else AudioFormat.CHANNEL_OUT_STEREO
            val minBuf = AudioTrack.getMinBufferSize(sampleRate, channelMask, AudioFormat.ENCODING_PCM_16BIT)
                .coerceAtLeast(3840 * 2)
            val builder = AudioTrack.Builder()
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_MUSIC)
                        .build()
                )
                .setAudioFormat(
                    AudioFormat.Builder()
                        .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                        .setSampleRate(sampleRate)
                        .setChannelMask(channelMask)
                        .build()
                )
                .setBufferSizeInBytes(minBuf)
                .setTransferMode(AudioTrack.MODE_STREAM)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                builder.setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
            }
            track = builder.build().also { it.play() }
            isReady = true
        } catch (_: Exception) {
            release()
        }
    }

    fun setMuted(mute: Boolean) {
        muted.set(mute)
        try {
            track?.setVolume(if (mute) 0f else 1f)
        } catch (_: Exception) {
        }
    }

    fun isMuted(): Boolean = muted.get()

    @Synchronized
    fun decodeAndPlay(opusPacket: ByteArray) {
        val mc = codec ?: return
        val at = track ?: return
        if (opusPacket.isEmpty()) return
        try {
            val inIdx = mc.dequeueInputBuffer(5_000)
            if (inIdx >= 0) {
                val inputBuf = mc.getInputBuffer(inIdx)
                if (inputBuf != null) {
                    inputBuf.clear()
                    if (opusPacket.size <= inputBuf.capacity()) {
                        inputBuf.put(opusPacket)
                        ptsUs += 20_000L
                        mc.queueInputBuffer(inIdx, 0, opusPacket.size, ptsUs, 0)
                    } else {
                        mc.queueInputBuffer(inIdx, 0, 0, 0, 0)
                    }
                }
            }
            while (true) {
                val outIdx = mc.dequeueOutputBuffer(bufferInfo, 0)
                if (outIdx >= 0) {
                    val outBuf = mc.getOutputBuffer(outIdx)
                    if (outBuf != null && bufferInfo.size > 0 && !muted.get()) {
                        outBuf.position(bufferInfo.offset)
                        outBuf.limit(bufferInfo.offset + bufferInfo.size)
                        val pcm = ByteArray(bufferInfo.size)
                        outBuf.get(pcm)
                        at.write(pcm, 0, pcm.size, AudioTrack.WRITE_NON_BLOCKING)
                    }
                    mc.releaseOutputBuffer(outIdx, false)
                } else {
                    break
                }
            }
        } catch (_: Exception) {
        }
    }

    @Synchronized
    fun release() {
        isReady = false
        try {
            codec?.stop()
        } catch (_: Exception) {
        }
        try {
            codec?.release()
        } catch (_: Exception) {
        }
        codec = null
        try {
            track?.pause()
            track?.flush()
            track?.release()
        } catch (_: Exception) {
        }
        track = null
    }
}
