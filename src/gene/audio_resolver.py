"""Hostinger audio preparation and a download-only, private home relay."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from gene.audio_cache import MediaCache

VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}
MEDIA_EXTENSIONS = {'.webm', '.m4a', '.mp4', '.ogg', '.mp3'}
# A refusal crosses the relay, host and runtime only as one of these fixed codes in this
# header. Upstream text, stream URLs and extractor traces never travel with it.
REFUSAL_HEADER = 'X-Gene-Refusal'
REFUSALS = {'too_long': 'Use a public track no longer than 30 minutes.',
            'too_large': 'This track exceeds the 64 MiB audio limit.',
            'unsupported': 'This source currently supports ordinary public YouTube tracks.'}


def refusal(reason: str) -> HTTPException:
    return HTTPException(422, REFUSALS[reason], headers={REFUSAL_HEADER: reason})


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


def create_app(token: str | None = None, extractor=None, *, cache_dir=None, downloader=None,
               relay_url=None, relay_token=None, relay_only=False, allowed_peer=None,
               transport=None, cache_options=None):
    token = token or os.environ.get('GENE_RESOLVER_TOKEN', '')
    if len(token) < 32:
        raise ValueError('Configure a private resolver token before starting')
    if relay_url:
        address = urlparse(relay_url)
        if (address.scheme != 'http' or not address.hostname or address.username or address.password
                or address.path or address.query or address.fragment
                or ipaddress.ip_address(address.hostname) not in ipaddress.ip_network('100.64.0.0/10')
                or len(relay_token or '') < 32):
            raise ValueError('The home relay must use a private Tailscale address and token')
    folder = Path(cache_dir or os.getenv('GENE_AUDIO_CACHE') or Path(__file__).parent / 'media')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    cache = None if relay_only else MediaCache(folder, **(cache_options or {}))
    transfer_slot = threading.BoundedSemaphore(1)
    slots = threading.BoundedSemaphore(2)
    metadata = {}
    if relay_only:
        # A restarted relay has no live transfers; remove only its own abandoned directories.
        for old in folder.glob('download-*'):
            if old.is_dir() and not old.is_symlink():
                shutil.rmtree(old)

    @asynccontextmanager
    async def lifespan(app):
        async def cleanup():
            while True:
                await asyncio.sleep(3600)
                await asyncio.to_thread(cache.clean)
        task = asyncio.create_task(cleanup()) if cache else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    if allowed_peer:
        @app.middleware('http')
        async def private_peer(request: Request, call_next):
            if not request.client or request.client.host != allowed_peer:
                from fastapi.responses import JSONResponse
                return JSONResponse({'detail': 'Private relay'}, status_code=403)
            return await call_next(request)

    def remote(route, identifier):
        with httpx.Client(timeout=90, transport=transport, trust_env=False,
                          follow_redirects=False) as client:
            response = client.post(relay_url + route, json={'identifier': identifier},
                                   headers={'Authorization': 'Bearer ' + relay_token})
            response.raise_for_status()
            return response.json()

    def receive(identifier, destination, maximum):
        digest, size = hashlib.sha256(), 0
        with httpx.Client(timeout=90, transport=transport, trust_env=False,
                          follow_redirects=False) as client:
            with client.stream('POST', relay_url + '/transfer', json={'identifier': identifier},
                               headers={'Authorization': 'Bearer ' + relay_token}) as response:
                reason = response.headers.get(REFUSAL_HEADER)
                if response.status_code == 422 and reason in REFUSALS:
                    raise refusal(reason)  # The relay's own text is never read.
                response.raise_for_status()
                extension = response.headers.get('x-gene-extension')
                expected_size = int(response.headers.get('content-length', '0'))
                expected_hash = response.headers.get('x-gene-sha256', '')
                if extension not in MEDIA_EXTENSIONS or not 0 < expected_size <= maximum:
                    raise ValueError('Invalid audio transfer')
                if not re.fullmatch('[a-f0-9]{64}', expected_hash):
                    raise ValueError('Missing audio checksum')
                row = json.loads(base64.b64decode(response.headers['x-gene-metadata'], validate=True))
                if row.get('identifier') != identifier:
                    raise ValueError('Wrong transferred track')
                path = destination / ('audio' + extension)
                started = time.monotonic()
                with path.open('xb') as output:
                    for chunk in response.iter_bytes(256 * 1024):
                        size += len(chunk)
                        if size > expected_size or time.monotonic() - started > 90:
                            raise ValueError('Audio transfer exceeded its limit')
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != expected_size or not hmac.compare_digest(digest.hexdigest(), expected_hash):
                    raise ValueError('Incomplete audio transfer')
                metadata[identifier] = row
                return path

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
                   'js_runtimes': {'deno': {'path': os.getenv('GENE_DENO_PATH', '/opt/homebrew/bin/deno')}}}
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
        video = parse_qs(urlparse(identifier).query).get('v', [''])[0]
        if cache and VIDEO_ID.fullmatch(video):
            saved = cache.metadata(video)
            if saved:
                return {'tracks': [saved], 'search': False, 'playlist': False, 'name': 'YouTube'}
        if relay_url:
            try:
                result = remote('/load', body.identifier)
                return result
            except Exception:
                raise HTTPException(502, 'The home download relay is unavailable. Cached tracks still work.') from None
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

    def prepare(identifier, folder, max_bytes):
        for attempt in range(2):
            result = extract('https://www.youtube.com/watch?v=' + identifier, flat=False)
            duration = result.get('duration') or 0
            if result.get('is_live') or not public_stream(result.get('url', '')):
                raise refusal('unsupported')
            if duration > 1800:
                raise refusal('too_long')
            if result.get('protocol') != 'https' or not duration > 0:
                raise refusal('unsupported')
            if (result.get('filesize') or 0) > max_bytes:
                raise refusal('too_large')
            try:
                path = download(result, folder, max_bytes)
                metadata[identifier] = {'identifier': identifier, 'title': (result.get('title') or 'YouTube track')[:300],
                    'author': (result.get('uploader') or result.get('channel') or 'YouTube')[:300],
                    'length': int(result['duration'] * 1000),
                    'uri': 'https://www.youtube.com/watch?v=' + identifier}
                return path
            except (ValueError, HTTPException):
                raise
            except Exception:
                if attempt:
                    raise
                # Refresh a rejected/expired stream URL and discard its partial transfer.
                for path in folder.iterdir():
                    if path.is_file():
                        path.unlink()

    class TransferResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            # Read the file here so cleanup cannot race a server-side path-send extension.
            scope = {**scope, 'extensions': {key: value for key, value in scope.get('extensions', {}).items()
                                           if key != 'http.response.pathsend'}}
            try:
                await super().__call__(scope, receive, send)
            finally:
                # Runs on success, interrupted sends, and client disconnects.
                self.temporary.cleanup()
                transfer_slot.release()

    @app.post('/transfer', dependencies=[Depends(authenticate)])
    def transfer(body: Query):
        if not relay_only:
            raise HTTPException(404, 'Not a download relay')
        if not VIDEO_ID.fullmatch(body.identifier):
            raise HTTPException(400, 'A video ID is required')
        if not transfer_slot.acquire(blocking=False):
            raise HTTPException(429, 'The home downloader is busy. Try again shortly.')
        temporary = tempfile.TemporaryDirectory(prefix='download-', dir=folder)
        try:
            path = prepare(body.identifier, Path(temporary.name), 64 * 1024 * 1024)
            if path.is_symlink() or path.resolve().parent != Path(temporary.name).resolve():
                raise ValueError('Invalid download')
            if path.suffix not in MEDIA_EXTENSIONS or not 0 < path.stat().st_size <= 64 * 1024 * 1024:
                raise ValueError('Unsupported audio file')
            with path.open('rb') as source:
                digest = hashlib.file_digest(source, 'sha256').hexdigest()
            row = metadata.pop(body.identifier)
            response = TransferResponse(path, media_type='application/octet-stream', headers={
                'X-Gene-Extension': path.suffix, 'X-Gene-SHA256': digest,
                'X-Gene-Metadata': base64.b64encode(json.dumps(row).encode()).decode()})
            response.temporary = temporary
            return response
        except Exception as exc:
            temporary.cleanup()
            transfer_slot.release()
            metadata.pop(body.identifier, None)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(502, 'This track could not be downloaded.') from None

    def prepared(body: Query, *, ttl=7200):
        if not cache:
            raise HTTPException(404, 'Playback runs on Hostinger')
        if not VIDEO_ID.fullmatch(body.identifier):
            raise HTTPException(400, 'A video ID is required')
        def obtain(folder, maximum):
            if relay_url:
                return receive(body.identifier, folder, maximum)
            return prepare(body.identifier, folder, maximum)
        try:
            with cache.lock:
                path = cache.get(body.identifier, obtain)
                row = metadata.pop(body.identifier, None)
                if row:
                    cache.save_metadata(body.identifier, row)
                lease = cache.pin(body.identifier, ttl=ttl)
        except HTTPException:
            metadata.pop(body.identifier, None)
            raise
        except Exception:
            metadata.pop(body.identifier, None)
            raise HTTPException(502, 'This track could not be prepared. Try again or choose another track.') from None
        return {'path': str(path), 'lease': lease}

    @app.post('/resolve', dependencies=[Depends(authenticate)])
    def resolve(body: Query):
        return prepared(body)

    @app.post('/prepare', dependencies=[Depends(authenticate)])
    def prepare_selected(body: Query):
        # A short durable pin spans transport handoff. The source then takes its own
        # playback lease. Orphaned handoffs expire without periodic work on the Mac.
        prepared(body, ttl=180)
        return {'ready': True}

    @app.post('/release', dependencies=[Depends(authenticate)])
    def release(body: Query):
        if cache:
            cache.release(body.identifier)
        return {'ok': True}

    return app


def main():
    import uvicorn
    relay_only = os.getenv('GENE_RELAY_ONLY') == 'true'
    options = {} if relay_only else {
        'max_bytes': int(os.getenv('GENE_AUDIO_CACHE_BYTES', str(256 * 1024 * 1024))),
        'ttl': int(os.getenv('GENE_AUDIO_CACHE_TTL', '604800')),
        'reserve_bytes': int(os.getenv('GENE_AUDIO_RESERVE_BYTES', str(2 * 1024**3))),
        'evict': True}
    app = create_app(relay_url=os.getenv('GENE_RELAY_URL'),
                     relay_token=os.getenv('GENE_RELAY_TOKEN'), relay_only=relay_only,
                     allowed_peer=os.getenv('GENE_RELAY_PEER'), cache_options=options)
    config = uvicorn.Config(app, host=os.getenv('GENE_RESOLVER_BIND', '127.0.0.1'),
                            port=int(os.getenv('GENE_RESOLVER_PORT', '2334')),
                            access_log=False, log_level='warning', proxy_headers=False)
    if relay_only:
        from gene.audio_relay_server import DownloadRelayServer
        DownloadRelayServer(config).run()
    else:
        uvicorn.Server(config).run()


if __name__ == '__main__':
    main()
