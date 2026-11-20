import unittest
from unittest.mock import patch
import av
from brioche.media import DesktopEncoder
from brioche.protocol import StreamConfig
from brioche.media import create_encoder
from unittest.mock import Mock


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
    def test_mac_hardware_without_first_packet_closes_and_falls_back(self):
        hardware, software = Mock(), Mock()
        hardware.frame.return_value = ([], 1.0)
        with patch('brioche.media.sys.platform', 'darwin'), patch('brioche.media.DesktopEncoder', side_effect=[hardware, software]):
            self.assertIs(create_encoder(StreamConfig('720p',30,4), 'auto'), software)
            hardware.close.assert_called_once()
            self.assertIn('VideoToolbox', software.fallback_reason)

    def test_mac_hardware_first_packet_is_kept_for_viewer(self):
        hardware = Mock()
        hardware.frame.return_value = ([b'first-idr'], 1.0)
        with patch('brioche.media.sys.platform', 'darwin'), patch('brioche.media.DesktopEncoder', return_value=hardware):
            self.assertIs(create_encoder(StreamConfig('720p',30,4), 'auto'), hardware)
            self.assertEqual(hardware._primed, ([b'first-idr'], 1.0))
            hardware.close.assert_not_called()

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
