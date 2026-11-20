from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest

from brioche.display_mode import DisplayLease, choose_mode, parse_randr
from brioche.protocol import StreamConfig, ProtocolError

QUERY = """Screen 0: minimum 8 x 8, current 6000 x 3840, maximum 32767 x 32767
HDMI-0 connected 2160x3840+0+0 left (normal left inverted right x axis y axis)
   3840x2160  60.00+ 119.88*
   2560x1440  120.00
   1920x1080  60.00
DP-2 connected primary 3840x2160+2160+772 (normal left inverted right x axis y axis)
   3840x2160  60.00+ 160.00*
   2560x1440  144.00 120.00
   1920x1080  119.88 60.00
DP-3 disconnected (normal left inverted right x axis y axis)
"""


class DisplayConfigTests(unittest.TestCase):
    def test_default_wire_remains_compatible_and_matching_is_explicit(self):
        wire = StreamConfig().to_wire()
        self.assertEqual(set(wire), {"resolution", "fps", "bitrate_mbps"})
        self.assertFalse(StreamConfig.parse(wire).match_display)
        self.assertTrue(
            StreamConfig.parse(StreamConfig(match_display=True).to_wire()).match_display
        )
        self.assertEqual(StreamConfig("native").resolution, "native")
        with self.assertRaises(ProtocolError):
            StreamConfig.parse({**wire, "match_display": 1})

    def test_select_landscape_and_portrait_without_stretch_or_upscale(self):
        state = parse_randr(QUERY)
        portrait, landscape = state["outputs"]
        self.assertEqual((portrait["mode"], portrait["rate"]), ("3840x2160", 119.88))
        chosen = choose_mode(portrait, "1440p", 90)
        self.assertEqual(
            (chosen["width"], chosen["height"], chosen["rotation"]),
            (1440, 2560, "left"),
        )
        chosen = choose_mode(landscape, "1440p", 60)
        self.assertEqual(
            (chosen["width"], chosen["height"], chosen["rate"]), (2560, 1440, 144)
        )
        self.assertIsNone(choose_mode(chosen, "1440p", 60))
        self.assertIsNone(choose_mode(landscape, "native", 60))


class FakeRandR:
    def __init__(self):
        self.state = parse_randr(QUERY)
        self.calls = []
        self.fail_next = False

    def snapshot(self):
        return deepcopy(self.state)

    def apply(self, output, framebuffer):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("modeset rejected")
        self.calls.append((deepcopy(output), list(framebuffer)))
        self.state["framebuffer"] = list(framebuffer)
        for index, item in enumerate(self.state["outputs"]):
            if item["name"] == output["name"]:
                self.state["outputs"][index] = deepcopy(output)
                return
        raise RuntimeError("unknown output")


@unittest.skipUnless(
    sys.platform.startswith("linux"), "Xorg mode leases require Linux flock"
)
class DisplayLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.backend = FakeRandR()
        self.original = self.backend.snapshot()
        self.lease = DisplayLease(self.temp.name, self.backend)

    def tearDown(self):
        self.lease.close()
        self.temp.cleanup()

    def test_modes_restore_after_multiscreen_session_and_keep_layout(self):
        for monitor in self.original["outputs"]:
            self.lease.apply(monitor, "1440p", 90)
        self.assertEqual(self.backend.state["outputs"][1]["width"], 2560)
        self.assertTrue(self.lease.path.exists())
        self.lease.close()
        self.assertEqual(self.backend.snapshot(), self.original)
        self.assertFalse(self.lease.path.exists())
        self.assertTrue(
            all(fb == self.original["framebuffer"] for _, fb in self.backend.calls)
        )

    def test_crash_journal_restores_on_next_lease(self):
        self.lease.apply(self.original["outputs"][1], "1440p", 60)
        self.lease.lock.close()  # Emulate OS releasing the lock after process termination.
        recovered = DisplayLease(self.temp.name, self.backend)
        recovered.close()
        self.assertEqual(self.backend.snapshot(), self.original)
        self.assertFalse(Path(self.temp.name, "display-restore.json").exists())

    def test_manual_mode_or_layout_change_is_not_overwritten(self):
        self.lease.apply(self.original["outputs"][1], "1440p", 60)
        self.backend.state["outputs"][1]["left"] += 10
        manual = self.backend.snapshot()
        self.lease.close()
        self.assertEqual(self.backend.snapshot(), manual)

    def test_failed_apply_rolls_back_and_does_not_leave_journal(self):
        self.backend.fail_next = True
        with self.assertRaises(RuntimeError):
            self.lease.apply(self.original["outputs"][1], "1440p", 60)
        self.assertEqual(self.backend.snapshot(), self.original)
        self.assertFalse(self.lease.path.exists())

    def test_concurrent_leases_cannot_change_the_same_display(self):
        with self.assertRaises(RuntimeError):
            DisplayLease(self.temp.name, self.backend)

    def test_unknown_monitor_does_not_change_any_output(self):
        with self.assertRaises(RuntimeError):
            self.lease.apply(
                {"left": 99, "top": 99, "width": 10, "height": 10}, "1440p", 60
            )
        self.assertEqual(self.backend.calls, [])
