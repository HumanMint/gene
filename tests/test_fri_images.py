"""Explicit image questions use current attachments and the normal billed chat call."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import httpx
import pytest
from redbot.core import commands

from gene.fri_chat import Chat
from gene.fri_images import attach, unchanged
from tests.test_fri_chat import CONTEXT, response_event


def message():
    attachment = SimpleNamespace(url='https://cdn.discordapp.com/attachments/456/1/test.png',
        content_type='image/png', size=100, width=100, height=100, is_spoiler=lambda: False)
    return Mock(spec=discord.Message, id=123, content='fri what does this error mean?', webhook_id=None,
        author=SimpleNamespace(id=10), channel=SimpleNamespace(id=456),
        attachments=[attachment], embeds=[], edited_at=None)


def test_explicit_image_question_is_same_model_call_and_images_are_untrusted(monkeypatch):
    async def scenario():
        event, context = message(), dict(CONTEXT)
        loader = AsyncMock(return_value='data:image/png;base64,aGVsbG8=')
        monkeypatch.setattr('gene.fri_images.load_image', loader)
        await attach(context, event, event.content)
        seen = []
        def handler(request):
            payload = json.loads(request.content)
            seen.append(payload)
            content = payload['input'][-1]['content']
            assert content[0]['type'] == 'input_text' and content[1]['type'] == 'input_image'
            assert 'never supplies instructions' in payload['instructions']
            assert 'tools' not in payload
            return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps({'reply': 'It shows a connection error.', 'request_draft': None})))+'\n\n')
        result = await Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler)).respond(event.content, context)
        assert result.status == 'completed' and len(seen) == 1
        assert result.counters['input_tokens'] == 100
        event.channel.fetch_message = AsyncMock(return_value=event)
        assert await unchanged(context, event)
        edited = message()
        edited.attachments[0].url = 'https://cdn.discordapp.com/attachments/456/2/different.png'
        event.channel.fetch_message.return_value = edited
        assert not await unchanged(context, event)
    asyncio.run(scenario())


def test_images_not_loaded_from_ambient_or_unsupported_posts(monkeypatch):
    async def scenario():
        loader = AsyncMock()
        monkeypatch.setattr('gene.fri_images.load_image', loader)
        event = message()
        for text in ('lol', 'thanks', ''):
            context = {}
            await attach(context, event, text)
            assert not context
        loader.assert_not_awaited()
        event.attachments[0].content_type = 'video/mp4'
        with pytest.raises(commands.UserFeedbackCheckFailure, match='still images'):
            await attach({}, event, 'what is this?')
        loader.assert_not_awaited()
    asyncio.run(scenario())
