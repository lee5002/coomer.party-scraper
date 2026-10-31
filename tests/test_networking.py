import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coomerscraper.networking import NamedUrl, _download


class DownloadTests(unittest.TestCase):
    def test_video_download_accepts_media_and_allows_slow_streams(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / 'video.mp4'
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {'Content-Length': '5'}
            response.iter_content.return_value = [b'video']

            with patch('coomerscraper.networking.requests.get', return_value=response) as get:
                _download(NamedUrl('https://coomerfans.com/storage/video.mp4', 'video.mp4'),
                          destination, 0, queue.Queue(), 1)

            self.assertEqual(destination.read_bytes(), b'video')
            self.assertEqual(get.call_args.kwargs['headers']['Accept'], '*/*')
            self.assertEqual(get.call_args.kwargs['timeout'], (10, 60))


if __name__ == '__main__':
    unittest.main()
