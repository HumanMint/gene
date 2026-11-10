import asyncio
import copy
import datetime as dt
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from gene.control_store import Controls
from gene.fri_pending import research_query
from gene.fri_research import start as start_research
from gene.gene_controls import requested
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_research import RECEIPT
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
    # Only member-authored authorization fields travel with the command, never the question.
    assert claim['continuation'] == {k: pending[k] for k in ('goal', 'source', 'request_text', 'revision')}
    assert claim['argument'] == 'blind melon no rain'
    assert controls.poll(['play']) is None
    with pytest.raises(ValueError):
        arm(restarted, {**origin(), 'source': pending['source']})


@pytest.mark.parametrize('finished', [False, True])
def test_consumed_origin_cannot_rearm_after_restart_with_memory_disabled(store, finished):
    store.personal_control('20', 'disable')
    initial = origin(revision=1)
    pending = arm(store, initial)
    controls = Controls(store)
    body = answer(pending, revision=1)
    identifier = controls.enqueue(body)['id']
    claim = controls.poll(['play'])
    controls.start(identifier, claim['lease'])
    if finished:
        controls.finish(identifier, claim['lease'], {'output': 'Track enqueued'})
    restarted = Store(store.path)
    context = restarted.chat_context('123', 'public:1', '20')
    assert context['turns'] == [] and context['pending'] is None
    assert not context['personal_memory']['enabled']
    with patch('time.time', return_value=time.time()+4):
        with pytest.raises(ValueError, match='already handled'):
            arm(restarted, initial)
        second = copy.deepcopy(body)
        second['source']['id'] = str(int(body['source']['id'])+1)
        with pytest.raises(ValueError):
            Controls(restarted).enqueue(second)
    assert Controls(restarted).poll(['play']) is None
    with restarted.connection() as db:
        assert db.execute('SELECT count(*) FROM gene_controls').fetchone()[0] == 1
        fence = dict(db.execute('SELECT * FROM gene_control_origins').fetchone())
        assert set(fence) == {'id', 'created'} and len(fence['id']) == 64
        assert pending['source']['id'] not in str(fence)
    with patch('time.time', return_value=time.time()+86401):
        assert Controls(restarted).poll(['play']) is None
        with restarted.connection() as db:
            assert db.execute('SELECT count(*) FROM gene_control_origins').fetchone()[0] == 0


@pytest.mark.parametrize('erased', [False, True])
def test_consumed_origin_cannot_replay_as_a_direct_command_even_after_erasure(store, erased):
    initial = origin()
    pending = arm(store, initial)
    controls = Controls(store)
    body = answer(pending)
    identifier = controls.enqueue(body)['id']
    claim = controls.poll(['play'])
    controls.start(identifier, claim['lease'])
    controls.finish(identifier, claim['lease'], {'output': 'Track enqueued'})
    if erased:
        store.personal_control('20', 'disable')
    restarted = Store(store.path)
    revision = restarted.chat_context('123', 'public:1', '20')['revision']
    replay = {key: value for key, value in initial.items() if key != 'goal'}
    replay['revision'] = revision
    with patch('time.time', return_value=time.time()+4):
        with pytest.raises(ValueError, match='already handled'):
            Controls(restarted).enqueue(replay)
        with pytest.raises(ValueError):
            arm(restarted, {**initial, 'revision': revision})
    assert Controls(restarted).poll(['play']) is None
    with restarted.connection() as db:
        assert db.execute('SELECT count(*) FROM gene_controls').fetchone()[0] == 1


def test_cancellation_requires_fresh_scoped_event_and_allows_a_new_request(controls_api):  # noqa: F811
    client, controls = controls_api
    pending = arm(controls.store)
    body = {key: control_body()[key] for key in ('guild_id', 'user_id', 'channel_id', 'conversation_id', 'source')}
    body['input'] = 'never mind'
    path = '/chat/pending/cancel'
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post(path, headers=headers(role), json=body).status_code == 401
    for changed in (
        {**body, 'input': 'no rain'},
        {**body, 'source': {**body['source'], 'user_id': '30'}},
        {**body, 'source': {**body['source'], 'kind': 'discord_interaction'}},
        {**body, 'source': {**body['source'], 'id': str(discord.utils.time_snowflake(
            dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=301)))}},
    ):
        assert client.post(path, headers=headers('fri'), json=changed).status_code == 400
    older = {**body, 'source': {**body['source'], 'id': str(int(pending['source']['id'])-1)}}
    other = {**body, 'user_id': '30', 'source': {**body['source'], 'user_id': '30'}}
    elsewhere = {**body, 'channel_id': '2', 'conversation_id': 'public:2',
                 'source': {**body['source'], 'channel_id': '2'}}
    for changed in (older, other, elsewhere):
        assert client.post(path, headers=headers('fri'), json=changed).json() == {'cancelled': False}
    assert controls.store.chat_context('123', 'public:1', '20')['pending'] == pending
    assert client.post(path, headers=headers('fri'), json=body).json() == {'cancelled': True}
    assert client.post(path, headers=headers('fri'), json=body).json() == {'cancelled': False}
    new = origin()
    new['source']['id'] = str(int(body['source']['id'])+1)
    assert arm(controls.store, new)['source']['id'] == new['source']['id']


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


@pytest.mark.parametrize('text,query', [('no rain', 'no rain'), ('<@99> no rain', 'no rain'), ('no rain <@99>', 'no rain'), ('no <@!99> rain', 'no rain')])
def test_song_title_or_direct_mention_is_not_mistaken_for_a_cancellation(store, text, query):
    pending = arm(store)
    body = answer(pending)
    body.update(request_text=text, argument=query, addressed_bot_id='99')
    assert Controls(store).enqueue(body)['id']


def test_public_research_confirmation_preserves_question_not_private_context(store):
    pending = arm(store, origin(goal='web', text='fri how are the bills doing nowadays'),
                  topic='how are the bills doing nowadays')
    assert research_query(pending['topic'], 'ya', pending) == pending['topic']
    assert research_query('private saved detail', 'ya', pending) is None
    assert research_query(pending['topic'], 'never mind', pending) is None
    receipt = {**RECEIPT, 'query': pending['topic']}
    source = control_body()['source']
    start_research(store, '123', 'public:1', '20', 0, source, 'ya', pending['topic'])
    store.save_chat('123', 'public:1', '20', 0, 'ya', 'Earlier check: web_search', 'model', 'high',
                    source=source, web_receipt=receipt)
    assert store.chat_context('123', 'public:1', '20')['web_research'] == [receipt]
    assert store.chat_context('123', 'public:1', '20')['pending'] is None


def test_research_correction_combines_only_explicit_public_excerpts(store):
    pending = arm(store, origin(goal='web', text='how fast is a jaguar'), topic='how fast is a jaguar')
    query = research_query('the car', 'the car', pending)
    assert query == 'how fast is a jaguar; clarification: the car'
    assert research_query('token=private', 'token=private', pending) is None
    source = control_body()['source']
    start_research(store, '123', 'public:1', '20', 0, source, 'the car', query)
    store.save_chat('123', 'public:1', '20', 0, 'the car', 'checked', 'model', 'high',
        source=source, web_receipt={**RECEIPT, 'query': query})


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


@pytest.mark.parametrize('removed', ['expired', 'command'])
def test_completed_research_survives_pending_expiry_or_a_later_command(store, removed):
    original = origin(goal='web', text='what is the newest Porter Robinson song')
    original['source']['id'] = str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=240)))
    pending = arm(store, original, topic=original['request_text'])
    body = control_body(request_text='yes. play it')
    source = body['source']
    start_research(store, '123', 'public:1', '20', 0, source, body['request_text'], pending['topic'])
    if removed == 'command':
        Controls(store).enqueue(body)
    with patch('time.time', return_value=time.time()+61):
        assert store.chat_context('123', 'public:1', '20')['pending'] is None
        receipt = {**RECEIPT, 'query': pending['topic']}
        store.save_chat('123', 'public:1', '20', 0, body['request_text'], 'checked', 'model', 'high',
                        source=source, web_receipt=receipt)
        assert store.chat_context('123', 'public:1', '20')['web_research'] == [receipt]
        with store.connection() as db:
            assert not db.execute('SELECT 1 FROM web_requests').fetchone()


@pytest.mark.parametrize('change', ['user', 'channel', 'source', 'query', 'expired', 'privacy'])
def test_research_receipt_permit_is_scoped_bounded_and_cannot_authorize_commands(store, change):
    pending = arm(store, origin(goal='web', text='how are the bills doing nowadays'), topic='how are the bills doing nowadays')
    source = control_body()['source']
    start_research(store, '123', 'public:1', '20', 0, source, 'ya', pending['topic'])
    with pytest.raises(ValueError):
        Controls(store).enqueue(answer(pending))  # Web evidence is never music authority.
    user, channel, query = '20', 'public:1', pending['topic']
    if change == 'user':
        user = source['user_id'] = '30'
    elif change == 'channel':
        channel, source['channel_id'] = 'public:2', '2'
    elif change == 'source':
        source['id'] = str(int(source['id'])+1)
    elif change == 'query':
        query = 'a private fact absent from the request'
    elif change == 'expired':
        with store.connection() as db:
            db.execute('UPDATE web_requests SET created=?', (time.time()-601,))
    else:
        store.personal_control('20', 'disable')
    with pytest.raises(ValueError):
        store.save_chat('123', channel, user, 0, 'ya', 'checked', 'model', 'high', source=source,
                        web_receipt={**RECEIPT, 'query': query})


def test_only_fri_can_start_a_pending_public_lookup(controls_api):  # noqa: F811
    client, controls = controls_api
    pending = arm(controls.store, origin(goal='web', text='how are the bills doing nowadays'), topic='how are the bills doing nowadays')
    source = control_body()['source']
    body = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1',
            'source': source, 'revision': 0, 'input': 'ya', 'query': pending['topic']}
    for role in ('worker', 'runtime', 'deploy'):
        assert client.post('/chat/research/start', headers=headers(role), json=body).status_code == 401
    assert client.post('/chat/research/start', headers=headers('fri'), json=body).status_code == 200
    assert client.post('/chat/research/start', headers=headers('fri'), json={**body, 'query': 'private saved notes'}).status_code == 400
