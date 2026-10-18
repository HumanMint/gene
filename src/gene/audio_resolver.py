"""Loopback-only public YouTube extraction for the separate Lavalink adapter."""

from __future__ import annotations

import hmac
import os
import re
import threading
from urllib.parse import parse_qs, urlparse

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
YOUTUBE_HOSTS = {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be'}


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


def create_app(token: str | None = None, extractor=None):
    token = token or os.environ.get('GENE_RESOLVER_TOKEN', '')
    if len(token) < 32:
        raise ValueError('Configure a private resolver token before starting')
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = threading.BoundedSemaphore(2)

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
        result = extract('https://www.youtube.com/watch?v=' + body.identifier, flat=False)
        url = result.get('url', '')
        if result.get('is_live') or not public_stream(url):
            raise HTTPException(422, 'This source currently supports ordinary public YouTube tracks.')
        headers = {k: v for k, v in result.get('http_headers', {}).items()
                   if k.lower() in ('user-agent', 'referer', 'origin')}
        return {'url': url, 'headers': headers}

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host='127.0.0.1', port=int(os.getenv('GENE_RESOLVER_PORT', '2334')),
                access_log=False, log_level='warning')


if __name__ == '__main__':
    main()
