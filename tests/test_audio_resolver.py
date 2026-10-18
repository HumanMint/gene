from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from gene.audio_resolver import MediaCache, canonical_query, create_app, public_stream


@pytest.fixture(autouse=True)
def isolated_media_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('GENE_AUDIO_CACHE', str(tmp_path / 'media'))


def test_prepare_publishes_a_complete_local_file_and_reuses_it_without_extraction(tmp_path):
    extractor = Mock(return_value={'id': 'aBcDeFgH123', 'url': 'https://rr1.googlevideo.com/audio',
                                   'duration': 174, 'protocol': 'https'})
    def download(result, folder, maximum):
        path = folder / 'audio.webm'
        path.write_bytes(b'complete audio')
        return path
    downloader = Mock(side_effect=download)
    client = TestClient(create_app('x' * 40, extractor, cache_dir=tmp_path, downloader=downloader))
    request = {'headers': {'Authorization': 'Bearer ' + 'x' * 40}, 'json': {'identifier': 'aBcDeFgH123'}}
    first = client.post('/resolve', **request)
    assert first.status_code == 200
    assert first.json()['path'] == str(tmp_path / 'aBcDeFgH123.webm')
    assert len(first.json()['lease']) == 32
    assert client.post('/resolve', **request).json()['path'] == first.json()['path']
    extractor.assert_called_once()
    downloader.assert_called_once()


def test_partial_or_oversized_downloads_are_removed_and_can_be_retried(tmp_path):
    cache = MediaCache(tmp_path, max_bytes=100, track_bytes=20)
    def broken(folder, limit):
        (folder / 'audio.webm.part').write_bytes(b'incomplete')
        raise RuntimeError('failed transfer')
    with pytest.raises(RuntimeError):
        cache.get('aBcDeFgH123', broken)
    assert not list(tmp_path.iterdir())
    def too_big(folder, limit):
        path = folder / 'audio.webm'
        path.write_bytes(b'x' * 21)
        return path
    with pytest.raises(ValueError):
        cache.get('aBcDeFgH123', too_big)
    assert not list(tmp_path.iterdir())
    def complete(folder, limit):
        path = folder / 'audio.webm'
        path.write_bytes(b'complete')
        return path
    assert cache.get('aBcDeFgH123', complete).read_bytes() == b'complete'


def test_cache_deduplicates_concurrent_downloads_and_protects_recent_tracks(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from fastapi import HTTPException
    cache = MediaCache(tmp_path, max_bytes=30, track_bytes=20)
    def complete(folder, limit):
        path = folder / 'audio.webm'
        path.write_bytes(b'x' * 20)
        return path
    producer = Mock(side_effect=complete)
    with ThreadPoolExecutor(max_workers=2) as pool:
        paths = list(pool.map(lambda _: cache.get('aBcDeFgH123', producer), range(2)))
    assert paths[0] == paths[1]
    producer.assert_called_once()
    with pytest.raises(HTTPException, match='cache is full'):
        cache.get('bBcDeFgH123', producer)
    assert paths[0].exists()


def test_cache_expires_old_files_and_rejects_paths_outside_its_directory(tmp_path):
    import os
    import time
    cache = MediaCache(tmp_path / 'cache', max_bytes=30, track_bytes=20, ttl=60)
    stale = cache.folder / 'zBcDeFgH123.webm'
    stale.write_bytes(b'x' * 20)
    os.utime(stale, (time.time() - 61, time.time() - 61))
    outside = tmp_path / 'outside.webm'
    outside.write_bytes(b'audio')
    with pytest.raises(ValueError):
        cache.get('aBcDeFgH123', lambda folder, limit: outside)
    assert not stale.exists()
    assert outside.exists()


def test_failed_transfer_retries_once_with_fresh_metadata_and_no_partial_file(tmp_path):
    extractor = Mock(return_value={'id': 'aBcDeFgH123', 'url': 'https://rr1.googlevideo.com/audio',
                                   'duration': 174, 'protocol': 'https'})
    attempts = []
    def download(result, folder, maximum):
        assert not list(folder.iterdir())
        attempts.append(1)
        path = folder / 'audio.webm'
        path.write_bytes(b'audio')
        if len(attempts) == 1:
            raise ConnectionError('temporary transfer failure')
        return path
    client = TestClient(create_app('x' * 40, extractor, cache_dir=tmp_path, downloader=download))
    response = client.post('/resolve', headers={'Authorization': 'Bearer ' + 'x' * 40},
                           json={'identifier': 'aBcDeFgH123'})
    assert response.status_code == 200
    assert extractor.call_count == 2
    assert (tmp_path / 'aBcDeFgH123.webm').read_bytes() == b'audio'


def test_recent_interrupted_downloads_count_toward_capacity(tmp_path):
    from fastapi import HTTPException
    cache = MediaCache(tmp_path, max_bytes=30, track_bytes=20)
    partial = tmp_path / 'download-interrupted'
    partial.mkdir()
    (partial / 'audio.webm.part').write_bytes(b'x' * 20)
    producer = Mock()
    with pytest.raises(HTTPException, match='cache is full'):
        cache.get('aBcDeFgH123', producer)
    producer.assert_not_called()


@pytest.mark.parametrize('query', ['http://127.0.0.1/admin', 'file:///etc/passwd',
    'https://youtube.com.evil.example/watch?v=aBcDeFgH123', 'https://www.youtube.com:8080/watch?v=aBcDeFgH123',
    'https://evil.example/https://youtube.com/watch?v=aBcDeFgH123', 'ytsearch: '])
def test_non_youtube_inputs_are_rejected(query):
    with pytest.raises(ValueError):
        canonical_query(query)


def test_search_is_bounded_and_video_links_are_canonicalized():
    assert canonical_query('ytsearch:example music') == ('ytsearch5:example music', True)
    assert canonical_query('https://youtu.be/aBcDeFgH123?t=12') == (
        'https://www.youtube.com/watch?v=aBcDeFgH123', False)
    assert canonical_query('https://www.youtube.com/shorts/aBcDeFgH123')[0].endswith('v=aBcDeFgH123')


def test_resolver_requires_auth_and_never_uses_personal_cookies():
    extractor = Mock(return_value={'_type': 'playlist', 'title': 'Search', 'entries': [
        {'id': 'aBcDeFgH123', 'title': 'Track', 'duration': 120}, None]})
    client = TestClient(create_app('x' * 40, extractor))
    assert client.post('/load', json={'identifier': 'ytsearch:music'}).status_code == 401
    extractor.assert_not_called()
    result = client.post('/load', headers={'Authorization': 'Bearer ' + 'x' * 40},
                         json={'identifier': 'ytsearch:music'})
    assert result.status_code == 200
    assert result.json()['tracks'][0]['length'] == 120000
    options = extractor.call_args.args[1]
    assert not options.get('cookiefile') and not options.get('cookiesfrombrowser')
    assert options['playlistend'] == 100


@pytest.mark.parametrize('url', ['http://127.0.0.1/private', 'https://googlevideo.com.evil.example/a',
    'https://rr1.googlevideo.com:9000/a', 'https://user:password@rr1.googlevideo.com/a'])
def test_resolved_media_cannot_target_private_or_arbitrary_hosts(url):
    assert not public_stream(url)
    client = TestClient(create_app('x' * 40, Mock(return_value={'url': url})))
    result = client.post('/resolve', headers={'Authorization': 'Bearer ' + 'x' * 40},
                         json={'identifier': 'aBcDeFgH123'})
    assert result.status_code == 422


def test_extractor_failures_do_not_leak_tracebacks_or_temporary_urls():
    client = TestClient(create_app('x' * 40, Mock(side_effect=RuntimeError('private extractor diagnostic'))))
    result = client.post('/resolve', headers={'Authorization': 'Bearer ' + 'x' * 40},
                         json={'identifier': 'aBcDeFgH123'})
    assert result.status_code == 502
    assert 'private extractor diagnostic' not in result.text
