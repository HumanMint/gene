"""Requested artwork exercises download, real decoding, selection and reply boundaries offline."""

import asyncio
import datetime as dt
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import httpx
import pytest
from PIL import Image, PngImagePlugin

from gene.fri_chat import ChatResult
from gene.fri_image_share import (
    API,
    PreparedImage,
    cdn_url,
    normalize,
    read,
    send_reply,
)
from tests.test_fri_social import api, source  # noqa: F401

QUERY = 'share a random sfw image featuring hatsune miku from danbooru'
DETAILED_QUERY = ("let's try another one\n\n"
    'to represent a theoretical japanese word for gaming, can you share a link to a random sfw '
    'image featuring hatsune miku off of danbooru but with relevant tags and context that would '
    'be cool to "represent" the japanese word of gaming and be framed as a theoretical '
    '"miku word of the day"')
CDN = 'https://cdn.donmai.us/original/ab/cd/'+'a'*32+'.png'
LINK_QUERY = ('share a random sfw image featuring hatsune miku from danbooru, with a relaxed '
    'gaming theme, blue lighting, a controller in her hands and a visible screen in the '
    'background. for inspiration use this original post: a good reference: '
    'https://danbooru.donmai.us/posts/123')


def pixels(color='red', fmt='PNG', **kwargs):
    out = io.BytesIO()
    Image.new('RGB', (32, 24), color).save(out, fmt, **kwargs)
    return out.getvalue()


def post(number=1, **changes):
    return {'id': number, 'rating': 'g', 'is_deleted': False, 'is_banned': False,
            'is_flagged': False, 'is_pending': False, 'file_size': 100,
            'image_width': 32, 'image_height': 24, 'file_url': CDN,
            'tag_string_artist': 'some_artist', **changes}


def provider(posts, *, body=None, status=200, headers=None):
    calls = []
    def handle(request):
        calls.append(request)
        assert not set(request.headers) & {'authorization', 'cookie'}
        if request.url.host == 'safebooru.donmai.us':
            assert request.url.params['tags'] == 'hatsune_miku rating:g order:random'
            assert request.url.params['limit'] == '6'
            return httpx.Response(200, json=posts, headers={'set-cookie': 'secret=do-not-forward'})
        assert str(request.url) == CDN
        return httpx.Response(status, content=body or pixels(), headers=headers or {'content-type': 'image/png'})
    return httpx.MockTransport(handle), calls


@pytest.mark.parametrize('url', ['https://cdn.donmai.us.evil.example/original/a.png',
    'https://user:pw@cdn.donmai.us/original/a.png', CDN+'?redirect=http://127.0.0.1',
    CDN+'#secret', 'http'+CDN[5:], 'https://127.0.0.1/a.png', 'file:///tmp/test',
    'https://cdn.donmai.us:444/original/a.png', CDN.replace('.png', '.svg')])
def test_cdn_scope_rejects_redirectors_local_addresses_and_other_files(url):
    assert not cdn_url(url)


def test_decode_strips_metadata_and_rejects_animation_corrupt_mime_and_bombs():
    meta = PngImagePlugin.PngInfo()
    meta.add_text('private', 'hidden caption')
    normalized, _, _ = normalize(pixels(pnginfo=meta), 'image/png')
    with Image.open(io.BytesIO(normalized)) as image:
        assert 'private' not in image.info
    invalid = [(b'<svg/>', 'image/png'), (pixels(), 'image/jpeg'), (pixels()[:35], 'image/png')]
    out = io.BytesIO()
    Image.new('RGB', (8, 8), 'red').save(out, 'PNG', save_all=True,
        append_images=[Image.new('RGB', (8, 8), 'blue')], duration=100, loop=0)
    invalid.append((out.getvalue(), 'image/png'))
    for body, mime in invalid:
        with pytest.raises((ValueError, OSError)):
            normalize(body, mime)


@pytest.mark.parametrize('mode', ['redirect', 'html', 'length', 'negative', 'chunked', 'encoding'])
def test_stream_rejects_untrusted_response_shapes(mode):
    async def scenario():
        headers, body, status = {'content-type': 'image/png'}, pixels(), 200
        if mode == 'redirect':
            status, headers['location'] = 302, 'http://169.254.169.254/token'
        elif mode == 'html':
            headers['content-type'] = 'text/html'
        elif mode == 'length':
            headers['content-length'] = 'bogus'
        elif mode == 'negative':
            headers['content-length'] = '-1'
        elif mode == 'chunked':
            body = b'x'*101
        else:
            headers['content-encoding'] = 'br'
        requests = []
        def handle(request):
            requests.append(request)
            return httpx.Response(status, content=body, headers=headers)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            with pytest.raises((ValueError, httpx.HTTPError)):
                await read(client, CDN, 100, {'image/png'})
        assert len(requests) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['denied', 'ambiguous', 'no_attach', 'changed'])
def test_delivery_failure_is_truthful_and_never_retries_ambiguous_upload(mode):
    async def scenario():
        media = PreparedImage(pixels(), 'artwork.png', API+'/posts/1', 'some_artist',
                              SimpleNamespace(current=AsyncMock(return_value=mode != 'changed')))
        event = SimpleNamespace(guild=None, channel=SimpleNamespace(), reply=AsyncMock())
        if mode in ('denied', 'ambiguous'):
            error = discord.HTTPException(SimpleNamespace(status=403 if mode == 'denied' else 500, reason='failed'), 'failed')
            event.reply.side_effect = [error, None]
        if mode == 'no_attach':
            event.guild = SimpleNamespace(me=object())
            event.channel.permissions_for = lambda _: SimpleNamespace(attach_files=False)
        result = ChatResult(reply='here it is', media=media)
        if mode == 'ambiguous':
            with pytest.raises(discord.HTTPException):
                await send_reply(event, result)
            assert event.reply.await_count == 1
        else:
            await send_reply(event, result)
            if mode == 'changed':
                event.reply.assert_not_awaited()
            else:
                assert 'file' not in event.reply.await_args.kwargs
                assert media.page in event.reply.await_args.args[0]
        assert result.media is None and not media.body
    asyncio.run(scenario())


def test_image_brief_bounds_and_ordinary_web_limits_stay_separate():
    from gene.fri_image_request import MAX_REQUEST, image_request, receipt_query
    from gene.fri_research import ResearchReceipt
    from gene.web_policy import public_query
    assert image_request('', DETAILED_QUERY) == DETAILED_QUERY
    assert image_request('private saved context', DETAILED_QUERY) == ''
    assert image_request('', 'a'*(MAX_REQUEST+1)) == ''
    assert image_request(None, DETAILED_QUERY) == ''
    assert not public_query(DETAILED_QUERY, DETAILED_QUERY)
    assert not public_query('hello\nworld', 'hello\nworld')
    label = receipt_query(DETAILED_QUERY)
    assert public_query(label, DETAILED_QUERY)
    ResearchReceipt(query=label, answer='verified image', sources=[API+'/posts/1'],
                    checked_at=dt.datetime.now(dt.timezone.utc).isoformat())
