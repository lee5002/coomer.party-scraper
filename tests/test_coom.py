import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coomerscraper.coom import (
    main,
    parse_media_urls_from_html,
    parse_post_urls_from_html,
    parse_creator_identity,
    process_post,
    process_page,
    process_prefetched,
)


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

    def test_creator_page_follows_post_links_to_find_videos(self):
        creator_url = 'https://coomerfans.com/u/creator/service'
        post_url = 'https://coomerfans.com/service/user/creator/post/123'
        page_html = (
            '<img src="/storage/preview.jpg">'
            f'<a href="{post_url}">View post</a>'
        )
        post_html = '<video src="/data/video.mp4?hash=signature"></video>'

        with patch(
            'coomerscraper.coom.fetch_html',
            side_effect=[page_html, post_html],
        ):
            with patch('coomerscraper.coom.api_fetch_post_multi', return_value=[]):
                named_urls = process_page(
                    creator_url,
                    skip_img=False,
                    skip_vid=False,
                    offsets=(None, None),
                )

        self.assertEqual(
            [url.name for url in named_urls],
            ['preview.jpg', 'video.mp4'],
        )
        self.assertEqual(
            named_urls[1].url,
            'https://coomerfans.com/data/video.mp4?hash=signature',
        )

    def test_creator_page_api_videos_are_not_hidden_by_preview_images(self):
        creator_url = 'https://coomerfans.com/u/creator/service'
        post = {
            'title': 'A video',
            'published': '2026-01-02',
            'file': {'path': 'video.mp4'},
            'attachments': [],
        }

        with patch(
            'coomerscraper.coom.fetch_html',
            return_value='<img src="/storage/preview.jpg">',
        ):
            with patch(
                'coomerscraper.coom.api_fetch_post_multi',
                return_value=[post],
            ) as fetch_posts:
                named_urls = process_page(
                    creator_url,
                    skip_img=False,
                    skip_vid=False,
                    offsets=(None, None),
                )

        fetch_posts.assert_called_once_with(
            'https://coomerfans.com', 'service', 'creator', 0
        )
        self.assertEqual(
            [url.name for url in named_urls],
            ['preview.jpg', '20260102-AVideo_0.mp4'],
        )

    def test_post_api_videos_are_not_hidden_by_preview_images(self):
        post_url = 'https://coomerfans.com/service/user/creator/post/123'
        post = {
            'title': 'A video',
            'published': '2026-01-02',
            'file': {'path': 'video.mp4'},
            'attachments': [],
        }

        with patch(
            'coomerscraper.coom.fetch_html',
            return_value='<img src="/storage/preview.jpg">',
        ):
            with patch(
                'coomerscraper.coom.api_fetch_post_single',
                return_value={'post': post},
            ):
                named_urls = process_post(
                    post_url,
                    skip_img=False,
                    skip_vid=False,
                )

        self.assertEqual(
            [url.name for url in named_urls],
            ['preview.jpg', '20260102-AVideo_0.mp4'],
        )

    def test_creator_page_video_is_submitted_to_download_queue(self):
        creator_url = 'https://coomerfans.com/u/creator/service'
        post_url = 'https://coomerfans.com/service/user/creator/post/123'
        page_html = f'<a href="{post_url}">View post</a>'
        post_html = '<video src="/data/video.mp4?hash=signature"></video>'

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                'coomerscraper.coom.fetch_html',
                side_effect=[page_html, post_html],
            ):
                with patch('coomerscraper.coom.api_fetch_post_multi', return_value=[]):
                    with patch('coomerscraper.coom.get_creator_name', return_value='creator_service'):
                        with patch('coomerscraper.coom.multithread_download') as download:
                            main(
                                [creator_url],
                                Path(temp_dir),
                                skip_img=False,
                                skip_vid=False,
                                offsets=(None, None),
                                dump_urls=False,
                                jobs=1,
                                retries=1,
                            )

        queued_urls = download.call_args.args[0]
        self.assertEqual(len(queued_urls), 1)
        self.assertEqual(
            queued_urls[0].url,
            'https://coomerfans.com/data/video.mp4?hash=signature',
        )

    def test_post_urls_support_both_creator_page_route_formats(self):
        creator_url = 'https://coomerfans.com/u/creator/service'
        html = (
            f'<a href="{creator_url}/123">Post</a>'
            '<a href="https://coomerfans.com/service/user/creator/post/456">'
            'Another post</a>'
        )

        post_urls = parse_post_urls_from_html(creator_url, html)

        self.assertEqual(
            post_urls,
            [
                f'{creator_url}/123',
                'https://coomerfans.com/service/user/creator/post/456',
            ],
        )

    def test_creator_url_parses_service_and_creator_for_api(self):
        self.assertEqual(
            parse_creator_identity('https://coomerfans.com/u/creator/service'),
            ('service', None, 'creator'),
        )


if __name__ == '__main__':
    unittest.main()
