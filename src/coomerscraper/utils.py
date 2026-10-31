import hashlib
import logging
import os
import re
from pathlib import Path
from sys import maxsize
from typing import Dict, Optional, Set, Tuple
from urllib.parse import urlparse, urlunparse


logger = logging.getLogger(__name__)


"""Return the canonical Coomerfans origin or the supplied site's origin."""
def base_url(url: str) -> str:
    if '://' not in url:
        url = f'https://{url}'
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if host == 'coomerfans.com' or host.endswith('.coomerfans.com'):
        base = 'https://coomerfans.com'
    elif parsed.netloc:
        netloc = parsed.netloc.lower()
        if netloc.startswith('www.'):
            netloc = netloc[4:]
        base = f'https://{netloc}'
    else:
        base = ''
    logger.debug(f'Base URL of ({url}) --> ({base})')
    return base


"""
Compute the hash of all files in a directory, including subdirectories.
This is the hash that is used in the media URLs.
- root: Starting path to hash from.
Returns a set of unique hashes from root.
"""
def compute_file_hashes(root: Path) -> Set[str]:
    return set(compute_file_hash_map(root))


def compute_file_hash(file: Path) -> str:
    curr_hash = hashlib.sha256()
    with file.open('rb') as f:
        while True:
            chunk = f.read(10 * 1024)
            if not chunk:
                break
            curr_hash.update(chunk)
    return curr_hash.hexdigest()


def compute_file_hash_map(root: Path) -> Dict[str, Path]:
    hashes = {}
    for file in root.glob('**/*'):
        if file.is_dir() or file.suffix == '.part':
            continue
        hashes.setdefault(compute_file_hash(file), file)
    return hashes


"""
Create the folder structure for download destinations
- dst: Root destination for all downloads
- user: Subfolder in the root, usually based on the username
- skip_img: If images are being skipped for downloads
- skip_vid: If videos are being skipped for downloads
"""
def create_folder_tree(dst: Path, user: str, skip_img: bool, skip_vid: bool) -> None:
    dst_pics = dst / user / 'pics'
    dst_vids = dst / user / 'vids'
    try:
        if not skip_img:
            os.makedirs(dst_pics)
    except FileExistsError:
        pass
    try:
        if not skip_vid:
            os.makedirs(dst_vids)
    except FileExistsError:
        pass
    return


"""
Round offsets in an API-friendly that includes the intended range.
- offsets: Offsets to round.
- modulo: Unit to round down or up by.
Returns the offsets rounded to the nearest number that is divisble by the modulo.
"""
def round_offsets( offsets: Tuple[Optional[int], Optional[int]]
                 , modulo: int) -> Tuple[int, int]:
    rounded_start = 0
    if offsets[0] is not None and offsets[0] % modulo != 0:
        rounded_start = offsets[0] - (offsets[0] % modulo)
    rounded_end = maxsize
    if offsets[1] is not None:
        rounded_end = offsets[1] - (offsets[1] % modulo) + modulo
        if offsets[1] % modulo == 0:
            rounded_end -= modulo
    if rounded_start != 0 or rounded_end != maxsize:
        rounded_start_str = '0' if offsets[0] is None else str(rounded_start)
        rounded_end_str = 'inf' if offsets[1] is None else str(rounded_end)
        start_str = '0' if offsets[0] is None else str(offsets[0])
        end_str = 'inf' if offsets[1] is None else str(offsets[1])
        logger.info(f'Fetching posts in clamped range [{rounded_start_str}, {rounded_end_str}].')
        logger.info(f'This will be pruned to [{start_str}, {end_str}] before downloading.')
    return (rounded_start, rounded_end)


"""
Sanitize the beginning of a URL to include https://www.
- url: URL to sanitize.
Returns the sanitized URL.
"""
def sanitize_url(url: str) -> str:
    if '://' not in url:
        url = f'https://{url}'

    parsed = urlparse(url)
    scheme = parsed.scheme or 'https'
    host = parsed.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    sanitized = urlunparse((scheme, host, parsed.path.rstrip('/'), '', parsed.query, ''))
    logger.debug(f'Santized URL ({url}) --> ({sanitized})')
    return sanitized


"""
Convert a sentence to CamelCase.
- sentence: Sentence to convert.
Returns the camel case equivalent.
"""
def to_camel(sentence: str) -> str:
    return ''.join([ word.capitalize() for word in sentence.split() ])