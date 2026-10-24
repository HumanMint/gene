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
    # It may quote other members with no record of whom: never treat it as fully attributed.
    assert store.chat_context('123', 'public:1', '20')['turns'][0]['attributed'] is False


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


def summary_agent(store, tmp_path, revisions='{"30": 0, "20": 0}'):
    import time

    from gene.config import Settings
    from gene.fri_agent_store import AgentStore
    agent = AgentStore(store, Settings(tmp_path/'unused.sqlite3', '123', 'owner', 'r'*40, 'w'*40, 'd'*40))
    with store.connection() as db:
        db.execute('INSERT INTO agent_summaries(channel_id,body,authors,at,revisions) VALUES (?,?,?,?,?)',
                   ('1', 'ash is moving to denver', '["20", "30"]', time.time(), revisions))
    return agent


def test_summary_carries_the_revisions_it_was_made_from(store, tmp_path):
    agent = summary_agent(store, tmp_path)
    context = agent.context('1', '20')
    assert context['summary'] and context['summary_provenance'] == [ASH]
    store.personal_control('30', 'disable')
    assert agent.context('1', '20')['summary'] == ''


@pytest.mark.parametrize('change', ['clear_chats', 'enable', 'legacy'])
def test_summary_fails_closed_after_any_author_revision_change(store, tmp_path, change):
    agent = summary_agent(store, tmp_path, '{}' if change == 'legacy' else '{"30": 0, "20": 0}')
    if change == 'clear_chats':
        store.clear_chats('30')
    elif change == 'enable':
        store.personal_control('30', 'enable')  # Revision moves without an eager delete.
    context = agent.context('1', '20')
    assert context['summary'] == '' and context['summary_provenance'] == []
    with store.connection() as db:
        assert not db.execute('SELECT COUNT(*) FROM agent_summaries').fetchone()[0]


def test_consumed_clarification_keeps_no_assistant_question(store):
    import json

    from gene.control_store import Controls, initialize
    from tests.test_gene_controls import control_body
    store.save_chat('123', 'public:1', '20', 0, 'fri play music', 'before or after ash moves to denver?',
                    'model', 'high', source=source(age=30, channel='1'),
                    clarification={'goal': 'play', 'topic': '', 'question': 'before or after ash moves to denver?'},
                    provenance=[ASH])
    pending = store.chat_context('123', 'public:1', '20')['pending']
    controls = Controls(store)
    controls.enqueue(control_body(request_text='blind melon no rain', argument='blind melon no rain',
                                  continuation_id=pending['source']['id']))
    store.personal_control('30', 'disable')
    with store.connection() as db:
        payload = json.loads(db.execute('SELECT payload FROM gene_controls').fetchone()[0])
        assert set(payload['continuation']) == {'goal', 'source', 'request_text', 'revision'}
        assert 'denver' not in str(controls.poll(['play']))
        # Rows written before this change are scrubbed on startup.
        payload['continuation']['question'] = 'ash is moving to denver'
        db.execute('UPDATE gene_controls SET payload=?', (json.dumps(payload),))
        initialize(db)
        assert 'denver' not in db.execute('SELECT payload FROM gene_controls').fetchone()[0]


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


def test_learning_overview_applies_the_summary_revision_fence(store, tmp_path):
    from gene.config import Settings
    from gene.fri_agent_store import AgentStore
    agent = summary_agent(store, tmp_path)
    agent.settings = Settings(tmp_path/'unused.sqlite3', '123', 'owner', 'r'*40, 'w'*40, 'd'*40,
                              agent_history_channels='1')
    assert [s['body'] for s in agent.overview()['summaries']] == ['ash is moving to denver']
    store.personal_control('30', 'enable')  # Revision moves without an eager delete.
    assert AgentStore(store, agent.settings).overview()['summaries'] == []


def test_clear_chats_removes_learning_reports_and_their_delivery_queue(store, tmp_path):
    import time

    from gene.config import Settings
    from gene.fri_agent_store import AgentStore
    agent = AgentStore(store, Settings(tmp_path/'unused.sqlite3', '123', 'owner', 'r'*40, 'w'*40, 'd'*40))
    with store.connection() as db:
        db.execute('INSERT INTO agent_reports(id,at,body,delivered) VALUES (?,?,?,0)',
                   ('r1', time.time(), 'Untrusted summary: ash is moving to denver'))
    assert agent.reports()
    store.clear_chats('30')
    assert agent.reports() == [] and agent.overview()['reports'] == []


def test_fri_message_sources_never_relabel_the_original_speaker(store):
    save(store, provenance=[ASH], reply_message_id='555', verbatim=True)
    assert store.message_sources('123', ['555'])['555']['provenance'][-1] == {'user_id': '20', 'revision': 0}
    store.personal_control('20', 'enable')  # The speaker's revision moves: the old reply no longer resolves.
    assert store.message_sources('123', ['555']) == {}
    with store.connection() as db:  # Rows saved before speaker revisions were recorded fail closed.
        db.execute("UPDATE chat_turns SET speaker_revision=NULL, provenance='[]'")
        db.execute("UPDATE chat_revisions SET revision=0 WHERE user_id='20'")
    assert store.message_sources('123', ['555']) == {}


def job(store, user_id='20', source_id='fri-draft:1', provenance=(ASH,), revision=None):
    return store.request('123', user_id, source_id, '', 'Add a moving checklist because ash moves', list(provenance),
                         revision)


def test_request_checks_draft_provenance_when_it_is_created(store, client):
    store.personal_control('30', 'enable')  # Revision moved after the draft was checked.
    with pytest.raises(Conflict):
        job(store)
    client.app.state.store.personal_control('30', 'enable')
    payload = {'guild_id': 'guild', 'user_id': 'member', 'source_id': 'fri-draft:2', 'source_url': '',
               'body': 'Add a moving checklist', 'channel_id': 'workshop', 'provenance': [ASH]}
    assert client.post('/requests', headers=headers('fri'), json=payload).status_code == 409
    for invalid in ([{'user_id': 'ash', 'revision': 0}], [ASH] * 201):
        assert client.post('/requests', headers=headers('fri'), json={**payload, 'provenance': invalid}).status_code == 422
    assert client.post('/requests', headers=headers('fri'),
                       json={**payload, 'provenance': [{'user_id': '30', 'revision': 1}]}).status_code == 200


@pytest.mark.parametrize('erase', ['disable', 'enable', 'erase_user', 'clear_chats', 'note_removal'])
def test_any_privacy_change_withdraws_requests_submitted_from_derived_drafts(store, erase):
    from gene.fri_memory_store import edit, entries
    text = 'I like A Change of Heart'
    store.save_chat('123', 'public:1', '30', 0, text, 'noted', 'model', 'high', source=source(user='30', channel='1'),
                    notes=[{'category': 'music', 'evidence': text}], provenance=[])
    derived, own = job(store), job(store, source_id='fri-draft:own', provenance=())
    if erase in ('disable', 'enable'):
        store.personal_control('30', erase)
    elif erase == 'note_removal':
        note = entries(store, '123', 'public:1', '30')['entries'][0]
        edit(store, '123', 'public:1', '30', note['id'], 0,
             {**source(user='30', channel='1'), 'kind': 'discord_interaction'}, 'remove')
    else:
        getattr(store, erase)('30')
    with store.connection() as db:
        rows = {row['id']: dict(row) for row in db.execute('SELECT id,user_id,body,state,provenance FROM jobs')}
    assert rows[derived['id']] == {'id': derived['id'], 'user_id': '20', 'state': 'rejected', 'provenance': '[]',
                                   'body': '[Request withdrawn: someone it drew on changed their privacy settings]'}
    assert rows[own['id']]['body'] == 'Add a moving checklist because ash moves'
    assert rows[own['id']]['state'] == 'awaiting_owner'


def test_downgrade_removes_derived_state_and_restores_the_legacy_summary_schema(store, tmp_path):
    from gene.provenance_downgrade import main
    save(store, provenance=[ASH])
    save(store, reply='my own answer')
    derived = job(store)
    summary_agent(store, tmp_path)
    main([str(store.path)])
    assert replies(store) == ['my own answer']
    with store.connection() as db:
        assert db.execute('SELECT body FROM jobs WHERE id=?', (derived['id'],)).fetchone()[0].startswith('[Request withdrawn')
        assert 'revisions' not in {row['name'] for row in db.execute('PRAGMA table_info(agent_summaries)')}
        # The exact insert the controller before turn-provenance uses.
        db.execute('INSERT INTO agent_summaries VALUES (?,?,?,?) ON CONFLICT(channel_id) '
                   'DO UPDATE SET body=excluded.body,authors=excluded.authors,at=excluded.at',
                   ('1', 'safe summary', '["20"]', 0))


@pytest.mark.parametrize('erase', ['dependent', 'own'])
def test_erasure_stops_a_running_build_from_writing_back(store, erase):
    from gene.reviews import request_reference
    store.set_paused(False, 'owner')
    created = job(store)
    store.review_decision(created['id'], 'intake', request_reference(created), 'approve', 'owner')
    running = store.claim()
    assert running['id'] == created['id']
    store.clear_chats('30') if erase == 'dependent' else store.erase_user('20')
    stopped = store.get(created['id'])
    assert stopped['state'] == 'rejected' and stopped['lease'] is None
    with pytest.raises(Conflict):
        store.heartbeat(created['id'], running['lease'])
    with pytest.raises(Conflict):
        store.finish_build(created['id'], running['lease'], {'sha': 'b'*40, 'base_sha': 'a'*40,
                           'branch': 'codex/request', 'tests_passed': True, 'summary': 'ash moves to denver'})
    assert 'denver' not in str(store.get(created['id']))


def test_request_checks_the_requesters_own_revision_when_created(store, client):
    store.personal_control('20', 'disable')  # Erased after Fri's fence passed.
    with pytest.raises(Conflict):
        job(store, provenance=(), source_id='fri-draft:own', revision=0)
    assert job(store, provenance=(), source_id='fri-draft:own', revision=1)['state'] == 'awaiting_owner'
    assert job(store, provenance=(), source_id='panel:legacy')['state'] == 'awaiting_owner'  # Explicit requests.
    payload = {'guild_id': 'guild', 'user_id': 'member', 'source_id': 'fri-draft:3', 'source_url': '',
               'body': 'Add a moving checklist', 'channel_id': 'workshop', 'revision': 1}
    assert client.post('/requests', headers=headers('fri'), json=payload).status_code == 409
    assert client.post('/requests', headers=headers('fri'), json={**payload, 'revision': 0}).status_code == 200



def test_turns_saved_without_provenance_are_marked_unattributed(store, client):
    save(store, reply='legacy companion reply', provenance=None)
    save(store, reply='capable reply', provenance=[])
    assert [(turn['reply'], turn['attributed']) for turn in store.chat_context('123', 'public:1', '20')['turns']] == [
        ('legacy companion reply', False), ('capable reply', True)]
    scope = {'guild_id': 'guild', 'channel_id': 'workshop', 'conversation_id': 'public:456', 'user_id': '20'}
    body = {**scope, 'revision': 0, 'input': 'where is ash moving', 'reply': 'ash said denver',
            'source': source(guild='guild'), 'model': 'model', 'effort': 'high'}
    assert client.post('/chat/turn', headers=headers('fri'), json=body).status_code == 200
    assert client.post('/chat/turn/check', headers=headers('fri'), json=body).json() == {'ok': True}
    body['source'] = source(guild='guild', age=1)
    assert client.post('/chat/turn', headers=headers('fri'), json={**body, 'provenance': []}).status_code == 200
    turns = client.post('/chat/context', headers=headers('fri'), json=scope).json()['turns']
    assert [turn['attributed'] for turn in turns] == [False, True]


def test_pending_question_from_a_legacy_companion_carries_no_provenance(store):
    question = {'goal': 'web', 'question': 'before or after ash moves to denver?', 'topic': 'how are the lakers doing'}
    store.save_chat('123', 'public:1', '20', 0, 'fri how are the lakers doing', question['question'], 'model',
                    'high', source=source(channel='1'), clarification=question)
    assert 'provenance' not in store.chat_context('123', 'public:1', '20')['pending']
