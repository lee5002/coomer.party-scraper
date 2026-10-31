import hashlib
import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coomerscraper.networking import (
    NamedUrl,
    _download,
    media_extension,
    multithread_download,
)


class DownloadTests(unittest.TestCase):
    def test_media_extension_ignores_signed_query_string(self):
        self.assertEqual(
            media_extension('https://img10.coomerfans.com/storage/video.MP4?e=1&hash=signature'),
            'mp4',
        )

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

    def test_download_replaces_old_duplicate_with_current_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            existing = root / 'existing.mp4'
            existing.write_bytes(b'identical media')
            duplicate = root / 'new.mp4'
            hashes = {
                hashlib.sha256(b'identical media').hexdigest(): existing,
            }

            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {'Content-Length': str(len(b'identical media'))}
            response.iter_content.return_value = [b'identical media']

            with patch('coomerscraper.networking.tqdm'):
                with patch(
                    'coomerscraper.networking.requests.get',
                    return_value=response,
                ):
                    multithread_download(
                        [NamedUrl('https://coomerfans.com/storage/new.mp4', 'new.mp4')],
                        root,
                        root,
                        hashes=hashes,
                        workers=1,
                        retries=1,
                    )

            self.assertFalse(existing.exists())
            self.assertEqual(duplicate.read_bytes(), b'identical media')
            self.assertEqual(
                hashes[hashlib.sha256(b'identical media').hexdigest()],
                duplicate,
            )

    def test_downloads_in_same_batch_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = NamedUrl('https://coomerfans.com/storage/first.mp4', 'first.mp4')
            second = NamedUrl('https://coomerfans.com/storage/second.mp4', 'second.mp4')
            response = unittest.mock.MagicMock()
            response.__enter__.return_value = response
            response.headers = {'Content-Length': '5'}
            response.iter_content.return_value = [b'same!']

            with patch('coomerscraper.networking.tqdm'):
                with patch(
                    'coomerscraper.networking.requests.get',
                    return_value=response,
                ):
                    multithread_download(
                        [first, second],
                        root,
                        root,
                        hashes={},
                        workers=1,
                        retries=1,
                    )

            self.assertEqual(
                sorted(path.name for path in root.glob('*.mp4')),
                ['second.mp4'],
            )


if __name__ == '__main__':
    unittest.main()
