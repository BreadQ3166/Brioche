import unittest
from unittest.mock import patch
import av
from brioche.media import DesktopEncoder
from brioche.protocol import StreamConfig


class RetinaCapture:
    monitor={'left':0,'top':0,'width':1280,'height':800}
    backend='retina-fixture'
    def __init__(self,*args):self.closed=False
    def grab(self):
        frame=av.VideoFrame(2560,1600,'bgra')
        for plane in frame.planes:plane.update(bytes(plane.buffer_size))
        return frame
    def close(self):self.closed=True


class RetinaCoordinatesTests(unittest.TestCase):
    def test_video_pixels_are_not_confused_with_input_points(self):
        with patch('brioche.media.DesktopCapture',RetinaCapture),patch('brioche.media.sys.platform','darwin'):
            encoder=DesktopEncoder(StreamConfig('1440p',30,8))
            try:
                self.assertEqual((encoder.width,encoder.height),(2304,1440))
                self.assertEqual((encoder.capture_width,encoder.capture_height),(2560,1600))
                self.assertEqual((encoder.monitor['width'],encoder.monitor['height']),(1280,800))
                packets,_=encoder.frame()
                decoder=av.CodecContext.create('h264','r')
                frames=[frame for packet in packets for frame in decoder.decode(av.Packet(packet))]
                self.assertEqual((frames[0].width,frames[0].height),(2304,1440))
            finally:encoder.close()
