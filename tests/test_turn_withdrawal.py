"""fri #129: a member's message edited or deleted after Fri saved their exchange takes that exchange with it."""
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_protocol import compatible, contract
from tests.test_api import headers
from tests.test_fri_endings import source

MARKER = 'synthetic-withdrawn-marker'
SCOPE = {'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51', 'user_id': '20'}


@pytest.fixture
def world(tmp_path):
    settings = Settings(Path(tmp_path/'withdraw.sqlite3'), '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def save(store, text, *, user='20', conversation='public:51', age=0, guild='123', **extra):
    said = extra.pop('source', None) or source(age=age, user=user, channel=conversation.split(':')[1], guild=guild)
    store.save_chat(guild, conversation, user, 0, text, 'okay', 'model', 'high', source=said,
                    provenance=extra.pop('provenance', []), **extra)
    return said['id']


def inputs(store, user='20', conversation='public:51', guild='123'):
    return [turn['input'] for turn in store.chat_context(guild, conversation, user)['turns']]


def withdraw(client, message_id, role='fri', **scope):
    return client.post('/chat/turn/withdraw', headers=headers(role),
                       json={**SCOPE, **scope, 'message_id': message_id})


def test_only_that_members_exchange_holding_the_message_goes(world):
    client, store = world
    taken = save(store, 'explain '+MARKER, age=30)
    save(store, 'what about lunch', age=20)
    other = save(store, 'explain that caption?', user='30', age=10, provenance=[{'user_id': '20', 'revision': 0}])
    save(store, 'same member, other channel', conversation='public:52', age=5)
    assert withdraw(client, other).json() == {'ok': True, 'removed': 0}  # Not this member's message.
    assert withdraw(client, taken, conversation_id='public:52', channel_id='52').json()['removed'] == 0
    assert withdraw(client, taken, user_id='30').json()['removed'] == 0
    assert inputs(store) == ['explain '+MARKER, 'what about lunch']
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 1}
    assert inputs(store) == ['what about lunch']
    assert inputs(store, '30') == ['explain that caption?']  # Another member's reply that drew on it stays (fri #101).
    assert inputs(store, conversation='public:52') == ['same member, other channel']
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 0}  # Again: nothing left, still fine.
    assert store.check_chat('20', 0) == {'ok': True}  # Not a privacy change: their replies in flight still save.


def test_a_message_inside_a_burst_takes_the_burst_exchange(world):
    client, store = world
    first, last = source(age=4, channel='51'), source(age=2, channel='51')
    burst = [{'source': first, 'text': MARKER+' part one'}, {'source': last, 'text': 'part two'}]
    save(store, 'part two', source=last, burst=burst)
    assert MARKER in str(store.chat_context('123', 'public:51', '20')['turns'])
    assert withdraw(client, first['id']).json()['removed'] == 1
    assert inputs(store) == []


def test_it_leaves_the_members_other_conversations_view(world):
    client, store = world
    taken = save(store, 'said where all can read '+MARKER, age=10, audience='server')
    assert [t['input'] for t in store.chat_context('123', 'public:52', '20')['elsewhere']] == [
        'said where all can read '+MARKER]
    withdraw(client, taken)
    assert store.chat_context('123', 'public:52', '20')['elsewhere'] == []


def test_a_question_and_research_it_left_open_go_too(world):
    client, store = world
    taken, kept = source(age=3), source(age=1, user='30')
    with store.connection() as db:
        for said in (taken, kept):
            db.execute('INSERT INTO chat_pending VALUES (?,?,?,?,?,?,?)',
                       ('123', 'public:51', said['user_id'], said['id'], 0, 0, '{}'))
            db.execute('INSERT INTO web_requests VALUES (?,?,?,?,?,?,?)',
                       ('123', 'public:51', said['user_id'], said['id'], 0, 0, MARKER))
    withdraw(client, taken['id'])
    with store.connection() as db:
        assert [r[0] for r in db.execute('SELECT user_id FROM chat_pending')] == ['30']
        assert [r[0] for r in db.execute('SELECT user_id FROM web_requests')] == ['30']


def test_scope_and_credentials_are_those_of_chat(world):
    client, store = world
    lab = save(store, 'lab chat', guild='777', conversation='public:78')
    assert withdraw(client, lab, guild_id='777', channel_id='78', conversation_id='public:78').json()['removed'] == 1
    for scope in ({'channel_id': '99'}, {'guild_id': '456'}, {'guild_id': '777', 'channel_id': '51'},
                  {'guild_id': '777', 'channel_id': '78', 'conversation_id': 'public:51'}):
        assert withdraw(client, '1', **scope).status_code == 403
    for role in ('runtime', 'worker', 'deploy'):
        assert withdraw(client, '1', role=role).status_code == 401
    for scope in ({'message_id': 'abc'}, {'message_id': ''}, {'conversation_id': 'server'},
                  {'mention_grant': 'a'*64}):
        assert client.post('/chat/turn/withdraw', headers=headers('fri'),
                           json={**SCOPE, 'message_id': '1', **scope}).status_code == 422


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_erasure_still_works_around_a_withdrawal(world, erase):
    client, store = world
    taken = save(store, 'explain '+MARKER, age=10)
    save(store, 'what about lunch', age=5)
    withdraw(client, taken)
    if erase == 'disable':
        store.personal_control('20', 'disable')
    else:
        getattr(store, erase)('20')
    assert inputs(store) == []
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 0}
    assert store.chat_context('123', 'public:51', '20')['revision'] == 1


def test_nothing_it_logs_holds_the_words(world, caplog):
    client, store = world
    caplog.set_level(logging.DEBUG)
    withdraw(client, save(store, 'explain '+MARKER))
    assert MARKER not in caplog.text


def test_the_contract_offers_it_as_an_optional_capability():
    advertised = contract()
    assert 'turn-withdrawal' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != 'turn-withdrawal']})
