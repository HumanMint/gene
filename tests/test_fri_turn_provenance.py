"""Saved replies remember whose messages shaped them, so erasure and privacy changes reach them."""

import sqlite3

import pytest

from gene.store import Conflict, Store
from tests.test_api import headers
from tests.test_fri_endings import source

ASH = {'user_id': '30', 'revision': 0}


def save(store, user='20', conversation='public:1', reply='ash said denver', provenance=(), revision=0):
    return store.save_chat('123', conversation, user, revision, 'where is ash moving', reply, 'model', 'high',
                           source=source(user=user, channel=conversation.split(':')[1]), provenance=provenance)


def replies(store, user='20', conversation='public:1'):
    return [turn['reply'] for turn in store.chat_context('123', conversation, user)['turns']]


def test_contract_advertises_turn_provenance(client):
    contract = client.get('/fri/contract', headers=headers('fri')).json()
    assert 'turn-provenance' in contract['capabilities']


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_erasure_removes_other_members_turns_built_from_their_messages(store, erase):
    save(store, provenance=[ASH])
    save(store, reply='unrelated answer')
    save(store, user='40', conversation='public:2', provenance=[ASH])
    save(store, user='40', conversation='public:2', reply='kit keeps this', provenance=[{'user_id': '50', 'revision': 0}])
    if erase == 'disable':
        store.personal_control('30', 'disable')
    elif erase == 'erase_user':
        store.erase_user('30')
    else:
        store.clear_chats('30')
    assert replies(store) == ['unrelated answer']
    assert replies(store, '40', 'public:2') == ['kit keeps this']


def test_privacy_change_after_reading_context_rejects_the_save(store):
    store.personal_control('30', 'enable')  # Revision 0 -> 1 while a reply was being prepared.
    with pytest.raises(Conflict):
        save(store, provenance=[ASH])
    store.personal_control('30', 'disable')
    with pytest.raises(Conflict):
        save(store, provenance=[{'user_id': '30', 'revision': 2}])  # Current revision, but memory is off.
    assert replies(store) == []


def test_revision_change_hides_an_existing_dependent_turn(store):
    save(store, provenance=[ASH])
    save(store, reply='unrelated answer')
    with store.connection() as db:  # A revision change without an eager delete, e.g. a future path.
        db.execute('INSERT INTO chat_revisions VALUES (?,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1',
                   ('30',))
    assert replies(store) == ['unrelated answer']
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM chat_turns WHERE provenance!='[]'").fetchone()[0] == 0


def test_speaker_entries_are_ignored_and_conflicting_entries_fail(store):
    save(store, provenance=[{'user_id': '20', 'revision': 7}, ASH, ASH])
    with store.connection() as db:
        assert db.execute('SELECT provenance FROM chat_turns').fetchone()[0] == '[{"user_id": "30", "revision": 0}]'
    with pytest.raises(ValueError):
        save(store, provenance=[ASH, {'user_id': '30', 'revision': 1}])


def test_api_accepts_and_validates_provenance(client):
    scope = {'guild_id': 'guild', 'channel_id': 'workshop', 'conversation_id': 'public:456', 'user_id': '20'}
    body = {**scope, 'revision': 0, 'input': 'where is ash moving', 'reply': 'ash said denver',
            'source': source(guild='guild'), 'model': 'model', 'effort': 'high', 'provenance': [ASH]}
    assert client.post('/chat/turn', headers=headers('fri'), json=body).status_code == 200
    for invalid in ([{'user_id': 'ash', 'revision': 0}], [{'user_id': '30', 'revision': -1}],
                    [{**ASH, 'extra': True}], [ASH] * 41):
        assert client.post('/chat/turn', headers=headers('fri'), json={**body, 'provenance': invalid}).status_code == 422
    assert client.post('/chat/context', headers=headers('fri'), json=scope).json()['turns'][-1]['reply'] == 'ash said denver'


def test_existing_databases_gain_the_provenance_column(tmp_path):
    path = tmp_path/'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE chat_turns (id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, '
                   'user_id TEXT NOT NULL, input TEXT NOT NULL, reply TEXT NOT NULL, model TEXT NOT NULL, '
                   'effort TEXT NOT NULL, at REAL NOT NULL, input_tokens INTEGER, output_tokens INTEGER)')
        db.execute("INSERT INTO chat_turns(guild_id,channel_id,user_id,input,reply,model,effort,at) "
                   "VALUES ('123','public:1','20','old question','old answer','model','high',strftime('%s','now'))")
    store = Store(path)
    store.bootstrap('a' * 40)
    with store.connection() as db:
        assert 'provenance' in {row['name'] for row in db.execute('PRAGMA table_info(chat_turns)')}
    assert replies(store) == ['old answer']


def test_check_applies_the_save_fence_without_writing(store, client):
    assert store.check_chat('20', 0, [ASH]) == {'ok': True}
    store.personal_control('30', 'disable')
    with pytest.raises(Conflict):
        store.check_chat('20', 0, [ASH])
    store.personal_control('20', 'enable')
    with pytest.raises(Conflict):
        store.check_chat('20', 0)
    assert replies(store) == []
    scope = {'guild_id': 'guild', 'channel_id': 'workshop', 'conversation_id': 'public:456', 'user_id': '20'}
    body = {**scope, 'revision': 0, 'input': 'where is ash moving', 'reply': 'ash said denver',
            'source': source(guild='guild'), 'model': 'model', 'effort': 'high', 'provenance': [ASH]}
    assert client.post('/chat/turn/check', headers=headers('fri'), json=body).json() == {'ok': True}
    assert client.post('/chat/turn/check', headers=headers('runtime'), json=body).status_code == 401
    assert client.post('/chat/turn/check', headers=headers('fri'), json={**body, 'revision': 1}).status_code == 409
    assert client.post('/chat/turn/check', headers=headers('fri'),
                       json={**body, 'mention_grant': 'a' * 64}).status_code == 400
    assert not client.post('/chat/context', headers=headers('fri'), json=scope).json()['turns']
