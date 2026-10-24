"""Saved replies remember whose messages shaped them, so erasure and privacy changes reach them."""

import sqlite3

import pytest

from gene.store import Conflict, Store
from tests.test_api import headers
from tests.test_fri_endings import source

ASH = {'user_id': '30', 'revision': 0}


def save(store, user='20', conversation='public:1', reply='ash said denver', provenance=(), revision=0, **extra):
    return store.save_chat('123', conversation, user, revision, 'where is ash moving', reply, 'model', 'high',
                           source=extra.pop('source', None) or source(user=user, channel=conversation.split(':')[1]),
                           provenance=provenance, **extra)


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
                    [{**ASH, 'extra': True}], [ASH] * 201):
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


def test_returned_turns_keep_provenance_for_follow_ups(store):
    save(store, provenance=[ASH])
    turn = store.chat_context('123', 'public:1', '20')['turns'][-1]
    assert turn['provenance'] == [ASH]  # A follow-up built on this answer inherits Ash.


def test_pending_clarification_shaped_by_another_member_follows_their_privacy(store):
    question = {'goal': 'web', 'question': 'before or after ash moves to denver?', 'topic': 'how are the lakers doing'}
    def arm():
        store.save_chat('123', 'public:1', '20', 0, 'fri how are the lakers doing', question['question'], 'model',
                        'high', source=source(channel='1'), clarification=question, provenance=[ASH])
    arm()
    pending = store.chat_context('123', 'public:1', '20')['pending']
    assert pending['question'] == question['question'] and pending['provenance'] == [ASH]
    store.personal_control('30', 'disable')  # Eager: Ash's erasure removes the derived question.
    assert store.chat_context('123', 'public:1', '20')['pending'] is None
    store.personal_control('30', 'enable')
    with store.connection() as db:  # Lazy: a revision change hides an existing one.
        store._checked_provenance(db, '20', [{'user_id': '30', 'revision': 2}])
    store.save_chat('123', 'public:1', '20', 0, 'fri how are the lakers doing', question['question'], 'model',
                    'high', source=source(channel='1'), clarification=question,
                    provenance=[{'user_id': '30', 'revision': 2}])
    with store.connection() as db:
        db.execute('UPDATE chat_revisions SET revision=revision+1 WHERE user_id=?', ('30',))
    assert store.chat_context('123', 'public:1', '20')['pending'] is None


def test_summary_carries_its_authors_revisions(store, tmp_path):
    import time

    from gene.config import Settings
    from gene.fri_agent_store import AgentStore
    settings = Settings(tmp_path/'unused.sqlite3', '123', 'owner', 'r'*40, 'w'*40, 'd'*40)
    agent = AgentStore(store, settings)
    with store.connection() as db:
        db.execute('INSERT INTO agent_summaries VALUES (?,?,?,?)', ('1', 'ash is moving to denver', '["30", "20"]',
                                                                     time.time()))
    context = agent.context('1', '20')
    assert context['summary'] and context['summary_provenance'] == [ASH]
    store.personal_control('30', 'disable')
    assert agent.context('1', '20') | {'historical_topics': []} == {
        'summary': '', 'summary_provenance': [], 'historical_topics': [], 'enabled': True, 'revision': 0}


def test_fri_message_sources_resolve_or_fail_closed(store, client):
    save(store, provenance=[ASH], reply_message_id='555', verbatim=True)
    save(store, reply='Earlier check: read_recent. Fetch current evidence.', reply_message_id='556', verbatim=False)
    found = store.message_sources('123', ['555', '556', '557'])
    assert found['555'] == {'verbatim': True, 'provenance': [ASH, {'user_id': '20', 'revision': 0}]}
    assert found['556']['verbatim'] is False and '557' not in found
    store.personal_control('30', 'disable')
    assert '555' not in store.message_sources('123', ['555'])  # Removed with Ash's erasure.
    store.personal_control('20', 'disable')
    assert store.message_sources('123', ['556']) == {}  # The speaker turned memory off.
    body = {'guild_id': '123', 'message_ids': ['555']}
    assert client.post('/chat/messages/sources', headers=headers('fri'), json=body).json() == {'messages': {}}
    assert client.post('/chat/messages/sources', headers=headers('runtime'), json=body).status_code == 401
    for invalid in ({'guild_id': '123', 'message_ids': []}, {'guild_id': '123', 'message_ids': ['x']},
                    {'guild_id': '123', 'message_ids': ['1'] * 11}, {**body, 'extra': 1}):
        assert client.post('/chat/messages/sources', headers=headers('fri'), json=invalid).status_code == 422
