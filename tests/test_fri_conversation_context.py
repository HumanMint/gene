import asyncio
import copy
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import httpx
import pytest

from gene.control_store import Controls
from gene.fri_chat import Chat
from gene.fri_conversation import Bursts, burst_continuation, checked_burst
from gene.fri_responses import quick_closure
from tests.test_fri_chat import CONTEXT, response_event
from tests.test_fri_endings import context as closing_context
from tests.test_gene_controls import control_body, controls_api  # noqa: F401
from tests.test_runtime_controls import runtime


def pair(**changes):
    body = control_body(request_text='blind melon no rain', argument='blind melon no rain', **changes)
    original = {'source': {**body['source'], 'id': str(int(body['source']['id'])-(1000 << 22))}, 'text': 'fri play'}
    return {**body, 'burst_origin': original, 'continuation_id': original['source']['id']}


@pytest.mark.parametrize('change', ['actor', 'channel', 'guild', 'old', 'gap', 'same', 'cancel', 'argument', 'example', 'complete'])
def test_split_permission_rejects_scope_age_cancellation_and_invented_arguments(store, change):
    body = pair()
    original = body['burst_origin']
    if change in ('actor', 'channel', 'guild'):
        original['source'][{'actor': 'user_id', 'channel': 'channel_id', 'guild': 'guild_id'}[change]] = '777'
    elif change == 'old':
        body['source']['id'] = str(int(body['source']['id'])-(301000 << 22))
        original['source']['id'] = str(int(body['source']['id'])-(1000 << 22))
    elif change == 'gap':
        original['source']['id'] = str(int(body['source']['id'])-(6000 << 22))
    elif change == 'same':
        original['source']['id'] = body['source']['id']
    elif change == 'cancel':
        body['request_text'] = body['argument'] = 'never mind'
    elif change == 'argument':
        body['argument'] = 'someone else song'
    elif change == 'example':
        original['text'] = 'for example fri play'
    elif change == 'complete':
        original['text'] = 'fri play a different song'
    assert burst_continuation(original, body['source'], body['request_text'], 'play', body['argument'], 0) is None
    with pytest.raises(ValueError):
        Controls(store).enqueue(body)
    assert Controls(store).poll(['play']) is None


@pytest.mark.parametrize('tamper', ['edited', 'deleted', 'author', 'no_voice'])
def test_runtime_rechecks_split_origin_before_start(store, tamper):
    async def scenario():
        body = pair()
        controls = Controls(store)
        controls.enqueue(body)
        claim = controls.poll(['play'])
        bot, runner, cog, _, member, channel, calls, message = await runtime()
        message.id, message.content = int(body['source']['id']), body['request_text']
        message.author = member
        original = copy.copy(message)
        original.id, original.content = int(body['burst_origin']['source']['id']), 'fri play'
        if tamper == 'edited':
            original.content = 'never mind'
        if tamper == 'author':
            original.author = SimpleNamespace(id=30)
        member.voice = None if tamper == 'no_voice' else SimpleNamespace(channel=SimpleNamespace(id=8))
        async def fetch(mid):
            if mid == original.id and tamper == 'deleted':
                raise discord.NotFound(SimpleNamespace(status=404, reason='Not Found'), 'gone')
            return original if mid == original.id else message
        channel.fetch_message = AsyncMock(side_effect=fetch)
        try:
            if tamper == 'deleted':
                with pytest.raises(discord.NotFound):
                    await runner.execute(cog, claim)
            else:
                assert 'blocked' in await runner.execute(cog, claim)
            assert not calls
            if tamper != 'no_voice':
                cog.api.assert_not_awaited()
        finally:
            await runner.close()
            await bot.close()
    asyncio.run(scenario())


def test_burst_retention_is_scoped_ephemeral_and_bounded():
    bursts = Bursts()
    def msg(mid, user=20, channel=1):
        return SimpleNamespace(id=mid, content='hello', author=SimpleNamespace(id=user),
                               channel=SimpleNamespace(id=channel))
    bursts.add(msg(1))
    assert bursts.has(1, 20) and not bursts.has(1, 30) and not bursts.has(2, 20)
    bursts.forget(20)
    assert not bursts.has(1, 20)
    for index in range(5):
        bursts.add(msg(index))
    with pytest.raises(ValueError):
        bursts.add(msg(6))
    assert not bursts.has(1, 20)
    bursts.add(msg(7))
    with patch('gene.fri_conversation.time.monotonic', return_value=time.monotonic()+6):
        assert not bursts.has(1, 20)


def test_conversation_model_receives_source_linked_burst_and_reply_without_misattribution():
    async def scenario():
        body = pair()
        rows = checked_burst([body['burst_origin'], {'source': body['source'], 'text': body['request_text']}],
                             body['source'], body['request_text'])
        target = [{'source': {**body['source'], 'user_id': '30'}, 'text': 'a different member recommendation'}]
        captured = []
        async def transport(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, text='data: '+json.dumps(response_event())+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'fictional'), httpx.MockTransport(transport))
        await chat.respond(body['request_text'], {**CONTEXT, 'message_burst': rows, 'reply_target': target})
        supplied = json.loads(captured[0]['input'][-1]['content'])
        assert supplied['reply_target'] == target and supplied['message_burst'] == rows
        assert supplied['message'] == body['request_text']
        assert 'possibly by another person' in captured[0]['instructions']
    asyncio.run(scenario())


def test_new_reply_target_or_unanswered_burst_never_uses_old_exchange_closing_shortcut():
    context = closing_context()
    assert quick_closure('gotcha', context)
    assert not quick_closure('gotcha', {**context, 'reply_target': [{'text': 'a new question?'}]})
    assert not quick_closure('gotcha', {**context, 'message_burst': [{'text': 'a new question?'}, {'text': 'gotcha'}]})
