import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from coomerscraper.coom import (
    HtmlFetchError,
    fetch_html,
    main,
    parse_creator_identity,
    parse_media_urls_from_html,
    parse_post_body_media_urls_from_html,
    parse_post_urls_from_html,
    process_page,
    process_post,
    process_prefetched,
    purge_duplicate_urls,
)
from coomerscraper.networking import NamedUrl
from coomerscraper.utils import base_url, sanitize_url


class ParseMediaUrlsTests(unittest.TestCase):
    def test_fetch_html_retries_service_unavailable_after_retry_after(self):
        unavailable = requests.Response()
        unavailable.status_code = 503
        unavailable.url = 'https://coomerfans.com/test'
        unavailable.headers['Retry-After'] = '3'
        available = requests.Response()
        available.status_code = 200
        available.url = 'https://coomerfans.com/test'
        available._content = b'<html>available</html>'

        with patch(
            'coomerscraper.coom.requests.get',
            side_effect=[unavailable, available],
        ) as get:
            with patch('coomerscraper.coom.time.sleep') as sleep:
                html = fetch_html('https://coomerfans.com/test')

        self.assertEqual(html, '<html>available</html>')
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once_with(3.0)

    def test_fetch_html_stops_after_bounded_service_unavailable_retries(self):
        unavailable = requests.Response()
        unavailable.status_code = 503
        unavailable.url = 'https://coomerfans.com/test'

        with patch(
            'coomerscraper.coom.requests.get',
            return_value=unavailable,
        ) as get:
            with patch('coomerscraper.coom.time.sleep') as sleep:
                with self.assertRaises(HtmlFetchError):
                    fetch_html('https://coomerfans.com/test')

        self.assertEqual(get.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [2, 4, 8],
        )

    def test_fetch_html_uses_configured_retry_count(self):
        unavailable = requests.Response()
        unavailable.status_code = 503
        unavailable.url = 'https://coomerfans.com/test'
        unavailable.headers['Retry-After'] = '0'

        with patch(
            'coomerscraper.coom.requests.get',
            return_value=unavailable,
        ) as get:
            with patch('coomerscraper.coom.time.sleep'):
                with self.assertRaises(HtmlFetchError):
                    fetch_html('https://coomerfans.com/test', retries=2)

        self.assertEqual(get.call_count, 3)

    def test_creator_scan_stops_after_exhausted_transient_post_error(self):
        creator_url = 'https://coomerfans.com/u/fansly/289067/creator'
        first_post_url = 'https://coomerfans.com/p/123/289067/fansly'
        second_post_url = 'https://coomerfans.com/p/456/289067/fansly'
        page_html = (
            f'<a href="{first_post_url}">First</a>'
            f'<a href="{second_post_url}">Second</a>'
        )
        first_post_html = (
            '<div class="post-body"><img src="/storage/first.jpg"></div>'
        )

        with patch(
            'coomerscraper.coom.fetch_html',
            side_effect=[
                page_html,
                first_post_html,
                HtmlFetchError('503 Server Error'),
            ],
        ) as fetch:
            named_urls = process_page(
                creator_url,
                skip_img=False,
                skip_vid=False,
                offsets=(None, None),
                retries=16,
            )

        self.assertEqual([url.name for url in named_urls], ['first.jpg'])
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(
            all(call.kwargs['retries'] == 16 for call in fetch.call_args_list)
        )

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
        creator_url = 'https://coomerfans.com/u/fansly/289067/creator'
        post_url = 'https://coomerfans.com/p/123/289067/fansly'
        page_html = (
            '<img src="/storage/preview.jpg">'
            f'<a href="{post_url}">View post</a>'
        )
        post_html = (
            '<div class="post-body">'
            '<video src="/data/video.mp4?hash=signature"></video>'
            '</div>'
        )

        with patch(
            'coomerscraper.coom.fetch_html',
            side_effect=[page_html, post_html],
        ):
            named_urls = process_page(
                creator_url,
                skip_img=False,
                skip_vid=False,
                offsets=(None, None),
            )

        self.assertEqual([url.name for url in named_urls], ['video.mp4'])
        self.assertEqual(
            named_urls[0].url,
            'https://coomerfans.com/data/video.mp4?hash=signature',
        )

    def test_creator_page_queues_post_body_originals_not_creator_thumbnails(self):
        creator_url = 'https://coomerfans.com/u/fansly/289067/creator'
        post_url = 'https://coomerfans.com/p/123/289067/fansly'
        page_html = (
            '<img src="/istorage/289067.jpg">'
            '<img src="/storage/creator-preview.jpg">'
            f'<a href="{post_url}">Post</a>'
        )
        post_html = (
            '<div class="post-body">'
            '<img src="/storage/image-preview.jpg" '
            'srcset="/storage/image-preview.jpg 1x, '
            '/storage/image-original.jpg 2x">'
            '<video src="/data/video.mp4"></video>'
            '</div>'
            '<img src="/storage/recommended-post-preview.jpg">'
        )

        with patch(
            'coomerscraper.coom.fetch_html',
            side_effect=[page_html, post_html],
        ):
            named_urls = process_page(
                creator_url,
                skip_img=False,
                skip_vid=False,
                offsets=(None, None),
            )

        self.assertEqual(
            [url.url for url in named_urls],
            [
                'https://coomerfans.com/storage/image-original.jpg',
                'https://coomerfans.com/data/video.mp4',
            ],
        )

    def test_creator_page_follows_coomerfans_pagination(self):
        creator_url = 'https://coomerfans.com/u/fansly/289067/creator'
        page_one = (
            '<a href="/p/123/289067/fansly">Post one</a>'
            '<a href="?page=2">2</a>'
        )
        page_two = (
            '<a href="/p/456/289067/fansly">Post two</a>'
            '<a href="?page=1">1</a>'
        )
        post_one = (
            '<div class="post-body">'
            '<img src="/storage/first.jpg">'
            '</div>'
        )
        post_two = (
            '<div class="post-body">'
            '<img src="/storage/second.jpg">'
            '</div>'
        )

        with patch(
            'coomerscraper.coom.fetch_html',
            side_effect=[page_one, page_two, post_one, post_two],
        ):
            named_urls = process_page(
                creator_url,
                skip_img=False,
                skip_vid=False,
                offsets=(None, None),
            )

        self.assertEqual(
            [url.url for url in named_urls],
            [
                'https://coomerfans.com/storage/first.jpg',
                'https://coomerfans.com/storage/second.jpg',
            ],
        )

    def test_post_download_uses_only_media_in_post_body(self):
        post_url = 'https://coomerfans.com/p/123/289067/fansly'
        html = (
            '<img src="/storage/profile-preview.jpg">'
            '<div class="post-body">'
            '<a href="/storage/image-original.jpg">'
            '<img src="/storage/image-preview.jpg">'
            '</a>'
            '<video src="/data/video.mp4"></video>'
            '</div>'
        )

        with patch('coomerscraper.coom.fetch_html', return_value=html):
            named_urls = process_post(
                post_url,
                skip_img=False,
                skip_vid=False,
            )

        self.assertEqual(
            [named_url.url for named_url in named_urls],
            [
                'https://coomerfans.com/storage/image-original.jpg',
                'https://coomerfans.com/data/video.mp4',
            ],
        )

    def test_creator_page_video_is_submitted_to_download_queue(self):
        creator_url = 'https://coomerfans.com/u/fansly/289067/creator'
        post_url = 'https://coomerfans.com/p/123/289067/fansly'
        page_html = f'<a href="{post_url}">View post</a>'
        post_html = (
            '<div class="post-body">'
            '<video src="/data/video.mp4?hash=signature"></video>'
            '</div>'
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch(
                'coomerscraper.coom.fetch_html',
                side_effect=[page_html, post_html],
            ):
                with patch(
                    'coomerscraper.coom.get_creator_name',
                    return_value='creator_service',
                ):
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

    def test_creator_folder_uses_creator_name_instead_of_numeric_id(self):
        creator_url = 'https://coomerfans.com/u/onlyfans/319490/aellagirl'

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('coomerscraper.coom.process_page', return_value=[]):
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

            self.assertTrue(
                (Path(temp_dir) / 'aellagirl_onlyfans' / 'pics').is_dir()
            )

    def test_post_folder_uses_creator_name_from_post_page(self):
        post_url = 'https://coomerfans.com/p/123/319490/onlyfans'
        post_html = (
            '<a href="/u/onlyfans/319490/aellagirl">aellagirl</a>'
            '<div class="post-body"><img src="/storage/image.jpg"></div>'
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('coomerscraper.coom.fetch_html', return_value=post_html):
                with patch('coomerscraper.coom.multithread_download'):
                    main(
                        [post_url],
                        Path(temp_dir),
                        skip_img=False,
                        skip_vid=False,
                        offsets=(None, None),
                        dump_urls=False,
                        jobs=1,
                        retries=1,
                    )

            self.assertTrue(
                (Path(temp_dir) / 'aellagirl_onlyfans' / 'pics').is_dir()
            )

    def test_non_coomerfans_urls_are_not_processed(self):
        with patch('coomerscraper.coom.fetch_html') as fetch:
            with patch('coomerscraper.coom.multithread_download') as download:
                main(
                    ['https://example.com/u/fansly/creator'],
                    Path('.'),
                    skip_img=False,
                    skip_vid=False,
                    offsets=(None, None),
                    dump_urls=False,
                    jobs=1,
                    retries=1,
                )

        fetch.assert_not_called()
        download.assert_not_called()

    def test_coomerfans_media_subdomains_are_supported_and_preserved(self):
        cdn_url = (
            'https://img5.coomerfans.com/storage/'
            '2892ac-0199dab3-dc8c-7616-baa0-0d496f866afa.jpg'
        )

        self.assertEqual(base_url(cdn_url), 'https://coomerfans.com')
        self.assertEqual(sanitize_url(cdn_url), cdn_url)

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch('coomerscraper.coom.multithread_download') as download:
                main(
                    [cdn_url],
                    Path(temp_dir),
                    skip_img=False,
                    skip_vid=False,
                    offsets=(None, None),
                    dump_urls=False,
                    jobs=1,
                    retries=1,
                )

        self.assertEqual(download.call_args.args[0][0].url, cdn_url)

    def test_post_urls_support_creatorfans_post_route_formats(self):
        creator_url = 'https://coomerfans.com/u/fansly/289067/creator'
        html = (
            f'<a href="{creator_url}/123">Post</a>'
            '<a href="https://coomerfans.com/p/456/289067/fansly">Another post</a>'
            '<a href="https://coomerfans.com/p/789/other-creator/fansly">'
            'Different creator</a>'
        )

        post_urls = parse_post_urls_from_html(creator_url, html)

        self.assertEqual(
            post_urls,
            [
                f'{creator_url}/123',
                'https://coomerfans.com/p/456/289067/fansly',
            ],
        )

    def test_creator_url_parses_service_and_creator(self):
        self.assertEqual(
            parse_creator_identity('https://coomerfans.com/u/fansly/289067/creator'),
            ('fansly', '289067', 'creator'),
        )
        self.assertEqual(
            parse_creator_identity('https://coomerfans.com/p/91822068/289067/fansly'),
            ('fansly', '289067', '289067'),
        )

    def test_post_body_parser_prefers_largest_image_and_excludes_thumbnails(self):
        html = (
            '<img src="/istorage/avatar.jpg">'
            '<img src="/storage/creator-preview.jpg">'
            '<div class="post-body">'
            '<img src="/storage/small.jpg" '
            'srcset="/storage/small.jpg 1x, /storage/large.jpg 2x">'
            '<a href="/storage/original.jpg">'
            '<img src="/storage/linked-preview.jpg">'
            '</a>'
            '<picture>'
            '<source srcset="/storage/picture-large.jpg 1200w">'
            '<img src="/storage/picture-small.jpg">'
            '</picture>'
            '<img src="https://example.com/image.jpg">'
            '</div>'
        )

        named_urls = parse_post_body_media_urls_from_html(
            'https://coomerfans.com/p/123/289067/fansly',
            html,
        )

        self.assertEqual(
            [named_url.url for named_url in named_urls],
            [
                'https://coomerfans.com/storage/large.jpg',
                'https://coomerfans.com/storage/original.jpg',
                'https://coomerfans.com/storage/picture-large.jpg',
            ],
        )

    def test_existing_coomerfans_filename_is_removed_from_queue(self):
        media_url = (
            'https://img5.coomerfans.com/storage/8/ns/jy/'
            '2892ac-0199dab3-dc8c-7616-baa0-0d496f866afa.jpg'
        )
        named_url = NamedUrl(
            media_url,
            '2892ac-0199dab3-dc8c-7616-baa0-0d496f866afa.jpg',
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            existing_file = (
                Path(temp_dir)
                / 'pics'
                / '2892ac-0199dab3-dc8c-7616-baa0-0d496f866afa.jpg'
            )
            existing_file.parent.mkdir()
            existing_file.write_bytes(b'already downloaded')

            remaining = purge_duplicate_urls(Path(temp_dir), [named_url])

        self.assertEqual(remaining, [])

    def test_existing_content_hash_is_removed_from_queue(self):
        content = b'already downloaded'
        digest = hashlib.sha256(content).hexdigest()
        named_url = NamedUrl(
            f'https://coomerfans.com/storage/{digest}.jpg',
            f'{digest}.jpg',
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, 'existing.jpg').write_bytes(content)
            remaining = purge_duplicate_urls(Path(temp_dir), [named_url])

        self.assertEqual(remaining, [])

    def test_post_route_extracts_post_body_from_coomerfans(self):
        with patch(
            'coomerscraper.coom.fetch_html',
            return_value=(
                '<div class="post-body">'
                '<video src="/data/video.mp4"></video>'
                '</div>'
            ),
        ):
            urls = process_post(
                'https://coomerfans.com/p/91822068/289067/fansly',
                skip_img=False,
                skip_vid=False,
            )

        self.assertEqual(
            [url.url for url in urls],
            ['https://coomerfans.com/data/video.mp4'],
        )


if __name__ == '__main__':
    unittest.main()
