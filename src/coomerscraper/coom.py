import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urljoin, urlparse

import requests

from .networking import (
    multithread_download,
    NamedUrl,
    IMG_EXTS,
    VID_EXTS,
    media_extension,
)
from .utils import base_url, compute_file_hash_map, create_folder_tree


logger = logging.getLogger(__name__)

HTML_DEFAULT_RETRIES = 3
HTML_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class HtmlFetchError(Exception):
    pass


def _html_retry_delay(response, attempt: int) -> float:
    retry_after = response.headers.get('Retry-After')
    if retry_after:
        try:
            return max(0, float(retry_after))
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=timezone.utc)
                return max(
                    0,
                    (retry_time - datetime.now(timezone.utc)).total_seconds(),
                )
            except (TypeError, ValueError, OverflowError):
                pass
    return min(30, 2 ** (attempt + 1))


def fetch_html(url: str, retries: Optional[int] = None) -> Optional[str]:
    max_retries = HTML_DEFAULT_RETRIES if retries is None else max(0, retries)
    for attempt in range(max_retries + 1):
        try:
            res = requests.get(
                url,
                headers={'User-Agent': 'Mozilla/5.0'},
                timeout=20,
            )
            if res.status_code in HTML_RETRYABLE_STATUS_CODES:
                error = requests.HTTPError(
                    f'{res.status_code} response while fetching {url}',
                    response=res,
                )
                if attempt == max_retries:
                    logger.error(
                        f'Failed to fetch HTML for {url} after '
                        f'{max_retries + 1} attempts: {error}'
                    )
                    raise HtmlFetchError(str(error)) from error

                delay = _html_retry_delay(res, attempt)
                logger.warning(
                    f'Failed to fetch HTML for {url}: {error}. '
                    f'Retrying in {delay:g} seconds '
                    f'({attempt + 1}/{max_retries})'
                )
                time.sleep(delay)
                continue
            res.raise_for_status()
            return res.text
        except requests.HTTPError as exc:
            logger.warning(f'Failed to fetch HTML for {url}: {exc}')
            return None
        except requests.RequestException as exc:
            if attempt == max_retries:
                logger.error(
                    f'Failed to fetch HTML for {url} after '
                    f'{max_retries + 1} attempts: {exc}'
                )
                raise HtmlFetchError(str(exc)) from exc

            delay = min(30, 2 ** (attempt + 1))
            logger.warning(
                f'Failed to fetch HTML for {url}: {exc}. '
                f'Retrying in {delay:g} seconds '
                f'({attempt + 1}/{max_retries})'
            )
            time.sleep(delay)

    return None


def parse_media_urls_from_html(url: str, html: Optional[str] = None) -> List[NamedUrl]:
    if html is None:
        try:
            html = fetch_html(url)
        except HtmlFetchError:
            return []
    if html is None:
        return []

    seen = set()
    named_urls = []
    for candidate in re.findall(r'(?:src|href|data-src|poster)=["\']([^"\']+)["\']', html, flags=re.I):
        media_url = _coomerfans_media_url(url, candidate)
        if not media_url or media_url in seen:
            continue
        seen.add(media_url)
        named_urls.append(
            NamedUrl(media_url, Path(urlparse(media_url).path).name)
        )
    return named_urls


def parse_video_urls_from_html(url: str, html: Optional[str] = None) -> List[NamedUrl]:
    return [
        named_url
        for named_url in parse_media_urls_from_html(url, html)
        if media_extension(named_url.url) in VID_EXTS
    ]


def _coomerfans_media_url(page_url: str, candidate: Optional[str]) -> Optional[str]:
    if not candidate:
        return None

    candidate = unescape(candidate.strip())
    media_url = urljoin('https://coomerfans.com/', candidate)
    parsed = urlparse(media_url)
    host = (parsed.hostname or '').lower()
    if host != 'coomerfans.com' and not host.endswith('.coomerfans.com'):
        return None
    if not parsed.path.startswith(('/storage/', '/data/')):
        return None
    if media_extension(media_url) not in IMG_EXTS + VID_EXTS:
        return None
    return media_url


def _largest_srcset_candidate(srcset: Optional[str]) -> Optional[str]:
    if not srcset:
        return None

    candidates = []
    for source in srcset.split(','):
        parts = source.strip().split()
        if not parts:
            continue
        descriptor = parts[1] if len(parts) > 1 else '1x'
        match = re.match(r'(\d+(?:\.\d+)?)(?:w|x)$', descriptor)
        size = float(match.group(1)) if match else 1.0
        candidates.append((size, parts[0]))
    return max(candidates, default=(0, None))[1]


class _PostBodyMediaParser(HTMLParser):
    _void_tags = {
        'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
        'link', 'meta', 'param', 'source', 'track', 'wbr',
    }

    def __init__(self, page_url: str):
        super().__init__(convert_charrefs=True)
        self.page_url = page_url
        self.element_stack = []
        self.named_urls = []
        self.seen = set()

    def _add_url(self, media_url: Optional[str]) -> None:
        if not media_url or media_url in self.seen:
            return
        self.seen.add(media_url)
        self.named_urls.append(
            NamedUrl(media_url, Path(urlparse(media_url).path).name)
        )

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        class_names = attributes.get('class', '').split()
        is_post_body = tag == 'div' and 'post-body' in class_names
        anchor_url = None
        if tag == 'a':
            anchor_url = _coomerfans_media_url(
                self.page_url, attributes.get('href')
            )

        if tag not in self._void_tags:
            self.element_stack.append((tag, is_post_body, anchor_url, None))

        if not any(entry[1] for entry in self.element_stack):
            return

        if anchor_url:
            self._add_url(anchor_url)
            return

        if tag == 'img':
            srcset_url = _largest_srcset_candidate(attributes.get('srcset'))
            candidate = (
                attributes.get('data-original')
                or srcset_url
                or attributes.get('data-src')
                or attributes.get('src')
            )
            media_url = _coomerfans_media_url(self.page_url, candidate)
            if media_url:
                media_ext = media_extension(media_url)
                enclosing_link = next(
                    (
                        entry[2]
                        for entry in reversed(self.element_stack)
                        if entry[2]
                    ),
                    None,
                )
                picture_source = next(
                    (
                        entry[3]
                        for entry in reversed(self.element_stack)
                        if entry[0] == 'picture' and entry[3]
                    ),
                    None,
                )
                if picture_source and media_ext in IMG_EXTS:
                    return
                if not enclosing_link or media_extension(enclosing_link) != media_ext:
                    self._add_url(media_url)
            return

        if tag in {'video', 'source', 'audio'}:
            candidate = (
                attributes.get('data-src')
                or _largest_srcset_candidate(attributes.get('srcset'))
                or attributes.get('src')
            )
            media_url = _coomerfans_media_url(self.page_url, candidate)
            if not media_url:
                return

            picture_index = next(
                (
                    index
                    for index in range(len(self.element_stack) - 1, -1, -1)
                    if self.element_stack[index][0] == 'picture'
                ),
                None,
            )
            if picture_index is not None and media_extension(media_url) in IMG_EXTS:
                picture = self.element_stack[picture_index]
                if not picture[3]:
                    self.element_stack[picture_index] = (
                        picture[0],
                        picture[1],
                        picture[2],
                        media_url,
                    )
                    self._add_url(media_url)
            else:
                self._add_url(media_url)

    def handle_endtag(self, tag):
        for index in range(len(self.element_stack) - 1, -1, -1):
            if self.element_stack[index][0] == tag:
                del self.element_stack[index:]
                break


def parse_post_body_media_urls_from_html(
    url: str, html: Optional[str] = None
) -> List[NamedUrl]:
    if html is None:
        try:
            html = fetch_html(url)
        except HtmlFetchError:
            return []
    if html is None:
        return []

    parser = _PostBodyMediaParser(url)
    parser.feed(html)
    parser.close()
    return parser.named_urls


def parse_post_urls_from_html(url: str, html: str) -> List[str]:
    page = urlparse(url)
    page_path = page.path.rstrip('/')
    service, creator_id, creator = parse_creator_identity(url)
    creator_key = creator_id or creator
    seen = set()
    post_urls = []
    for candidate in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        candidate = urljoin(url, unescape(candidate))
        post = urlparse(candidate)
        if post.netloc.lower() != page.netloc.lower():
            continue
        relative_segments = []
        if post.path.startswith(f'{page_path}/'):
            relative_segments = [
                segment for segment in post.path[len(page_path):].split('/') if segment
            ]
        is_nested_post = (
            len(relative_segments) == 1
            or (len(relative_segments) == 2 and relative_segments[0] == 'post')
        )
        post_service, post_creator_id, post_creator = parse_creator_identity(candidate)
        post_creator_key = post_creator_id or post_creator
        post_segments = [segment for segment in post.path.split('/') if segment]
        is_post_route = (
            (len(post_segments) >= 4 and post_segments[0] == 'p')
            or (len(post_segments) >= 5 and post_segments[-2] == 'post')
        )
        is_creator_post = (
            is_post_route
            and post_service == service
            and post_creator_key == creator_key
        )
        if not (is_nested_post or is_creator_post):
            continue
        if media_extension(post.path) in IMG_EXTS + VID_EXTS:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        post_urls.append(candidate)
    return post_urls


def parse_creator_pagination_urls(url: str, html: str) -> List[str]:
    page = urlparse(url)
    seen = set()
    page_urls = []
    for candidate in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        candidate = urljoin(url, unescape(candidate))
        parsed = urlparse(candidate)
        if parsed.netloc.lower() != page.netloc.lower() or parsed.path != page.path:
            continue
        page_numbers = parse_qs(parsed.query).get('page', [])
        if not any(number.isdigit() and int(number) > 1 for number in page_numbers):
            continue
        if candidate not in seen:
            seen.add(candidate)
            page_urls.append(candidate)
    return page_urls


def parse_creator_identity(url: str) -> Tuple[str, Optional[str], str]:
    segments = [segment for segment in urlparse(url).path.split('/') if segment]
    if not segments:
        return '', None, ''

    if segments[0] == 'u' and len(segments) >= 3:
        if len(segments) >= 5 and segments[3] == 'post':
            return segments[1], None, segments[2]
        if len(segments) >= 4:
            return segments[1], segments[2], segments[3]
        return segments[1], None, segments[2]

    if segments[0] == 'p' and len(segments) >= 4:
        return segments[3], segments[2], segments[2]

    if len(segments) >= 3 and segments[-2] == 'user':
        return segments[-3], None, segments[-1]

    if len(segments) >= 2 and segments[-2] == 'post':
        if segments[0] == 'u' and len(segments) >= 5:
            return segments[1], segments[2], segments[3]
        return segments[-5], None, segments[-3]

    if len(segments) >= 2 and segments[-1] == 'post':
        if segments[0] == 'u' and len(segments) >= 4:
            return segments[1], segments[2], segments[3]
        return segments[0], None, segments[-1]

    return segments[0], None, segments[-1] if len(segments) > 1 else ''


def parse_creator_name_from_html(url: str, html: Optional[str]) -> Optional[str]:
    if not html:
        return None

    service, creator_id, _ = parse_creator_identity(url)
    if not creator_id:
        return None

    for candidate in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        creator_url = urljoin(url, unescape(candidate))
        if base_url(creator_url) != 'https://coomerfans.com':
            continue
        creator_service, linked_creator_id, creator_name = parse_creator_identity(
            creator_url
        )
        if (
            creator_service == service
            and linked_creator_id == creator_id
            and creator_name
            and creator_name != creator_id
        ):
            return creator_name
    return None


"""
Process a pre-fetched media URL
- url: Main URL of the pre-fetched media
- skip_img: If image downloads should be skipped.
- skip_vid: If video downloads should be skipped.
Returns a single-element list of NamedUrl for the pre-fetched media.
"""
def process_prefetched( url: str
                      , skip_img: bool
                      , skip_vid: bool ) -> List[NamedUrl]:
    # Use the hash as the name
    name = Path(urlparse(url).path).name

    # Ensure it shouldn't be skipped (for some reason)
    ext = media_extension(url)
    if (ext in IMG_EXTS and skip_img) or (ext in VID_EXTS and skip_vid):
        return []

    # Create the NamedUrl
    named_urls = [ NamedUrl(url, name) ]
    return named_urls


"""
Process a post to get all media URLs
- url: Main URL of the post.
- skip_img: If image downloads should be skipped.
- skip_vid: If video downloads should be skipped.
Returns a list of NamedUrl extracted from the post.
"""
def process_post( url: str
                , skip_img: bool
                , skip_vid: bool
                , html: Optional[str] = None ) -> List[NamedUrl]:
    if base_url(url) != 'https://coomerfans.com':
        logger.error(f'Cannot download post because no Coomerfans page was found: {url}')
        return []

    logger.info('Parsing media URLs from Coomerfans post body')
    named_urls = parse_post_body_media_urls_from_html(url, html)
    named_urls = [
        named_url
        for named_url in named_urls
        if not (
            (skip_img and media_extension(named_url.url) in IMG_EXTS)
            or (skip_vid and media_extension(named_url.url) in VID_EXTS)
        )
    ]
    logger.info(f'Found {len(named_urls)} media files to download')

    # Remove duplicates by finding multiple posts that show the same media
    # I imagine this can only happen by mistake of the uploader
    seen = set()
    unique_urls = []
    for nu in named_urls:
        if nu.url not in seen:
            seen.add(nu.url)
            unique_urls.append(nu)
    return unique_urls


"""
Process a page to get all media URLs.
- url: Main URL of the page.
- skip_img: If image downloads should be skipped.
- skip_vid: If video downloads should be skipped.
- offsets: Range of offsets to download.
Returns a list of NamedUrl extracted from all posts belonging to the page.
"""
def process_page( url: str
                , skip_img: bool
                , skip_vid: bool
                , offsets: Tuple[Optional[int], Optional[int]]
                , retries: Optional[int] = None ) -> List[NamedUrl]:
    if base_url(url) != 'https://coomerfans.com':
        logger.error(f'Cannot download page because no Coomerfans page was found: {url}')
        return []

    try:
        page_html = fetch_html(url, retries=retries)
    except HtmlFetchError as exc:
        logger.warning(f'Aborting creator scan for {url}: {exc}')
        return []
    if page_html is None:
        return []

    page_urls = [url]
    seen_pages = {url}
    post_urls = []
    seen_posts = set()
    page_index = 0
    while page_index < len(page_urls):
        page_url = page_urls[page_index]
        try:
            current_html = (
                page_html
                if page_index == 0
                else fetch_html(page_url, retries=retries)
            )
        except HtmlFetchError as exc:
            logger.warning(f'Aborting creator scan at {page_url}: {exc}')
            break
        page_index += 1
        if current_html is None:
            continue

        for post_url in parse_post_urls_from_html(page_url, current_html):
            if post_url not in seen_posts:
                seen_posts.add(post_url)
                post_urls.append(post_url)
        for next_page in parse_creator_pagination_urls(page_url, current_html):
            if next_page not in seen_pages:
                seen_pages.add(next_page)
                page_urls.append(next_page)
        if offsets[1] is not None and offsets[1] > 0 and len(post_urls) >= offsets[1]:
            break

    logger.info(f'Found {len(post_urls)} post links on creator page')
    start = 0 if offsets[0] is None else max(0, offsets[0] - 1)
    end = None if offsets[1] is None else offsets[1]
    named_urls = []
    for post_url in post_urls[start:end]:
        try:
            post_html = fetch_html(post_url, retries=retries)
        except HtmlFetchError as exc:
            logger.warning(f'Aborting creator scan at {post_url}: {exc}')
            break
        if post_html is not None:
            named_urls.extend(
                parse_post_body_media_urls_from_html(post_url, post_html)
            )

    named_urls = [
        named_url
        for named_url in named_urls
        if not (
            (skip_img and media_extension(named_url.url) in IMG_EXTS)
            or (skip_vid and media_extension(named_url.url) in VID_EXTS)
        )
    ]
    logger.info(f'Found {len(named_urls)} media files to download')

    # Remove duplicates by finding multiple posts that show the same media
    seen = set()
    unique_urls = []
    for nu in named_urls:
        if nu.url not in seen:
            seen.add(nu.url)
            unique_urls.append(nu)
    return unique_urls


"""Build a stable download folder name for a creator."""
def get_creator_name(base: str, service: str, creator: str) -> str:
    return f'{creator}_{service}'


"""
Remove duplicate URLs based on existing file hashes or stable media filenames.
- dst: Directory to check for existing files.
- named_urls: URLs to remove duplicates from.
Returns a possibily reduced list of URLs
"""
def purge_duplicate_urls(
    dst: Path,
    named_urls: List[NamedUrl],
    hashes: Optional[Dict[str, Path]] = None,
) -> List[NamedUrl]:
    if hashes is None:
        hashes = compute_file_hash_map(dst)
    existing_filenames = {
        file.name.casefold()
        for file in dst.glob('**/*')
        if file.is_file() and file.suffix != '.part'
    }

    unique_urls = []
    for nu in named_urls:
        media_path = Path(urlparse(nu.url).path)
        url_hash = media_path.stem
        filenames = {nu.name.casefold(), media_path.name.casefold()}
        if not (url_hash in hashes or filenames.intersection(existing_filenames)):
            unique_urls.append(nu)
        else:
            logger.debug(f'Removing already downloaded media: {media_path.name}')
    return unique_urls


"""
Driver function to download media from Coomerfans
- urls: List of URLs to download from (page, post, or pre-fetched media)
- dst: Download destination for the media (per-creator subfolders will be made)
- skip_img: If image files should be skipped when downloading
- skip_vid: If video files should be skipped whne downloading
- offsets: Post offsets to start from and end at when downloading a page.
- dump_urls: If URLs should be dumped instead of downloaded from.
- jobs: Maximum number of threads to perform downloads, one thread per download.
- retries: Number of retries per media file
"""
def main( urls: List[str]
        , dst: Path
        , skip_img: bool
        , skip_vid: bool
        , offsets: Tuple[Optional[int], Optional[int]]
        , dump_urls: bool
        , jobs: int
        , retries: int) -> None:

    # Loop through the URLs to get more URLs
    hashes = None
    for url in urls:
        logger.info(f'Parsing argument-provided URL "{url}"')
        if base_url(url) != 'https://coomerfans.com':
            logger.error(f'Only Coomerfans URLs can be downloaded: {url}')
            continue

        path_segments = [segment for segment in urlparse(url).path.split('/') if segment]
        if len(path_segments) < 2:
            logger.error('The URL is malformed')
            return
        user = None
        service, creator_id, creator = parse_creator_identity(url)
        creator_key = creator_id or creator

        # Fetch URLs to download media from a post
        if (
            len(path_segments) >= 2
            and (path_segments[0] == 'p' or path_segments[-2] == 'post')
        ):
            logger.debug('URL is suspected to be a post')
            if offsets[0] is not None or offsets[1] is not None:
                logger.warning('Start and end offsets are ignored when downloading a post')
            try:
                post_html = fetch_html(url, retries=retries)
            except HtmlFetchError as exc:
                logger.warning(f'Skipping post {url}: {exc}')
                continue
            creator_name = parse_creator_name_from_html(url, post_html) or creator
            user = get_creator_name(base_url(url), service, creator_name)
            named_urls = (
                process_post(url, skip_img, skip_vid, post_html)
                if post_html is not None
                else []
            )

        # Fetch URLs to download media from pre-fetched media
        elif 'data' in path_segments or 'storage' in path_segments:
            logger.debug('URL is suspected to be pre-fetched media')
            if offsets[0] is not None or offsets[1] is not None:
                logger.warning('Start and end offsets are ignored when downloading pre-fetched media')
            logger.warning('Cannot determine username for pre-fetched media. Download will be in a folder named "unknown"')
            user = 'unknown'
            named_urls = process_prefetched(url, skip_img, skip_vid)

        # Fetch URLs to download media from a page
        else:
            logger.debug('URL is suspected to be a page')
            user = get_creator_name(base_url(url), service, creator)
            named_urls = process_page(
                url, skip_img, skip_vid, offsets, retries=retries
            )

        # Remove URLs of files that already exist
        dst_root = dst / user
        if hashes is None:
            logger.info(f'Computing SHA-256 hashes of existing files in {dst}')
            hashes = compute_file_hash_map(dst)
        named_urls = purge_duplicate_urls(dst_root, named_urls, hashes)
        logger.info(f'New number of media files to download is {len(named_urls)}')

        # Conditionally dump the URLs and return
        if dump_urls:
            for nu in named_urls:
                print(f'{nu.name}\t{nu.url}') # Print is used here instead of logging for a better UX
            return

        # Create the folder tree for the download destination
        create_folder_tree(dst, user, skip_img, skip_vid)

        # Perform the downloads
        dst_pics = dst_root / 'pics'
        dst_vids = dst_root / 'vids'
        logger.info(f'Downloading to {dst / user}')
        multithread_download(
            named_urls,
            dst_pics,
            dst_vids,
            hashes=hashes,
            workers=jobs,
            retries=retries,
        )

    return