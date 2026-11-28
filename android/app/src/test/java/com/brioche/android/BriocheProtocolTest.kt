package com.brioche.android

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.DataInputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder

class BriocheProtocolTest {
    @Test
    fun pairingCodeMatchesPythonReferenceVector() {
        val pin = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
        val nonce = "fedcba9876543210fedcba9876543210"
        val clientId = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assertEquals("694777", BriocheProtocol.pairingCode(pin, nonce, clientId))
    }

    @Test
    fun parseLanTargetAcceptsPrivateAndTailscaleIps() {
        assertEquals(Pair("192.168.1.8", 47995), BriocheProtocol.parseLanTarget("192.168.1.8"))
        assertEquals(Pair("10.27.56.170", 48000), BriocheProtocol.parseLanTarget("10.27.56.170:48000"))
        assertEquals(Pair("100.64.1.2", 47995), BriocheProtocol.parseLanTarget("100.64.1.2"))
    }

    @Test(expected = IllegalArgumentException::class)
    fun parseLanTargetRejectsPublicIp() {
        BriocheProtocol.parseLanTarget("8.8.8.8")
    }

    @Test
    fun uriRoundTripWorks() {
        val pin = "a".repeat(64)
        val token = "B".repeat(43)
        val uri = BriocheProtocol.formatUri("192.168.1.10", 47995, pin, token)
        val parsed = BriocheProtocol.parseUri(uri)
        assertEquals("192.168.1.10", parsed.host)
        assertEquals(47995, parsed.port)
        assertEquals(pin, parsed.pin)
        assertEquals(token, parsed.token)
    }

    @Test
    fun framePackAndReadRoundTrip() {
        val msg = JSONObject().apply {
            put("type", "ping")
            put("stamp", 123.45)
        }
        val packed = BriocheProtocol.packControl(msg)
        val wire = BriocheProtocol.readFrame(DataInputStream(ByteArrayInputStream(packed)))
        assertEquals(BriocheProtocol.CONTROL, wire.kind)
        val decoded = JSONObject(String(wire.payload, Charsets.UTF_8))
        assertEquals("ping", decoded.getString("type"))
    }

    @Test
    fun fileAndAudioFramesPackAndRead() {
        val filePayload = ByteArray(1024) { (it % 251).toByte() }
        val packedFile = BriocheProtocol.packFrame(BriocheProtocol.FILE, filePayload)
        val wireFile = BriocheProtocol.readFrame(DataInputStream(ByteArrayInputStream(packedFile)))
        assertEquals(BriocheProtocol.FILE, wireFile.kind)
        assertEquals(1024, wireFile.payload.size)

        val audioPayload = ByteArray(160) { 0x42 }
        val packedAudio = BriocheProtocol.packFrame(BriocheProtocol.AUDIO, audioPayload)
        val wireAudio = BriocheProtocol.readFrame(DataInputStream(ByteArrayInputStream(packedAudio)))
        assertEquals(BriocheProtocol.AUDIO, wireAudio.kind)
        assertEquals(160, wireAudio.payload.size)
    }

    @Test
    fun beaconParserExtractsValidHost() {
        val raw = """{"product":"Brioche","name":"Workstation","platform":"Linux","port":47995,"pin_prefix":"abcdef123456","busy":false}"""
            .toByteArray(Charsets.UTF_8)
        val dev = LanDiscovery.parseBeacon(raw, "192.168.1.50", nowMs = 1000L)
        assertNotNull(dev)
        assertEquals("Workstation", dev!!.name)
        assertEquals("192.168.1.50", dev.ip)
        assertEquals(47995, dev.port)
    }

    @Test
    fun opusHeadFollowsRfc7845() {
        val head = OpusAudioPlayer.createOpusHead(48_000, 2, 312)
        assertEquals(19, head.size)
        assertEquals("OpusHead", String(head, 0, 8, Charsets.US_ASCII))
        val bb = ByteBuffer.wrap(head).order(ByteOrder.LITTLE_ENDIAN)
        assertEquals(1.toByte(), bb.get(8))
        assertEquals(2.toByte(), bb.get(9))
        assertEquals(312.toShort(), bb.getShort(10))
        assertEquals(48_000, bb.getInt(12))
        assertEquals(0.toShort(), bb.getShort(16))
        assertEquals(0.toByte(), bb.get(18))
    }

    @Test
    fun embeddedNetworkParsesPeersAndDerivesDeterministicIps() {
        val parsed = EmbeddedNetwork.parsePeerUrls("tcp://1.2.3.4:11010, bad://x udp://5.6.7.8:11010")
        assertEquals(listOf("tcp://1.2.3.4:11010", "udp://5.6.7.8:11010"), parsed)
        assertEquals(EmbeddedNetwork.DEFAULT_PEERS, EmbeddedNetwork.parsePeerUrls(""))
        val (vip, pip) = EmbeddedNetwork.deriveNodeIps("00000000-1111-2222-3333-444444444444")
        assertTrue(vip.startsWith("10.144."))
        assertTrue(pip.startsWith("10.145."))
        assertEquals("android-pixel-8-pro", EmbeddedNetwork.sanitizeHostname("Android Pixel 8 Pro!"))
    }
}
