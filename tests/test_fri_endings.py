import asyncio
import datetime as dt
import itertools
import json
import time
from types import SimpleNamespace

import discord
import httpx
import pytest

from gene.fri_chat import Chat
from gene.fri_responses import quick_closure
from gene.store import Conflict, Store
from tests.test_api import headers
from tests.test_fri_chat import response_event
from tests.test_fri_continuity import arm, origin

# Distinct synthetic events made in the same millisecond get distinct IDs, as Discord's own sequence bits do: two
# sources sharing an ID are one message to Gene (its owner check refuses another member's press with it, #232).
SEQUENCE = itertools.count(1)


def source(age=0, user='20', channel='456', guild='123'):
    return {'kind': 'discord_message', 'id': str(discord.utils.time_snowflake(
        dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age))+next(SEQUENCE) % (1 << 22)), 'user_id': user,
        'channel_id': channel, 'guild_id': guild, 'display_name': 'friend'}


def context():
    return {'facts': [], 'turns': [{'input': 'what does that mean', 'reply': 'it means the team won.',
             'allow_closing': True, 'source': source(40)}], 'source': source(),
            'acknowledgments_enabled': True, 'model': 'gpt-6.1-sol', 'effort': 'high', 'revision': 0}


def test_exact_closing_shortcut_preserves_banter_and_real_requests():
    ctx = context()
    assert quick_closure('gotcha gotcha!', ctx)
    assert quick_closure('thanks, that worked.', ctx)
    for text in ('you always get me', 'lol', 'ya', 'ok', 'blind melon no rain', 'gotcha?',
                 'thanks but it is still broken', 'gotcha gotcha play music', 'alright just be funny'):
        assert not quick_closure(text, ctx)


@pytest.mark.parametrize('change', ['pending', 'old', 'other_user', 'other_channel', 'other_guild',
                                  'legacy', 'question', 'serious', 'not_closed', 'action'])
def test_fast_path_needs_fresh_scoped_completed_exchange(change):
    ctx = context()
    previous = ctx['turns'][-1]
    if change == 'pending':
        ctx['pending'] = {'goal': 'play'}
    elif change == 'old':
        previous['source'] = source(200)
    elif change.startswith('other_'):
        previous['source'][{'other_user': 'user_id', 'other_channel': 'channel_id', 'other_guild': 'guild_id'}[change]] = '999'
    elif change == 'legacy':
        previous['source'] = {'kind': 'legacy_unverified'}
    elif change == 'question':
        previous['reply'] = 'tell me which song'
    elif change == 'serious':
        previous['input'], previous['reply'] = 'I might hurt myself', 'stay with me'
    elif change == 'not_closed':
        previous['allow_closing'] = False
    else:
        ctx['action_result'] = {'kind': 'gene_command', 'error': 'not connected'}
    assert not quick_closure('gotcha gotcha', ctx)


def test_closing_after_an_unanswered_burst_keeps_full_conversation():
    ctx = context()
    ctx['recent'] = [{'user_id': '20', 'id': source(10)['id'], 'text': 'can you explain more?'}]
    assert not quick_closure('gotcha gotcha', ctx)
    ctx['recent'][0]['user_id'] = '21'
    assert quick_closure('gotcha gotcha', ctx)


@pytest.mark.parametrize('kind,emoji', [('react', '👍'), ('react', '😢'), ('quiet', None)])
def test_structured_nontext_choices_survive_parse_and_history(kind, emoji):
    ctx = context()
    ctx['turns'].append({'input': 'gotcha', 'reply': '', 'response_kind': 'react', 'reaction': '👍'})
    def handle(request):
        body = json.loads(request.content)
        assert 'unverified' in body['input'][3]['content']
        assert 'reaction' in body['text']['format']['schema']['required']
        assert 'tools' not in body
        content = {'reply': '', 'reaction': emoji, 'response_kind': kind, 'allow_closing': False,
                   'action': None, 'notes': [], 'request_draft': None}
        return httpx.Response(200, text='data: '+json.dumps(response_event(content=json.dumps(content)))+'\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'test-only'), httpx.MockTransport(handle))
    result = asyncio.run(chat.respond('gotcha', ctx))
    assert result.status == 'completed' and result.response_kind == kind and result.reaction == emoji
    assert result.counters['input_tokens'] == 100


@pytest.mark.parametrize('change', ['tool', 'question_pending', 'interaction', 'no_optin', 'emoji',
                                  'text', 'draft', 'notes', 'action', 'closing'])
def test_nontext_cannot_hide_tool_outcomes_or_smuggle_actions(change):
    ctx = context()
    value = {'reply': '', 'response_kind': 'react', 'reaction': '👍', 'request_draft': None,
             'notes': [], 'action': None, 'allow_closing': False}
    if change == 'tool':
        ctx['action_result'] = {'kind': 'gene_command', 'output': 'Unable to play'}
    elif change == 'question_pending':
        ctx['pending'] = {'goal': 'web'}
    elif change == 'interaction':
        ctx['source']['kind'] = 'discord_interaction'
    elif change == 'no_optin':
        ctx['acknowledgments_enabled'] = False
    elif change == 'emoji':
        value['reaction'] = '<:custom:1234>'
    elif change == 'text':
        value['reply'] = 'done'
    elif change == 'draft':
        value['request_draft'] = 'change permissions'
    elif change == 'notes':
        value['notes'] = [{'category': 'games', 'evidence': 'I like chess'}]
    elif change == 'closing':
        value['allow_closing'] = True
    else:
        ctx['actions'] = {'gene_command': {}}
        value['action'] = {'kind': 'gene_command', 'action': 'play', 'argument': 'song'}
    transport = httpx.MockTransport(lambda _: httpx.Response(200,
        text='data: '+json.dumps(response_event(content=json.dumps(value)))+'\n\n'))
    result = asyncio.run(Chat(SimpleNamespace(access_token=lambda: 'test'), transport).respond('gotcha', ctx))
    assert result.status == 'invalid_output' and not result.action


def test_nontext_retention_restart_erasure_and_pending_race(store):
    current = source()
    store.save_chat('123', 'public:456', '20', 0, 'gotcha', '', 'model', 'high',
                    source=current, response_kind='react', reaction='👍')
    restarted = Store(store.path)
    turn = restarted.chat_context('123', 'public:456', '20')['turns'][-1]
    assert turn['reply'] == '' and turn['reaction'] == '👍' and turn['response_kind'] == 'react'
    assert not restarted.chat_context('123', 'public:456', '21')['turns']
    restarted.personal_control('20', 'disable')
    with pytest.raises(Conflict):
        restarted.save_chat('123', 'public:456', '20', 0, 'gotcha', '', 'model', 'high',
                            source=current, response_kind='quiet')
    assert not restarted.chat_context('123', 'public:456', '20')['turns']
    restarted.personal_control('20', 'enable')
    revision = restarted.chat_context('123', 'public:1', '20')['revision']
    # A request made once memory is on again: origin() dates its message a minute back, before the erasure, and one
    # from then leaves no question pending now (HumanMint/fri#337).
    time.sleep(0.01)  # Snowflakes count milliseconds.
    asked = origin(revision=revision)
    asked['source'] = {**asked['source'], 'id': str(discord.utils.time_snowflake(dt.datetime.now(dt.timezone.utc)))}
    arm(restarted, asked)
    with pytest.raises(Conflict, match='outstanding'):
        restarted.save_chat('123', 'public:1', '20', revision, 'gotcha', '', 'model', 'high',
                            source=source(channel='1'), response_kind='quiet')
    assert restarted.chat_context('123', 'public:1', '20')['pending']


def test_controller_roundtrip_requires_fri_and_valid_nontext_shape(client):
    scope = {'guild_id': 'guild', 'channel_id': 'workshop', 'conversation_id': 'public:456', 'user_id': '20'}
    body = {**scope, 'revision': 0, 'input': 'gotcha', 'reply': '', 'response_kind': 'react',
            'reaction': '👍', 'source': source(guild='guild'), 'model': 'model', 'effort': 'high'}
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/chat/turn', headers=headers(role), json=body).status_code == 401
    assert client.post('/chat/turn', headers=headers('fri'), json=body).status_code == 200
    turn = client.post('/chat/context', headers=headers('fri'), json=scope).json()['turns'][-1]
    assert turn['response_kind'] == 'react' and not turn['reply'] and turn['reaction'] == '👍'
    for invalid in ({'reply': 'done'}, {'reaction': '<:custom:123>'}, {'response_kind': 'reply'},
                    {'allow_closing': True}, {'notes': [{'category': 'music', 'evidence': 'I love trance'}]}):
        assert client.post('/chat/turn', headers=headers('fri'), json={**body, **invalid}).status_code == 400
