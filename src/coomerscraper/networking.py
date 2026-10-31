import logging
import queue
import requests
import time

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tqdm import tqdm
from typing import Dict, List, Optional
from urllib.parse import urlparse

from .utils import compute_file_hash


IMG_EXTS = [ 'jpg', 'jpeg', 'png', 'gif', 'webp' ]
VID_EXTS = [ 'mp4', 'm4v', 'mkv', 'mov', 'wmv', 'webm', 'avi', 'flv', 'mp3' ]

THROTTLE_TIME = 30
CHUNK_SIZE = 10 * 1024

logger = logging.getLogger(__name__)


def media_extension(url: str) -> str:
    return Path(urlparse(url).path).suffix.lower().lstrip('.')


"""
Wrapper class to associate a URL with a name
"""
@dataclass
class NamedUrl:
    url: str
    name: str


"""
Wrapper class to describe download progress
"""
@dataclass
class _ProgressUpdate:
    slot: int
    done: int
    total: Optional[int]
    desc: str
    n: int
    finished: bool = False
    paused: bool = False
    succeeded: bool = False


"""
Download a single media URL, resuming after interrupted streams.
- url: NamedUrl to download.
- dst: Destination of the URL.
- slot: Position in the progress rendering
- q: The queue that this job belongs to
- retries: Number of retries per media file
"""
def _download( url: NamedUrl, dst: Path, slot: int, q: queue.Queue, retries: int ) -> None:
    tmp = dst.with_suffix(dst.suffix + '.part')
    total = None
    errcnt = 0
    succeeded = False

    while True:
        headers = {
            'Connection': 'close',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
            'Accept': '*/*',
        }
        done = tmp.stat().st_size if tmp.exists() else 0
        if done > 0:
            headers['Range'] = f'bytes={done}-'

        q.put(_ProgressUpdate(slot, done, total, str(dst), errcnt))

        try:
            with requests.get(url.url, stream=True, timeout=(10, 60), headers=headers) as res:
                res.raise_for_status()

                if total is None:
                    cl = res.headers.get('Content-Length')
                    cr = res.headers.get('Content-Range')
                    if cr is not None:
                        total = int(cr.split('/')[-1])
                    elif cl is not None:
                        total = int(cl)

                with tmp.open('ab') as f:
                    for chunk in res.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        errcnt = 0
                        f.write(chunk)
                        done += len(chunk)
                        q.put(_ProgressUpdate(slot, done, total, str(dst), errcnt))
                tmp.replace(dst)
                succeeded = True

        except (requests.RequestException, requests.exceptions.ReadTimeout):
            errcnt += 1
            q.put(_ProgressUpdate(slot, done, total, str(dst), errcnt, paused=True))
            if retries is None or errcnt < retries:
                time.sleep(THROTTLE_TIME)
                continue
        
        q.put(
            _ProgressUpdate(
                slot,
                done,
                total,
                str(dst),
                errcnt,
                finished=True,
                succeeded=succeeded,
            )
        )
        return


"""
Use the Coomer/Kemono API to fetch a collection of posts.
- base: Base URL for the API (includes up the the TLD).
- service: Service the media originates from.
- creator: Creator of the media.
- offset: Offset into all of the posts to fetch from.
Returns a collection of posts, including possibly an empty collection
"""
def api_fetch_post_multi(base: str, service: str, creator: str, offset: int) -> List[dict]:
    api_url = f'{base}/api/v1/{service}/user/{creator}/posts?o={offset}'
    while True:
        try:
            res = requests.get(api_url, headers={'accept': 'text/css'})
        except Exception:
            if res.status_code in [429, 403]:
                time.sleep(THROTTLE_TIME)
        else:
            break

    if res.status_code != 200:
        logger.error(f'Failed to fetch posts using the API ({api_url}) --> {res.status_code}')
        return []

    return res.json()


"""
Use the Coomer/Kemono API to fetch a single post.
- base: Base URL for the API (includes up the the TLD).
- service: Service the media originates from.
- creator: Creator of the media.
- post_id: Post ID to query.
Returns a single post.
"""
def api_fetch_post_single(base: str, service: str, creator: str, post_id: str) -> dict:
    api_url = f'{base}/api/v1/{service}/user/{creator}/post/{post_id}'
    while True:
        try:
            res = requests.get(api_url, headers={'accept': 'text/css'})
        except Exception:
            if res.status_code in [429, 403]:
                time.sleep(THROTTLE_TIME)
        else:
            break

    if res.status_code != 200:
        logger.error(f'Failed to fetch posts using the API ({api_url}) --> {res.status_code}')
        return {}

    return res.json()


"""
Use the Coomer/Kemono API to fetch a creator profile.
- base: Base URL for the API (includes up the the TLD).
- service: Service the media originates from.
- creator: Creator of the media.
Returns a creator profile.
"""
def api_fetch_creator_profile(base: str, service: str, creator: str) -> dict:
    api_url = f'{base}/api/v1/{service}/user/{creator}/profile'
    while True:
        try:
            res = requests.get(api_url, headers={'accept': 'text/css'})
        except Exception:
            if res.status_code in [429, 403]:
                time.sleep(THROTTLE_TIME)
        else:
            break

    if res.status_code != 200:
        logger.error(f'Failed to fetch creator profile using the API ({api_url}) --> {res.status_code}')
        return {}

    return res.json()


"""
Download a list of NamedUrl using multithreading and remove newly downloaded
files whose content already exists locally.
- urls: List of NamedUrl to download.
- dst_pics: Path to download pictures to.
- dst_vids: Path to download videos to.
- hashes: Existing SHA-256 index, updated as downloads complete.
- workers: Maximum number of threads to use for downloading.
- retries: Number of retries per media file
Returns the updated SHA-256 index.
"""
def multithread_download( urls: List[NamedUrl]
                        , dst_pics: Path
                        , dst_vids: Path
                        , hashes: Optional[Dict[str, Path]] = None
                        , workers: int = 8
                        , retries: int = None
                        ) -> Dict[str, Path]:
    q: queue.Queue = queue.Queue()
    hashes = {} if hashes is None else hashes
    url_iter = iter(urls)
    max_desc_width = max((len(u.name) for u in urls), default=0) + 6

    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Create a "master" bar the denotes total progress
        master_bar = tqdm(total=len(urls), unit="file", desc="Total Media Files", position=0)

        # Initialize tqdm bars for the fixed number of workers
        bars: List[tqdm] = [tqdm(unit="B", unit_scale=True, unit_divisor=1024, position=i+1) for i in range(workers)]

        # Helper to submit the next available URL to a specific slot
        def submit_next(slot: int) -> bool:
            try:
                next_url = next(url_iter)
                dst = (dst_pics if media_extension(next_url.url) in IMG_EXTS else dst_vids) / next_url.name
                pool.submit(_download, next_url, dst, slot, q, retries)
                return True
            except StopIteration:
                return False

        # Initialize necessary slots
        active_workers = 0
        for slot in range(workers):
            if submit_next(slot):
                active_workers += 1
            else:
                bars[slot].clear()

        # Continually refill slots as jobs finish
        while active_workers > 0:
            try:
                msg = q.get(timeout=0.1)
            except queue.Empty:
                continue

            bar = bars[msg.slot]
            if msg.paused:
                bar.set_description((f'[ {msg.n}*] ' + msg.desc).ljust(max_desc_width))
            else:
                bar.set_description((f'[ {msg.n} ] ' + msg.desc).ljust(max_desc_width))

            if msg.finished:
                if msg.succeeded:
                    downloaded_path = Path(msg.desc)
                    file_hash = compute_file_hash(downloaded_path)
                    duplicate_path = hashes.get(file_hash)
                    if duplicate_path is not None and duplicate_path != downloaded_path:
                        duplicate_path.unlink()
                        logger.info(
                            f'Replaced duplicate file {duplicate_path} with '
                            f'{downloaded_path} to preserve the current media filename'
                        )
                    hashes[file_hash] = downloaded_path
                master_bar.update(1)
                if submit_next(msg.slot):
                    bar.reset()
                else:
                    active_workers -= 1
            else:
                if msg.total:
                    bar.total = msg.total
                bar.n = msg.done
                bar.refresh()

    return hashes