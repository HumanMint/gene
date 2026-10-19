import asyncio
import base64
import hashlib
import json
import os
import time
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from gene.audio_cache import MediaCache
from gene.audio_resolver import Query, create_app

ID = 'aBcDeFgH123'
SECOND = 'bBcDeFgH123'
TOKEN = 'x' * 40
HEADERS = {'Authorization': 'Bearer ' + TOKEN}
ROW = {'identifier': ID, 'title': 'Song', 'author': 'Artist', 'length': 174000,
       'uri': 'https://www.youtube.com/watch?v=' + ID}


def extractor():
    return Mock(return_value={'id': ID, 'title': 'Song', 'uploader': 'Artist',
                             'url': 'https://rr1.googlevideo.com/audio',
                             'duration': 174, 'protocol': 'https'})


def download(result, folder, maximum):
    path = folder / 'audio.webm'
    path.write_bytes(b'complete audio')
    return path


def relay(tmp_path, **kwargs):
    return create_app(TOKEN, extractor(), cache_dir=tmp_path, downloader=download,
                      relay_only=True, **kwargs)


def packet(content=b'complete audio'):
    return {'content-length': str(len(content)), 'x-gene-extension': '.webm',
            'x-gene-sha256': hashlib.sha256(content).hexdigest(),
            'x-gene-metadata': base64.b64encode(json.dumps(ROW).encode()).decode()}


def host(tmp_path, handler):
    return TestClient(create_app(TOKEN, cache_dir=tmp_path,
        relay_url='http://100.64.0.1:2334', relay_token='y' * 40,
        transport=httpx.MockTransport(handler)))


def test_home_relay_serves_complete_file_then_deletes_it(tmp_path):
    with TestClient(relay(tmp_path)) as client:
        assert client.post('/transfer', json={'identifier': ID}).status_code == 401
        response = client.post('/transfer', headers=HEADERS, json={'identifier': ID})
        assert response.content == b'complete audio'
        assert response.headers['x-gene-sha256'] == hashlib.sha256(response.content).hexdigest()
        assert not list(tmp_path.iterdir())
        assert client.post('/resolve', headers=HEADERS, json={'identifier': ID}).status_code == 404


def test_relay_deletes_interrupted_sends_and_releases_its_single_slot(tmp_path):
    app = relay(tmp_path)
    transfer = next(route.endpoint for route in app.routes if route.path == '/transfer')
    response = transfer(Query(identifier=ID))
    assert list(tmp_path.glob('download-*'))
    with pytest.raises(HTTPException) as busy:
        transfer(Query(identifier=SECOND))
    assert busy.value.status_code == 429

    async def fail_send(message):
        raise ConnectionError('receiver disappeared')
    async def receive():
        return {'type': 'http.disconnect'}
    scope = {'type': 'http', 'method': 'POST', 'headers': [], 'asgi': {'spec_version': '2.4'}}
    with pytest.raises(ConnectionError):
        asyncio.run(response(scope, receive, fail_send))
    assert not list(tmp_path.iterdir())
    with TestClient(app) as client:
        assert client.post('/transfer', headers=HEADERS, json={'identifier': ID}).status_code == 200


def test_relay_cleans_restart_debris_and_rejects_other_peers(tmp_path):
    old = tmp_path / 'download-interrupted'
    old.mkdir()
    (old / 'partial').write_bytes(b'partial')
    untouched = tmp_path / 'unrelated'
    untouched.mkdir()
    app = relay(tmp_path, allowed_peer='100.64.0.2')
    assert not old.exists() and untouched.exists()
    with TestClient(app, client=('100.64.0.3', 1234)) as client:
        assert client.post('/transfer', headers=HEADERS, json={'identifier': ID}).status_code == 403
    with TestClient(app, client=('100.64.0.2', 1234)) as client:
        assert client.post('/transfer', headers=HEADERS, json={'identifier': ID}).status_code == 200


def test_broken_download_leaves_no_files_and_can_retry(tmp_path):
    def fail(result, folder, maximum):
        (folder / 'audio.part').write_bytes(b'partial')
        raise ValueError('failed')
    with TestClient(create_app(TOKEN, extractor(), cache_dir=tmp_path,
                              downloader=fail, relay_only=True)) as client:
        for _ in range(2):
            assert client.post('/transfer', headers=HEADERS, json={'identifier': ID}).status_code == 502
            assert not list(tmp_path.iterdir())


def test_host_caches_verified_file_and_metadata_without_further_mac_requests(tmp_path):
    calls = []
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer ' + 'y' * 40
        assert request.url.path == '/transfer'
        calls.append(request)
        return httpx.Response(200, headers=packet(), content=b'complete audio')
    client = host(tmp_path, handler)
    request = {'headers': HEADERS, 'json': {'identifier': ID}}
    first = client.post('/resolve', **request).json()
    assert Path(first['path']).read_bytes() == b'complete audio'
    assert len(first['lease']) == 32
    assert client.post('/resolve', **request).json()['path'] == first['path']
    # A new resolver process can play by saved link with the home Mac offline.
    offline = host(tmp_path, lambda r: (_ for _ in ()).throw(ConnectionError('Mac offline')))
    assert offline.post('/load', **request).json()['tracks'] == [ROW]
    assert offline.post('/resolve', **request).status_code == 200
    assert len(calls) == 1
    assert not list(tmp_path.glob('download-*'))
    assert client.post('/release', headers=HEADERS, json={'identifier': first['lease']}).status_code == 200
    assert first['lease'] not in json.loads((tmp_path / '.leases.json').read_text())


@pytest.mark.parametrize('change', ['checksum', 'short', 'oversized', 'extension', 'wrong_track', 'redirect'])
def test_host_rejects_bad_transfers_without_publishing_a_file(tmp_path, change):
    headers = packet()
    content = b'complete audio'
    status = 200
    if change == 'checksum':
        headers['x-gene-sha256'] = '0' * 64
    elif change == 'short':
        content = b'partial'
    elif change == 'oversized':
        headers['content-length'] = str(65 * 1024**2)
    elif change == 'extension':
        headers['x-gene-extension'] = '/../outside'
    elif change == 'wrong_track':
        headers['x-gene-metadata'] = base64.b64encode(json.dumps({**ROW, 'identifier': SECOND}).encode()).decode()
    elif change == 'redirect':
        status = 302
        headers['location'] = 'http://127.0.0.1/private'
    client = host(tmp_path, lambda r: httpx.Response(status, headers=headers, content=content))
    assert client.post('/resolve', headers=HEADERS, json={'identifier': ID}).status_code == 502
    assert not list(tmp_path.iterdir())


def test_relay_errors_are_private_and_do_not_fall_back_to_host_youtube(tmp_path):
    extraction = Mock()
    client = TestClient(create_app(TOKEN, extraction, cache_dir=tmp_path,
        relay_url='http://100.64.0.1:2334', relay_token='y' * 40,
        transport=httpx.MockTransport(lambda r: httpx.Response(502, text='private failure'))))
    for route in ('/load', '/resolve'):
        response = client.post(route, headers=HEADERS, json={'identifier': ID})
        assert response.status_code == 502 and 'private failure' not in response.text
    extraction.assert_not_called()


def write_track(folder, limit):
    path = folder / 'audio.webm'
    path.write_bytes(b'x' * 20)
    return path


def test_week_retention_refreshes_on_play_and_expires_after_idle_week(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr('gene.audio_cache.time.time', lambda: now)
    cache = MediaCache(tmp_path, track_bytes=20, ttl=7 * 86400)
    path = cache.get(ID, write_track)
    os.utime(path, (now - 6 * 86400, now - 6 * 86400))
    producer = Mock(side_effect=write_track)
    assert cache.get(ID, producer) == path
    producer.assert_not_called()
    now += 6 * 86400
    cache.clean()
    assert path.exists()
    now += 2 * 86400
    cache.clean()
    assert not path.exists()


def test_lru_eviction_protects_playback_and_persists_leases_across_restart(tmp_path):
    def large(folder, limit):
        path = folder / 'audio.webm'
        path.write_bytes(b'x' * 20000)
        return path
    cache = MediaCache(tmp_path, max_bytes=100000, track_bytes=20000, evict=True)
    first = cache.get(ID, large)
    second = cache.get(SECOND, large)
    os.utime(first, (time.time() - 100, time.time() - 100))
    lease = cache.pin(ID)
    restarted = MediaCache(tmp_path, max_bytes=50000, track_bytes=20000, evict=True)
    restarted.get('cBcDeFgH123', large)
    assert first.exists() and not second.exists()
    restarted.release(lease)
    restarted.max_bytes = 30000
    restarted.get('dBcDeFgH123', large)
    assert not first.exists()


def test_expiry_waits_for_active_playback_and_crashed_lease_eventually_expires(tmp_path, monkeypatch):
    now = time.time()
    cache = MediaCache(tmp_path, track_bytes=20, ttl=60)
    path = cache.get(ID, write_track)
    cache.pin(ID)
    os.utime(path, (now - 61, now - 61))
    cache.clean()
    assert path.exists()
    monkeypatch.setattr('gene.audio_cache.time.time', lambda: now + 7201)
    cache.clean()
    assert not path.exists()


def test_free_space_reserve_and_partial_bytes_prevent_new_download(tmp_path, monkeypatch):
    disk = Mock(free=50)
    monkeypatch.setattr('gene.audio_cache.shutil.disk_usage', lambda p: disk)
    cache = MediaCache(tmp_path, max_bytes=10000, track_bytes=20, reserve_bytes=40)
    producer = Mock()
    with pytest.raises(HTTPException):
        cache.get(ID, producer)
    producer.assert_not_called()
    disk.free = 100000
    partial = tmp_path / 'download-incomplete'
    partial.mkdir()
    (partial / 'part').write_bytes(b'x' * 60)
    cache.max_bytes = 70
    with pytest.raises(HTTPException):
        cache.get(ID, producer)
    producer.assert_not_called()


@pytest.mark.parametrize('url,token', [
    ('http://example.com:2335', 'x' * 40),
    ('http://127.0.0.1:2335', 'x' * 40),
    ('http://100.64.0.1:2335/redirect', 'x' * 40),
    ('http://100.64.0.1:2335', 'short'),
])
def test_home_connection_rejects_public_destinations_and_weak_credentials(tmp_path, url, token):
    with pytest.raises(ValueError):
        create_app(TOKEN, cache_dir=tmp_path, relay_url=url, relay_token=token)


def test_preparation_downloads_once_and_keeps_a_short_handoff_pin(tmp_path):
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=b'complete audio', headers=packet())
    client = host(tmp_path, handler)
    body = {'identifier': ID}
    assert client.post('/prepare', json=body).status_code == 401
    assert client.post('/prepare', headers=HEADERS, json=body).json() == {'ready': True}
    leases = json.loads((tmp_path/'.leases.json').read_text())
    assert all(0 < lease['until']-time.time() <= 180 for lease in leases.values())
    resolved = client.post('/resolve', headers=HEADERS, json=body).json()
    assert Path(resolved['path']).read_bytes() == b'complete audio'
    assert calls == ['/transfer']
