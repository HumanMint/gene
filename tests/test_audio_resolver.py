from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from gene.audio_resolver import canonical_query, create_app, public_stream


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
