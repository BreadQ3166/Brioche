import gc
import unittest

import av

from brioche.ui import FrameMailbox


class FrameMailboxTests(unittest.TestCase):
    def test_native_and_legacy_frames_preserve_color_and_own_pixels(self):
        for pixel_format, red in (("bgra", b"\x00\x00\xff\xff"), ("rgb24", b"\xff\x00\x00")):
            with self.subTest(pixel_format=pixel_format):
                frame = av.VideoFrame(2, 2, pixel_format)
                plane = frame.planes[0]
                pixels = bytearray(plane.buffer_size)
                for row in range(2):
                    pixels[row * plane.line_size:row * plane.line_size + len(red) * 2] = red * 2
                plane.update(pixels)
                mailbox = FrameMailbox()
                mailbox.publish(frame)
                # A subsequent decoder write cannot mutate the displayed frame.
                plane.update(bytes(plane.buffer_size))
                del frame, plane
                gc.collect()
                image = mailbox.take()
                self.assertEqual(image.pixelColor(1, 1).getRgb(), (255, 0, 0, 255))
                self.assertIsNone(mailbox.take())

    def test_latest_frame_replaces_stale_frame(self):
        mailbox = FrameMailbox()
        mailbox.publish(av.VideoFrame(2, 2, 'bgra'))
        mailbox.publish(av.VideoFrame(4, 4, 'bgra'))
        image = mailbox.take()
        self.assertEqual((image.width(), image.height()), (4, 4))
