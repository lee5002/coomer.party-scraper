import logging
import re
from html import unescape
from pathlib import Path
from sys import maxsize
from typing import List, Optional, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import requests

from .networking import ( api_fetch_post_multi, api_fetch_post_single, api_fetch_creator_profile
                        , multithread_download, NamedUrl, IMG_EXTS, VID_EXTS, media_extension )
from .utils import base_url, compute_file_hashes, create_folder_tree, round_offsets, to_camel


POSTS_PER_FETCH = 50

logger = logging.getLogger(__name__)


def fetch_html(url: str) -> Optional[str]:
    try:
        res = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
        res.raise_for_status()
    except Exception as exc:
        logger.warning(f'Failed to fetch HTML for {url}: {exc}')
        return None

    return res.text


def parse_media_urls_from_html(url: str, html: Optional[str] = None) -> List[NamedUrl]:
    if html is None:
        html = fetch_html(url)
    if html is None:
        return []

    seen = set()
    named_urls = []
    for candidate in re.findall(r'(?:src|href|data-src|poster)=["\']([^"\']+)["\']', html, flags=re.I):
        if not candidate:
            continue
        candidate = unescape(candidate)
        if candidate.startswith('//'):
            candidate = 'https:' + candidate
        elif candidate.startswith('/'):
            candidate = f'https://coomerfans.com{candidate}'
        elif '://' not in candidate:
            candidate = 'https://coomerfans.com/' + candidate.lstrip('/')

        if 'coomerfans.com' not in candidate.lower() and 'coomerfans.com' not in url.lower():
            continue
        if not any(path in candidate.lower() for path in ('/storage/', '/istorage/', '/data/')):
            continue

        media_path = urlparse(candidate).path
        ext = media_extension(candidate)
        if ext not in IMG_EXTS and ext not in VID_EXTS:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        named_urls.append(NamedUrl(candidate, Path(media_path).name))
    return named_urls


def parse_post_urls_from_html(url: str, html: str) -> List[str]:
    page = urlparse(url)
    page_path = page.path.rstrip('/')
    seen = set()
    post_urls = []
    for candidate in re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I):
        candidate = urljoin(url, unescape(candidate))
        post = urlparse(candidate)
        if post.netloc.lower() != page.netloc.lower():
            continue
        if not post.path.startswith(f'{page_path}/'):
            continue
        relative_segments = [
            segment for segment in post.path[len(page_path):].split('/') if segment
        ]
        is_post_path = (
            len(relative_segments) == 1
            or (len(relative_segments) == 2 and relative_segments[0] == 'post')
        )
        if not is_post_path or media_extension(post.path) in IMG_EXTS + VID_EXTS:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        post_urls.append(candidate)
    return post_urls


def parse_creator_identity(url: str) -> Tuple[str, Optional[str], str]:
    segments = [segment for segment in urlparse(url).path.split('/') if segment]
    if not segments:
        return '', None, ''

    if segments[0] == 'u' and len(segments) >= 4:
        return segments[1], segments[2], segments[3]

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


def resolve_legacy_to_coomerfans(url: str) -> str:
    if 'coomerfans.com' in url.lower():
        return url

    parsed = urlparse(url)
    host = parsed.netloc.lower().replace('www.', '')
    if not (host.endswith('coomer.st') or host.endswith('coomer.party')):
        return url

    segments = [segment for segment in parsed.path.split('/') if segment]
    if not segments:
        return url

    lookup = None
    service = None
    if len(segments) >= 3 and segments[-2] == 'user':
        service = segments[-3]
        lookup = segments[-1]
    elif len(segments) >= 4 and segments[-2] == 'post':
        service = segments[-4]
        lookup = segments[-3]
    elif len(segments) >= 1:
        lookup = segments[-1]
        service = segments[0] if segments[0] not in {'u'} else None

    if not lookup:
        return url

    search_terms = [lookup]
    if service:
        profile = api_fetch_creator_profile('https://coomer.st', service, lookup)
        if profile and profile.get('name'):
            search_terms.insert(0, profile['name'])

    for term in search_terms:
        search_url = f'https://coomerfans.com/?q={quote_plus(term)}'
        try:
            res = requests.get(search_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=20)
            res.raise_for_status()
        except Exception as exc:
            logger.warning(f'Failed to resolve legacy Coomer URL {url} to coomerfans via query {term}: {exc}')
            continue

        for href in re.findall(r'href=["\']([^"\']+)["\']', res.text, flags=re.I):
            if '/u/' not in href:
                continue
            href_lower = href.lower()
            if service and f'/u/{service}/' not in href_lower:
                continue
            if term.lower() not in href_lower:
                continue
            return urljoin('https://coomerfans.com', href)

    logger.warning(f'No matching coomerfans page found for legacy URL {url}')
    return url


"""
Extract media URLs from a list of post JSONs
- base: Base URL that the media should be one
- posts: List of post JSONs to parse
- skip_img: If images should be skipped
- skip_vid: if videos should be skipped
Returns a list of NamedUrl extracted from the posts
"""
def parse_posts_json( base: str
                    , posts: List[dict]
                    , skip_img: bool
                    , skip_vid: bool ) -> List[NamedUrl]:
    named_urls = []
    base = base.replace('https://', 'https://n1.')
    for post in posts:
        title = to_camel(re.sub(r'[^A-Za-z0-9\s]+', '', post['title']))
        datetime = re.sub('-|:', '', post['published'])
        if 'path' in post['file']:
            ext = media_extension(post['file']['path'])
            if skip_vid and ext in VID_EXTS:
                continue
            if skip_img and ext in IMG_EXTS:
                continue
            name = f'{datetime}-{title}_0.{ext}'
            url = f'{base}/data/{post["file"]["path"]}'
            named_urls.append(NamedUrl(url, name))

        for i, attachment in enumerate(post['attachments']):
            ext = media_extension(attachment['path'])
            if skip_vid and ext in VID_EXTS:
                continue
            if skip_img and ext in IMG_EXTS:
                continue
            url = f'{base}/data/{attachment["path"]}'
            name = f'{datetime}-{title}_{i+1}.{ext}'
            named_urls.append(NamedUrl(url, name))
    return named_urls



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
                , skip_vid: bool ) -> List[NamedUrl]:
    resolved_url = resolve_legacy_to_coomerfans(url)
    base = base_url(resolved_url)
    path = urlparse(resolved_url).path
    segments = [segment for segment in path.split('/') if segment]
    service, creator_id, creator = parse_creator_identity(resolved_url)
    post_id = segments[-1]
    creator_key = creator_id or creator

    html_urls = []
    if 'coomerfans.com' in base:
        html_urls = parse_media_urls_from_html(resolved_url)
        html_urls = [
            nu for nu in html_urls
            if not ((media_extension(nu.url) in IMG_EXTS and skip_img)
                    or (media_extension(nu.url) in VID_EXTS and skip_vid))
        ]

    # coomer.st still exposes the original API interface; use it directly.
    if 'coomer.st' in base:
        logger.info('Using coomer.st API data source for media extraction')

    # Get the JSON version of the post
    post_response = api_fetch_post_single(base, service, creator_key, post_id)
    post = [post_response['post']] if 'post' in post_response else []

    # Parse the media URLs from the post
    logger.info('Parsing media URLs from 1 post')
    named_urls = html_urls + parse_posts_json(base, post, skip_img, skip_vid)
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
                , offsets: Tuple[Optional[int], Optional[int]] ) -> List[NamedUrl]:
    resolved_url = resolve_legacy_to_coomerfans(url)
    base = base_url(resolved_url)
    service, creator_id, creator = parse_creator_identity(resolved_url)
    creator_key = creator_id or creator

    html_urls = []
    if 'coomerfans.com' in base:
        page_html = fetch_html(resolved_url)
        html_urls = parse_media_urls_from_html(resolved_url, page_html)
        post_urls = parse_post_urls_from_html(resolved_url, page_html) if page_html else []
        if post_urls:
            logger.info(f'Found {len(post_urls)} post links on creator page')
            start = 0 if offsets[0] is None else offsets[0] - 1
            end = None if offsets[1] is None else offsets[1]
            for post_url in post_urls[start:end]:
                html_urls.extend(parse_media_urls_from_html(post_url))
        if html_urls:
            seen = set()
            filtered_urls = []
            for nu in html_urls:
                if nu.url in seen:
                    continue
                seen.add(nu.url)
                ext = media_extension(nu.url)
                if (skip_img and ext in IMG_EXTS) or (skip_vid and ext in VID_EXTS):
                    continue
                filtered_urls.append(nu)
            html_urls = filtered_urls

    # coomer.st still exposes the original API interface; use it directly.
    if 'coomer.st' in base:
        logger.info('Using coomer.st API data source for media extraction')

    # Round the offsets
    rounded_offsets = round_offsets(offsets, POSTS_PER_FETCH)

    # Iterate through post ranges for the page
    all_posts = []
    offset = rounded_offsets[0]
    while True:
        logger.info(f'Fetching posts {offset + 1} - {offset + POSTS_PER_FETCH}')
        curr_posts = api_fetch_post_multi(base, service, creator_key, offset)
        all_posts += curr_posts
        offset += POSTS_PER_FETCH
        if len(curr_posts) % POSTS_PER_FETCH != 0 or len(curr_posts) == 0 or offset >= rounded_offsets[1]:
            break
    
    # Prune to user-requested offsets
    if rounded_offsets[0] != 0 and offsets[0] is not None:
        skip_start = offsets[0] - rounded_offsets[0] - 1
        all_posts = all_posts[skip_start:]
    if rounded_offsets[1] != maxsize and offsets[1] is not None:
        skip_end = rounded_offsets[1] - offsets[1]
        all_posts = all_posts[:-skip_end]

    # Parse the media URLs from each post
    logger.info(f'Parsing media URLs from {len(all_posts)} posts')
    named_urls = html_urls + parse_posts_json(base, all_posts, skip_img, skip_vid)
    logger.info(f'Found {len(named_urls)} media files to download')

    # Remove duplicates by finding multiple posts that show the same media
    seen = set()
    unique_urls = []
    for nu in named_urls:
        if nu.url not in seen:
            seen.add(nu.url)
            unique_urls.append(nu)
    return unique_urls


"""
Get creator name as string. Some services only use integer IDs which can be 
replaced by clear names using this function.
Returns creator name as string
"""
def get_creator_name(base: str, service: str, creator: str) -> str:
    if 'coomerfans.com' in base:
        return f'{creator}_{service}'
    profile = api_fetch_creator_profile(base, service, creator)
    if profile:
        return f'{profile["name"]}_{service}'
    else:
        return f'{creator}_{service}'


"""
Remove duplicate URLs based on the SHA-256 hash of existing files.
Note that since URLs are hashes, there should be no duplicates between posts.
- dst: Directory to check for existing files.
- named_urls: URLs to remove duplicates from.
Returns a possibily reduced list of URLs
"""
def purge_duplicate_urls(dst: Path, named_urls: List[NamedUrl]) -> List[NamedUrl]:
    # Get the hashes of the existing files
    hashes = compute_file_hashes(dst)

    # Remove duplicates by finding URLs that includ the hash
    unique_urls = []
    for nu in named_urls:
        url_hash = nu.url.split('/')[-1].split('.')[0]
        if url_hash not in hashes:
            unique_urls.append(nu)
        else:
            logger.debug(f'Removing from download list based on hash: {url_hash}')
    return unique_urls


"""
Driver function to download media from Coomer or Kemono
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
    for url in urls:
        logger.info(f'Parsing argument-provided URL "{url}"')

        path_segments = [segment for segment in urlparse(url).path.split('/') if segment]
        if len(path_segments) < 2:
            logger.error('The URL is malformed')
            return
        user = None
        service, creator_id, creator = parse_creator_identity(url)
        creator_key = creator_id or creator

        # Fetch URLs to download media from a post
        if len(path_segments) >= 2 and path_segments[-2] == 'post':
            logger.debug('URL is suspected to be a post')
            if offsets[0] is not None or offsets[1] is not None:
                logger.warning('Start and end offsets are ignored when downloading a post')
            user = get_creator_name(base_url(url), service, creator_key)
            named_urls = process_post(url, skip_img, skip_vid)

        # Fetch URLs to download media from pre-fetched media
        elif 'data' in path_segments:
            logger.debug('URL is suspected to be pre-fetched media')
            if offsets[0] is not None or offsets[1] is not None:
                logger.warning('Start and end offsets are ignored when downloading pre-fetched media')
            logger.warning('Cannot determine username for pre-fetched media. Download will be in a folder named "unknown"')
            user = 'unknown'
            named_urls = process_prefetched(url, skip_img, skip_vid)

        # Fetch URLs to download media from a page
        else:
            logger.debug('URL is suspected to be a page')
            user = get_creator_name(base_url(url), service, creator_key)
            named_urls = process_page(url, skip_img, skip_vid, offsets)

        # Remove URLs of files that already exist
        dst_root = dst / user
        logger.info(f'Begin hashing files in {dst_root}')
        named_urls = purge_duplicate_urls(dst_root, named_urls)
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
        multithread_download(named_urls, dst_pics, dst_vids, workers=jobs, retries=retries)

    return