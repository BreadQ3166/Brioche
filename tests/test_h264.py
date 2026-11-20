import struct
import time
import unittest
from brioche.h264 import START, access_unit_info, annexb_access_unit
from brioche.web_video import BrowserVideoFlow, H264_HEADER, VideoStreamError

SPS = b"\x67\x42\xc0\x33\x01"
PPS = b"\x68\x01"
IDR = b"\x65\x01"
DELTA = START+b"\x41\x01"
KEY = START+SPS+START+PPS+START+IDR


class H264Tests(unittest.TestCase):
    def test_annexb_profile_and_no_copy_fast_path(self):
        self.assertIs(annexb_access_unit(KEY), KEY)
        self.assertEqual(access_unit_info(KEY), {"key":True,"complete_key":True,"codec":"avc1.42c033"})
        self.assertFalse(access_unit_info(DELTA)["key"])

    def test_avcc_to_annexb_with_parameter_sets(self):
        extra = b"\x01\x42\xc0\x33\xff\xe1" + len(SPS).to_bytes(2,"big") + SPS + b"\x01" + len(PPS).to_bytes(2,"big") + PPS
        self.assertEqual(annexb_access_unit(len(IDR).to_bytes(4,"big")+IDR,extra,key=True), KEY)
        for value in (b"\x00\x00", b"\x00\x00\x00\x10\x41"):
            with self.assertRaises(ValueError):
                annexb_access_unit(value,extra)

    def flow(self):
        return BrowserVideoFlow({"codec":"h264","stream_id":0,"width":1280,"height":720,"fps_limit":60})

    def test_reference_chain_recovery_and_bounded_window(self):
        flow=self.flow()
        config, first, _=flow.packet(KEY)
        self.assertEqual(config["revision"],1)
        for _ in range(3):
            self.assertIsNotNone(flow.packet(DELTA)[1])
        self.assertEqual(len(flow.pending),4)
        self.assertIsNone(flow.packet(DELTA)[1])
        self.assertTrue(flow.waiting_key)
        flow.acknowledge(1)
        self.assertIsNone(flow.packet(DELTA)[1],"A delta escaped after dropping a reference")
        config, restored, _=flow.packet(KEY)
        epoch, frame, revision, key, stamp=H264_HEADER.unpack(restored[:H264_HEADER.size])
        self.assertEqual((epoch,revision,key),(0,2,1))
        self.assertEqual(len(flow.pending),1)
        flow.acknowledge(2) # stale ACK must not acknowledge the new IDR
        self.assertIn(frame,flow.pending)
        self.assertGreaterEqual(stamp,0)

    def test_new_screen_requires_a_complete_key(self):
        flow=self.flow()
        flow.packet(KEY)
        flow.reset({"codec":"h264","stream_id":1,"width":720,"height":1280,"fps_limit":60})
        self.assertIsNone(flow.packet(DELTA)[1])
        self.assertIsNone(flow.packet(START+IDR)[1])
        config, packet, _=flow.packet(KEY)
        self.assertEqual(config["stream_id"],1)
        self.assertEqual(H264_HEADER.unpack(packet[:21])[0],1)

    def test_stalled_browser_expires(self):
        flow=self.flow()
        now=time.monotonic()
        flow.packet(KEY,now)
        with self.assertRaises(VideoStreamError):
            flow.packet(DELTA,now+11)
