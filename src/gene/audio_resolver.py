"""Loopback-only public YouTube extraction for the separate Lavalink adapter."""

from __future__ import annotations

import hmac
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}
MEDIA_EXTENSIONS = {'.webm', '.m4a', '.mp4', '.ogg', '.mp3'}


class MediaCache:
    """Publish only complete files; retain recent tracks while Lavalink may use them."""

    def __init__(self, folder, *, max_bytes=256 * 1024 * 1024, track_bytes=64 * 1024 * 1024, ttl=3600):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.max_bytes, self.track_bytes, self.ttl = max_bytes, track_bytes, ttl
        self.lock = threading.Lock()

    def get(self, identifier, download):
        if not VIDEO_ID.fullmatch(identifier):
            raise ValueError('A video ID is required')
        # One download at a time also bounds temporary disk usage and coalesces duplicates.
        with self.lock:
            now = time.time()
            for path in self.folder.iterdir():
                if path.is_dir() and not path.is_symlink() and path.name.startswith('download-'):
                    if now - path.stat().st_mtime > self.ttl:
                        shutil.rmtree(path)
                if path.is_file() and not path.is_symlink() and path.suffix in MEDIA_EXTENSIONS:
                    if now - path.stat().st_mtime > self.ttl:
                        path.unlink()
            for path in self.folder.glob(identifier + '.*'):
                if path.is_file() and not path.is_symlink() and path.suffix in MEDIA_EXTENSIONS:
                    if 0 < path.stat().st_size <= self.track_bytes:
                        path.touch()
                        return path
            # Count interrupted downloads too, so crashes cannot bypass the disk budget.
            used = sum(p.stat().st_size for p in self.folder.rglob('*') if p.is_file())
            if used + self.track_bytes > self.max_bytes:
                raise HTTPException(503, 'The music cache is full. Try again after older tracks expire.')
            with tempfile.TemporaryDirectory(prefix='download-', dir=self.folder) as temporary:
                folder = Path(temporary)
                candidate = Path(download(folder, self.track_bytes))
                if candidate.is_symlink() or candidate.resolve().parent != folder.resolve():
                    raise ValueError('Invalid cached audio path')
                if candidate.suffix not in MEDIA_EXTENSIONS or not 0 < candidate.stat().st_size <= self.track_bytes:
                    raise ValueError('Audio file exceeds the track limit or uses an unsupported format')
                destination = self.folder / (identifier + candidate.suffix)
                candidate.chmod(0o600)
                candidate.replace(destination)
                return destination


def canonical_query(value: str) -> tuple[str, bool]:
    if value.startswith(('ytsearch:', 'ytmsearch:')):
        query = value.split(':', 1)[1].strip()
        if not query:
            raise ValueError('Enter a song name or YouTube link.')
        return 'ytsearch5:' + query, True
    if VIDEO_ID.fullmatch(value):
        return 'https://www.youtube.com/watch?v=' + value, False
    url = urlparse(value)
    if url.scheme != 'https' or url.hostname not in YOUTUBE_HOSTS or url.port not in (None, 443):
        raise ValueError('Only public YouTube links and searches are supported by this source.')
    query = parse_qs(url.query)
    video = url.path.strip('/') if url.hostname == 'youtu.be' else query.get('v', [''])[0]
    if url.path.startswith(('/shorts/', '/live/')):
        video = url.path.split('/')[2]
    if VIDEO_ID.fullmatch(video):
        return 'https://www.youtube.com/watch?v=' + video, False
    playlist = query.get('list', [''])[0]
    if url.path == '/playlist' and re.fullmatch(r'[A-Za-z0-9_-]{10,100}', playlist):
        return 'https://www.youtube.com/playlist?list=' + playlist, False
    raise ValueError('Use a YouTube video or playlist link.')


def public_stream(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == 'https' and bool(parsed.hostname)
            and parsed.hostname.endswith('.googlevideo.com') and parsed.port in (None, 443)
            and not parsed.username and not parsed.password)


class QuietLogger:
    def debug(self, message):
        pass

    warning = error = debug


class Query(BaseModel):
    identifier: str = Field(min_length=1, max_length=1000)


def create_app(token: str | None = None, extractor=None, *, cache_dir=None, downloader=None):
    token = token or os.environ.get('GENE_RESOLVER_TOKEN', '')
    if len(token) < 32:
        raise ValueError('Configure a private resolver token before starting')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = threading.BoundedSemaphore(2)
    cache = MediaCache(cache_dir or os.getenv('GENE_AUDIO_CACHE') or Path(__file__).parent / 'media')

    def download(result, folder, max_bytes):
        if downloader:
            return downloader(result, folder, max_bytes)
        from yt_dlp import YoutubeDL
        started = time.monotonic()

        def progress(event):
            if event.get('downloaded_bytes', 0) > max_bytes or time.monotonic() - started > 60:
                raise ValueError('Track download exceeded its limit')

        options = {'quiet': True, 'no_warnings': True, 'logger': QuietLogger(), 'cachedir': False,
                   'outtmpl': str(folder / 'audio.%(ext)s'), 'socket_timeout': 10, 'retries': 1,
                   'max_filesize': max_bytes, 'progress_hooks': [progress], 'noplaylist': True,
                   'http_chunk_size': 1024 * 1024}
        with YoutubeDL(options) as client:
            client.process_info(result)
            return Path(client.prepare_filename(result))

    def authenticate(authorization: str = Header(default='')):
        if not hmac.compare_digest(authorization, 'Bearer ' + token):
            raise HTTPException(401, 'Invalid resolver credentials')

    def extract(identifier, *, flat):
        options = {'quiet': True, 'no_warnings': True, 'logger': QuietLogger(),
                   'skip_download': True, 'noplaylist': not flat, 'playlistend': 100,
                   'extract_flat': 'in_playlist' if flat else False,
                   'format': 'bestaudio/best', 'socket_timeout': 20, 'retries': 1,
                   'extractor_retries': 1, 'cachedir': False,
                   'js_runtimes': {'deno': {'path': '/opt/homebrew/bin/deno'}}}
        if not slots.acquire(blocking=False):
            raise HTTPException(429, 'Audio extraction is busy. Try again in a moment.')
        try:
            if extractor:
                return extractor(identifier, options)
            from yt_dlp import YoutubeDL
            with YoutubeDL(options) as client:
                return client.extract_info(identifier, download=False)
        except HTTPException:
            raise
        except Exception:
            # Do not echo extractor traces, temporary stream URLs or authentication details.
            raise HTTPException(502, 'This public track could not be loaded. Try another track.') from None
        finally:
            slots.release()

    @app.get('/health')
    def health():
        return {'ok': True}

    @app.post('/load', dependencies=[Depends(authenticate)])
    def load(body: Query):
        try:
            identifier, search = canonical_query(body.identifier)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None
        result = extract(identifier, flat=True)
        rows = result.get('entries') if result.get('_type') in ('playlist', 'multi_video') else [result]
        tracks = []
        for row in rows or []:
            if not row or not VIDEO_ID.fullmatch(row.get('id', '')):
                continue
            tracks.append({'identifier': row['id'], 'title': row.get('title') or 'YouTube track',
                           'author': row.get('uploader') or row.get('channel') or 'YouTube',
                           'length': int((row.get('duration') or 0) * 1000),
                           'uri': 'https://www.youtube.com/watch?v=' + row['id']})
        return {'tracks': tracks, 'search': search, 'playlist': 'entries' in result,
                'name': result.get('title') or 'YouTube'}

    @app.post('/resolve', dependencies=[Depends(authenticate)])
    def resolve(body: Query):
        if not VIDEO_ID.fullmatch(body.identifier):
            raise HTTPException(400, 'A video ID is required')

        def prepare(folder, max_bytes):
            for attempt in range(2):
                result = extract('https://www.youtube.com/watch?v=' + body.identifier, flat=False)
                if result.get('is_live') or not public_stream(result.get('url', '')):
                    raise HTTPException(422, 'This source currently supports ordinary public YouTube tracks.')
                if result.get('protocol') != 'https' or not 0 < (result.get('duration') or 0) <= 1800:
                    raise HTTPException(422, 'Use a public track no longer than 30 minutes.')
                if (result.get('filesize') or 0) > max_bytes:
                    raise HTTPException(422, 'This track exceeds the 64 MiB audio limit.')
                try:
                    return download(result, folder, max_bytes)
                except (ValueError, HTTPException):
                    raise
                except Exception:
                    if attempt:
                        raise
                    # Refresh a rejected/expired stream URL and discard its partial transfer.
                    for path in folder.iterdir():
                        if path.is_file():
                            path.unlink()

        try:
            path = cache.get(body.identifier, prepare)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(502, 'This track could not be prepared. Try again or choose another track.') from None
        return {'path': str(path)}

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=int(os.getenv('GENE_RESOLVER_PORT', '2334')),
                access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
