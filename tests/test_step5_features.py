import array
import math
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtWidgets import QApplication

from brioche.audio import (
    AudioCaptureStream,
    AudioPlayer,
    FRAME_SAMPLES,
    OpusDecoder,
    OpusEncoder,
    PCM_FRAME_BYTES,
    SAMPLE_RATE,
    scale_pcm_s16le,
)
from brioche.protocol import AUDIO, StreamConfig, pack_audio, read_message
from brioche.security import access_uri
from brioche.session import Host, Viewer


def until(predicate, seconds=5):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_sine_pcm(freq=440.0, amp=12000):
    samples = array.array("h")
    for i in range(FRAME_SAMPLES):
        val = int(amp * math.sin(2.0 * math.pi * freq * i / SAMPLE_RATE))
        samples.append(val)
        samples.append(val)
    return samples.tobytes()


class OpusAudioPipelineTests(unittest.TestCase):
    def test_opus_encode_decode_roundtrip_and_volume_scaling(self):
        pcm = make_sine_pcm()
        self.assertEqual(len(pcm), PCM_FRAME_BYTES)

        enc = OpusEncoder()
        dec = OpusDecoder()
        try:
            packets = []
            for _ in range(4):
                packets.extend(enc.encode_pcm(pcm))
            self.assertGreaterEqual(len(packets), 3)
            decoded = b"".join(dec.decode_packet(p) for p in packets)
            self.assertGreater(len(decoded), PCM_FRAME_BYTES)

            silenced = scale_pcm_s16le(pcm, 0.0)
            self.assertEqual(silenced, b"\x00" * len(pcm))
            halved = scale_pcm_s16le(pcm, 0.5)
            self.assertEqual(len(halved), len(pcm))
            orig = array.array("h", pcm)
            half = array.array("h", halved)
            self.assertLess(max(abs(x) for x in half), max(abs(x) for x in orig))
        finally:
            enc.close()
            dec.close()

    def test_capture_stream_and_audio_player_with_custom_pcm_io(self):
        pcm = make_sine_pcm()
        produced = [0]

        def reader(size):
            if produced[0] >= 8:
                time.sleep(0.05)
                return b""
            produced[0] += 1
            return pcm

        played = []
        cap = AudioCaptureStream(pcm_reader=reader)
        player = AudioPlayer(pcm_writer=played.append)
        self.assertTrue(cap.start())
        self.assertTrue(player.start())
        try:
            collected = []
            self.assertTrue(
                until(lambda: (collected.extend(cap.drain_packets()) or len(collected) >= 3), 3)
            )
            for pkt in collected:
                player.push(pkt)
            self.assertTrue(until(lambda: len(played) >= 2, 3))

            # Muting suppresses output writes cleanly
            player.set_muted(True)
            before = len(played)
            for pkt in collected:
                player.push(pkt)
            time.sleep(0.15)
            self.assertEqual(len(played), before)
        finally:
            cap.close()
            player.close()


class _SyntheticCapture:
    def __init__(self, index, fps):
        self.monitor = {"left": 0, "top": 0, "width": 320, "height": 180}
        self.backend = "synthetic"

    def grab(self):
        import av

        frame = av.VideoFrame(320, 180, "yuv420p")
        for plane in frame.planes:
            plane.update(bytes(plane.buffer_size))
        return frame

    def close(self):
        pass


class AudioAndPauseTLSIntegrationTests(unittest.TestCase):
    def test_tls_audio_stream_and_host_input_pause(self):
        monitors = [{"id": 1, "label": "Synthetic", "left": 0, "top": 0, "width": 320, "height": 180}]

        pcm = make_sine_pcm()
        count = [0]

        def pcm_reader(size):
            count[0] += 1
            time.sleep(0.015)
            return pcm

        with tempfile.TemporaryDirectory() as folder:
            states = []
            audio_packets = []
            infos = []

            def fake_audio_stream():
                return AudioCaptureStream(pcm_reader=pcm_reader)

            with (
                patch("brioche.session.platform.system", return_value="Linux"),
                patch("brioche.capture.list_monitors", return_value=monitors),
                patch("brioche.media.DesktopCapture", _SyntheticCapture),
                patch("brioche.platform_input.InputSink") as sink_cls,
                patch("brioche.audio.AudioCaptureStream", side_effect=fake_audio_stream),
            ):
                sink = sink_cls.return_value
                sink.monitor = monitors[0]
                host = Host(
                    port=0,
                    folder=folder,
                    view_only=False,
                    audio_enabled=True,
                    on_session_state=states.append,
                )
                host.start()
                self.assertTrue(host.ready.wait(5))
                uri = access_uri("127.0.0.1", host.port, host.pin, host.token)
                viewer = Viewer(
                    uri,
                    StreamConfig("720p", 30, 6),
                    lambda _f: None,
                    on_info=infos.append,
                    audio_enabled=True,
                    on_audio=audio_packets.append,
                )
                viewer.start()
                try:
                    self.assertTrue(until(lambda: any(i.get("type") == "welcome" for i in infos), 8))
                    welcome = next(i for i in infos if i.get("type") == "welcome")
                    self.assertTrue(welcome.get("audio"))
                    self.assertEqual(welcome.get("audio_codec"), "opus")
                    self.assertTrue(until(lambda: len(audio_packets) >= 2, 5))
                    self.assertTrue(any(s.get("active") for s in states))

                    # Pausing host input releases keys and ignores subsequent viewer input
                    host.set_input_paused(True)
                    sink.release_all.assert_called()
                    sink.apply.reset_mock()
                    viewer.input({"event": "button", "button": "left", "down": True})
                    time.sleep(0.15)
                    sink.apply.assert_not_called()

                    # Resuming host input allows viewer input again
                    host.set_input_paused(False)
                    viewer.input({"event": "button", "button": "left", "down": True})
                    self.assertTrue(until(lambda: sink.apply.called, 3))
                finally:
                    viewer.stop()
                    viewer.join(5)
                    host.stop()
                    host.join(5)
            self.assertTrue(any(not s.get("active") for s in states))


@unittest.skipUnless(
    os.environ.get("BRIOCHE_DESKTOP_TESTS") == "1", "requires isolated Qt desktop"
)
class HostPillQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_host_session_pill_pause_collapse_and_disconnect(self):
        from brioche.remote_overlays import HostSessionPill

        pill = HostSessionPill()
        paused_events = []
        disconnected = []
        pill.pause_input_toggled.connect(paused_events.append)
        pill.disconnect_requested.connect(lambda: disconnected.append(True))
        try:
            pill.show_session(view_only=False, input_paused=False)
            self.assertTrue(pill.isVisible())
            self.assertIn("正在被控制", pill.status_label.text())

            pill.pause_btn.setChecked(True)
            self.assertEqual(paused_events, [True])
            self.assertIn("输入已暂停", pill.status_label.text())

            pill.toggle_collapsed()
            self.assertTrue(pill.pause_btn.isHidden())
            self.assertTrue(pill.disconnect_btn.isHidden())
            pill.toggle_collapsed()
            self.assertFalse(pill.pause_btn.isHidden())

            pill.disconnect_btn.click()
            self.assertEqual(disconnected, [True])
            pill.hide_session()
            self.assertFalse(pill.isVisible())
        finally:
            pill.close()
