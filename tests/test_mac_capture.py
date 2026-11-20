import ctypes
import unittest
from unittest.mock import Mock, patch
from mss.screenshot import ScreenShot
from brioche.mac_capture import grab
from brioche.mac_permissions import require_host


class MacCaptureTests(unittest.TestCase):
    def core(self):
        # Two Retina rows, each with eight bytes of row padding.
        self.buffer = ctypes.create_string_buffer(b'\x11' * 8 + b'\xff' * 8 + b'\x22' * 8 + b'\xff' * 8)
        return Mock(**{
            'CGWindowListCreateImage.return_value': 1,
            'CGImageGetWidth.return_value': 2, 'CGImageGetHeight.return_value': 2,
            'CGImageGetBytesPerRow.return_value': 16,
            'CGImageGetBitsPerPixel.return_value': 32,
            'CGImageGetDataProvider.return_value': 2,
            'CGDataProviderCopyData.return_value': 3,
            'CFDataGetLength.return_value': 32,
            'CFDataGetBytePtr.return_value': ctypes.addressof(self.buffer),
        })

    def test_repeated_retina_frames_release_owned_objects_and_strip_padding(self):
        core = self.core()
        monitor = {'left': 0, 'top': 0, 'width': 1, 'height': 1}
        for _ in range(1000):
            shot = grab(core, monitor, None, ScreenShot)
            self.assertEqual(shot.bgra, b'\x11' * 8 + b'\x22' * 8)
            self.assertEqual((shot.width, shot.height), (2, 2))
        self.assertEqual(core.CGImageRelease.call_count, 1000)
        self.assertEqual(core.CFRelease.call_count, 1000)
        core.CFRelease.assert_called_with(3)
        core.CGDataProviderRelease.assert_not_called()

    def test_failed_pixel_copy_still_releases_image_and_data(self):
        core = self.core()
        core.CFDataGetLength.return_value = 2
        with self.assertRaisesRegex(RuntimeError, '不完整'):
            grab(core, {}, None, ScreenShot)
        core.CFRelease.assert_called_once_with(3)
        core.CGImageRelease.assert_called_once_with(1)
        core.CGDataProviderRelease.assert_not_called()

    def test_permission_failure_before_capture_and_readonly_needs_no_input(self):
        with patch('brioche.mac_permissions.require_screen', side_effect=RuntimeError('屏幕录制')), patch('brioche.mac_permissions.require_input') as inputs:
            with self.assertRaisesRegex(RuntimeError, '屏幕录制'):
                require_host()
            inputs.assert_not_called()
        with patch('brioche.mac_permissions.require_screen'), patch('brioche.mac_permissions.require_input') as inputs:
            require_host(view_only=True)
            inputs.assert_not_called()
