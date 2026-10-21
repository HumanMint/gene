import asyncio
import copy
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from gene.control_store import Controls
from gene.fri_actions import Actions
from gene.fri_chat import ChatResult
from gene.fri_pending import research_query
from gene.fri_runner import run
from gene.gene_controls import requested
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_actions import harness
from tests.test_fri_research import RECEIPT
from tests.test_fri_runtime import event
from tests.test_gene_controls import control_body, controls_api  # noqa: F401
from tests.test_runtime_controls import runtime


def origin(goal='play', text='can you join the VC and play music', **scope):
    body = control_body(**scope)
    body['source'] = {**body['source'], 'id': str(discord.utils.time_snowflake(
        dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=60)))}
    return {**body, 'request_text': text, 'goal': goal}


def arm(store, body=None, topic=''):
    body = body or origin()
    question = {'goal': body['goal'], 'question': 'what song do you want?' if body['goal'] == 'play' else 'which one?', 'topic': topic}
    store.save_chat(body['guild_id'], body['conversation_id'], body['user_id'], body['revision'],
        body['request_text'], question['question'], 'model', 'high', source=body['source'], clarification=question)
    return store.chat_context(body['guild_id'], body['conversation_id'], body['user_id'])['pending']


def answer(pending, **changes):
    return control_body(request_text='blind melon no rain', argument='blind melon no rain',
                        continuation_id=pending['source']['id'], **changes)


def test_pending_request_survives_restart_and_only_one_answer_can_enqueue(store):
    pending = arm(store)
    restarted = Store(store.path)
    assert restarted.chat_context('123', 'public:1', '20')['pending'] == pending
    controls = Controls(restarted)
    body = answer(pending)
    identifier = controls.enqueue(body)['id']
    assert controls.enqueue(body)['id'] == identifier  # delivery retry is not another command
    assert restarted.chat_context('123', 'public:1', '20')['pending'] is None
    next_answer = copy.deepcopy(body)
    next_answer['source']['id'] = str(int(body['source']['id'])+1)
    with pytest.raises(ValueError):
        controls.enqueue(next_answer)
    claim = controls.poll(['play'])
    assert claim['continuation'] == pending and claim['argument'] == 'blind melon no rain'
    assert controls.poll(['play']) is None
    with pytest.raises(ValueError):
        arm(restarted, {**origin(), 'source': pending['source']})


@pytest.mark.parametrize('change', ['user', 'channel', 'guild', 'private', 'expired', 'clear', 'disable',
                                   'cancel', 'different_action', 'argument', 'same_message', 'unrelated_turn'])
def test_continuations_cannot_cross_scope_expiry_privacy_or_action(store, change):
    pending = arm(store)
    body = answer(pending)
    if change in ('user', 'channel', 'guild'):
        key = change+'_id'
        body[key] = body['source'][key] = '999'
        if change == 'channel':
            body['conversation_id'] = 'public:999'
    elif change == 'private':
        body['source']['guild_id'] = None
        body['conversation_id'] = 'private:1'
    elif change == 'expired':
        with store.connection() as db:
            db.execute('UPDATE chat_pending SET created=?', (time.time()-301,))
    elif change == 'clear':
        store.clear_chats('20')
    elif change == 'disable':
        store.personal_control('20', 'disable')
    elif change == 'cancel':
        body['request_text'] = body['argument'] = 'nah nevermind'
    elif change == 'different_action':
        body['action'], body['argument'] = 'stop', ''
    elif change == 'argument':
        body['argument'] = 'a different song from someone else'
    elif change == 'same_message':
        body['source']['id'] = pending['source']['id']
    elif change == 'unrelated_turn':
        store.save_chat('123', 'public:1', '20', 0, 'anyway how was your day', 'fine', 'model', 'high', source=body['source'])
    with pytest.raises(ValueError):
        Controls(store).enqueue(body)
    assert Controls(store).poll(['play']) is None


@pytest.mark.parametrize('text', ['could I ask you to play music?', 'for example play music',
                                '> play music', 'I play music', 'do not join the VC and play music'])
def test_assistant_question_cannot_create_authority_from_nonrequests(store, text):
    with pytest.raises(ValueError):
        arm(store, origin(text=text))
    assert store.chat_context('123', 'public:1', '20')['pending'] is None


def test_compound_play_request_is_supported_without_enabling_join_or_arbitrary_commands():
    assert requested('<@99> can you join the VC and play music', 'play')
    assert not requested('he asked can you join the VC and play music', 'play')
    assert not requested('can you join the VC', 'play')
    assert not requested('can you join the VC and delete everything', 'eval')


@pytest.mark.parametrize('text,query', [('no rain', 'no rain'), ('<@99> no rain', 'no rain')])
def test_song_title_or_direct_mention_is_not_mistaken_for_a_cancellation(store, text, query):
    pending = arm(store)
    body = answer(pending)
    body.update(request_text=text, argument=query)
    assert Controls(store).enqueue(body)['id']


@pytest.mark.parametrize('changed', ['edited', 'erased'])
def test_pending_research_rechecks_source_before_any_web_export(monkeypatch, tmp_path, store, changed):
    async def scenario():
        pending = arm(store, origin(goal='web', text='how are the bills doing nowadays'), topic='how are the bills doing nowadays')
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.state['web_search_enabled'] = True
        actions = Actions(agent, 20, 1, True)
        actions.pending, actions.message = pending, 'ya'
        actions.scope = {'guild_id': '123', 'channel_id': '1', 'user_id': '20', 'conversation_id': 'public:1'}
        actions.event = event(user=20, channel=1, content='ya')
        old = SimpleNamespace(author=SimpleNamespace(id=20), webhook_id=None, content='edited request')
        actions.event.channel.fetch_message = AsyncMock(return_value=old)
        fri.api = AsyncMock(return_value={'revision': 0, 'pending': pending if changed == 'edited' else None})
        with patch('gene.fri_actions.lookup', new=AsyncMock()) as lookup:
            result = await actions.execute({'kind': 'web_search', 'query': pending['topic']})
            assert 'blocked' in result
            lookup.assert_not_awaited()
        await fri.close()
    asyncio.run(scenario())


def test_public_research_confirmation_preserves_question_not_private_context(store):
    pending = arm(store, origin(goal='web', text='fri how are the bills doing nowadays'),
                  topic='how are the bills doing nowadays')
    assert research_query(pending['topic'], 'ya', pending) == pending['topic']
    assert research_query('private saved detail', 'ya', pending) is None
    assert research_query(pending['topic'], 'never mind', pending) is None
    receipt = {**RECEIPT, 'query': pending['topic']}
    store.save_chat('123', 'public:1', '20', 0, 'ya', 'Earlier check: web_search', 'model', 'high',
                    source=control_body()['source'], web_receipt=receipt)
    assert store.chat_context('123', 'public:1', '20')['web_research'] == [receipt]
    assert store.chat_context('123', 'public:1', '20')['pending'] is None


def test_research_correction_combines_only_explicit_public_excerpts(store):
    pending = arm(store, origin(goal='web', text='how fast is a jaguar'), topic='how fast is a jaguar')
    query = research_query('the car', 'the car', pending)
    assert query == 'how fast is a jaguar; clarification: the car'
    assert research_query('token=private', 'token=private', pending) is None
    store.save_chat('123', 'public:1', '20', 0, 'the car', 'checked', 'model', 'high',
        source=control_body()['source'], web_receipt={**RECEIPT, 'query': query})


def test_real_clarification_to_red_command_keeps_voice_checks_and_receipts(monkeypatch, tmp_path, controls_api):  # noqa: F811
    async def scenario():
        client, controls = controls_api
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        bot, runner, cog, _, member, channel, calls, message = await runtime()
        original = copy.copy(message)
        initial = origin(text='<@99> can you join the VC and play music')
        original.id, original.content = int(initial['source']['id']), initial['request_text']
        original.author = message.author = member
        message.content = 'blind melon no rain'
        channel.fetch_message = AsyncMock(side_effect=lambda mid: original if mid == original.id else message)
        async def api(method, path, body=None, role='fri'):
            if path == '/agent/admit':
                return {'allowed': True}
            response = client.request(method, path, headers=headers(role), json=body)
            assert response.is_success, (path, response.text)
            return response.json()
        fri.api = api
        cog.api = lambda method, path, body=None: api(method, path, body, 'runtime')
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'clarify', 'goal': 'play', 'topic': '', 'question': 'what song?'}),
            ChatResult(status='completed', action={'kind': 'gene_command', 'action': 'play', 'argument': message.content}),
            ChatResult(status='completed', reply='queued it')])
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        async def deliver(_):
            claim = controls.poll(['play'])
            assert claim
            outcome = await runner.execute(cog, claim)
            controls.finish(claim['id'], claim['lease'], outcome)
        try:
            result = await fri.talk(123, 1, 20, initial['request_text'], 'public', event=original)
            assert result.reply == 'what song?' and calls == []
            assert client.app.state.store.chat_context('123', 'public:1', '20')['pending']
            with patch('gene.fri_actions.asyncio.sleep', new=deliver):
                result = await fri.talk(123, 1, 20, message.content, 'public', event=message)
            assert result.reply == 'queued it'
            assert len(calls) == 1 and calls[0][1] == 'blind melon no rain'
            assert 'Track Enqueued' in fri.chat.respond.await_args.args[1]['action_result']['output']
            assert controls.poll(['play']) is None
            assert client.app.state.store.chat_context('123', 'public:1', '20')['pending'] is None
        finally:
            await runner.close()
            await bot.close()
            await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('tamper', ['edited', 'author', 'no_voice'])
def test_runtime_rechecks_original_request_and_member_permissions(store, tamper):
    async def scenario():
        pending = arm(store)
        body = answer(pending)
        controls = Controls(store)
        controls.enqueue(body)
        claim = controls.poll(['play'])
        bot, runner, cog, _, member, channel, calls, message = await runtime()
        message.id, message.content = int(body['source']['id']), body['request_text']
        original = copy.copy(message)
        original.id, original.content = int(pending['source']['id']), pending['request_text']
        if tamper == 'edited':
            original.content = 'never mind'
        if tamper == 'author':
            original.author = SimpleNamespace(id=30)
        member.voice = None if tamper == 'no_voice' else SimpleNamespace(channel=SimpleNamespace(id=8))
        channel.fetch_message = AsyncMock(side_effect=lambda mid: original if mid == original.id else message)
        try:
            assert 'blocked' in await runner.execute(cog, claim)
            assert not calls
        finally:
            await runner.close()
            await bot.close()
    asyncio.run(scenario())


def test_irrelevant_research_can_refine_once_without_exporting_extra_context(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        agent.state['web_search_enabled'] = True
        agent.record = AsyncMock()
        actions = Actions(agent, 20, 1, True)
        actions.message, actions.model, actions.effort = 'how are the bills doing nowadays', 'model', 'high'
        first = {**RECEIPT, 'answer': 'The league schedule is available; no team record established.'}
        second = {**RECEIPT, 'answer': 'Fixture evidence: the specified team has a verified record.', 'sources': ['https://www.buffalobills.com/']}
        replies = [ChatResult(action={'kind': 'web_refine', 'query': ''}), ChatResult(reply='heres how theyre doing', status='completed')]
        fri.chat_once = AsyncMock(side_effect=replies)
        with patch('gene.fri_actions.lookup', new=AsyncMock(side_effect=[(first, {}, 'completed'), (second, {}, 'completed')])) as lookup:
            result, receipts, research = await run(fri, actions, actions.message, {'actions': {'web_search': {}}},
                ChatResult(action={'kind': 'web_search', 'query': actions.message}))
            assert result.reply == 'heres how theyre doing' and len(receipts) == 2
            assert lookup.await_count == 2
            assert lookup.await_args.kwargs['previous']['query'] == actions.message
            assert research['sources'] == ['https://www.buffalobills.com/', 'https://www.python.org/downloads/']
            assert fri.chat_once.await_args.args[1]['actions'] == {}
            assert 'blocked' in await actions.execute({'kind': 'web_refine', 'query': ''})
            assert 'blocked' in await actions.execute({'kind': 'web_search', 'query': actions.message})
            assert lookup.await_count == 2
        await fri.close()
    asyncio.run(scenario())
