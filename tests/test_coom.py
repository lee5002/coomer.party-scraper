import unittest
from unittest.mock import patch

from coomerscraper.coom import parse_media_urls_from_html, process_prefetched


class ParseMediaUrlsTests(unittest.TestCase):
    def test_prefetched_video_keeps_signed_query_string(self):
        media_url = 'https://coomerfans.com/data/video.mp4?e=1&hash=signature'

        named_urls = process_prefetched(media_url, skip_img=False, skip_vid=False)

        self.assertEqual(len(named_urls), 1)
        self.assertEqual(named_urls[0].url, media_url)
        self.assertEqual(named_urls[0].name, 'video.mp4')

    def test_video_url_under_data_path_is_parsed(self):
        media_url = (
            'https://coomerfans.com/data/10/bu/fq/'
            'video.mp4?e=1793486736&hash=signature'
        )
        response = unittest.mock.MagicMock()
        response.text = f'<video src="{media_url.replace("&", "&amp;")}"></video>'

        with patch('coomerscraper.coom.requests.get', return_value=response):
            named_urls = parse_media_urls_from_html(
                'https://coomerfans.com/u/creator/service/123'
            )

        self.assertEqual(len(named_urls), 1)
        self.assertEqual(named_urls[0].url, media_url)
        self.assertEqual(named_urls[0].name, 'video.mp4')

    def test_video_url_keeps_signed_query_string(self):
        media_url = (
            'https://img10.coomerfans.com/storage/10/bu/fq/'
            '2892ac-0199dde8-7128-7319-aa86-d2b677d072c3.mp4'
            '?e=1793486736&hash=LNJvYwqZ6UUHSjNEc2CA8w'
        )
        response = unittest.mock.MagicMock()
        response.text = f'<video src="{media_url.replace("&", "&amp;")}"></video>'

        with patch('coomerscraper.coom.requests.get', return_value=response):
            named_urls = parse_media_urls_from_html(
                'https://coomerfans.com/u/creator/service/123'
            )

        self.assertEqual(len(named_urls), 1)
        self.assertEqual(named_urls[0].url, media_url)
        self.assertEqual(
            named_urls[0].name,
            '2892ac-0199dde8-7128-7319-aa86-d2b677d072c3.mp4',
        )


if __name__ == '__main__':
    unittest.main()
