package com.brioche.android

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.DataInputStream

class BriocheProtocolTest {
    @Test
    fun parseLanTargetAcceptsPrivateIpv4AndCustomPort() {
        assertEquals(Pair("192.168.1.8", 47995), BriocheProtocol.parseLanTarget("192.168.1.8"))
        assertEquals(Pair("10.27.56.170", 48000), BriocheProtocol.parseLanTarget("10.27.56.170:48000"))
        assertEquals(Pair("100.64.0.9", 47995), BriocheProtocol.parseLanTarget("100.64.0.9"))
    }

    @Test
    fun parseUriRoundTripMatchesDesktopFormat() {
        val pin = "a".repeat(64)
        val token = "A".repeat(43)
        val uri = BriocheProtocol.formatUri("10.27.56.170", 47995, pin, token)
        val parsed = BriocheProtocol.parseUri(uri)
        assertEquals("10.27.56.170", parsed.host)
        assertEquals(47995, parsed.port)
        assertEquals(pin, parsed.pin)
        assertEquals(token, parsed.token)
    }

    @Test
    fun pairingCodeMatchesDesktopPythonExactVector() {
        val pin = "0123456789abcdef".repeat(4)
        val nonce = "fedcba9876543210".repeat(2)
        val clientId = "00112233445566778899aabbccddeeff"
        val code1 = BriocheProtocol.pairingCode(pin, nonce, clientId)
        val code2 = BriocheProtocol.pairingCode(pin, nonce, clientId)
        assertEquals("267826", code1)
        assertEquals(code1, code2)
    }

    @Test
    fun packAndReadControlFrameRoundTrips() {
        val payload = JSONObject().apply {
            put("type", "ping")
            put("stamp", 123.5)
        }
        val packed = BriocheProtocol.packControl(payload)
        val msg = BriocheProtocol.readFrame(DataInputStream(ByteArrayInputStream(packed)))
        assertEquals(BriocheProtocol.CONTROL, msg.kind)
        val decoded = JSONObject(String(msg.payload, Charsets.UTF_8))
        assertEquals("ping", decoded.getString("type"))
    }

    @Test
    fun lanBeaconParsesValidPayloadAndRejectsForeignProduct() {
        val valid = """{"product":"Brioche","version":2,"name":"Ubuntu-3090","platform":"Linux","port":47995,"pin_prefix":"abcdef0123456789","busy":false}"""
        val parsed = LanDiscovery.parseBeacon(valid.toByteArray(Charsets.UTF_8), "10.27.56.170")
        assertNotNull(parsed)
        assertEquals("Ubuntu-3090", parsed!!.name)
        assertEquals(47995, parsed.port)

        val bad = """{"product":"OtherApp","port":47995,"name":"X"}"""
        assertNull(LanDiscovery.parseBeacon(bad.toByteArray(Charsets.UTF_8), "10.27.56.170"))
    }
}
