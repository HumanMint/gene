"""Fri's own rolling summary of each member, built only from what they said where everyone could read it."""

import json
import sqlite3
import time

import pytest

from gene.config import Settings
from gene.fri_agent_store import AgentStore
from gene.fri_member_summary import CLAIM, DAY, LENGTH, NEXT, TTL, claim, finish, main, purge
from gene.fri_memory_store import edit as edit_note
from gene.fri_memory_store import entries
from gene.fri_protocol import compatible, contract
from gene.store import Conflict, Store
from tests.test_api import headers
from tests.test_fri_memory_items import source as control_source
from tests.test_gene_controls import controls_api  # noqa: F401
from tests.test_member_profiles import say

ASH = {'user_id': '30', 'revision': 0}


def background(store, tmp_path):
    agent = AgentStore(store, Settings(tmp_path/'unused.sqlite3', '123', 'owner', 'r'*40, 'w'*40, 'd'*40,
                                       agent_channels='1,2'))
    agent.update({'enabled': True}, 0, 'owner')
    return agent


def chat(store, text, conversation='public:1', user='20', audience='server', provenance=(), ago=7200):
    """An exchange the member had with Fri, `ago` seconds back."""
    revision = store.chat_context('123', conversation, user)['revision']
    say(store, text, None, conversation=conversation, audience=audience, user=user, provenance=provenance,
        revision=revision, reply='fri said something')
    with store.connection() as db:
        db.execute('UPDATE chat_turns SET at=at-? WHERE id=(SELECT max(id) FROM chat_turns)', (ago,))


def offered(agent):
    with agent.store.connection() as db:
        agent.put(db, NEXT, 0)  # Not waiting out the idle pause between looks.
    return claim(agent)


def build(agent, text='they planned a raid and asked about builds', status='completed', keep=None, previous=True):
    offer = offered(agent)
    assert offer, 'nobody was due a summary'
    conversations = keep if keep is not None else sorted({row['conversation_id'] for row in offer['exchanges']})
    return offer, finish(agent, offer['id'], text, status, conversations, previous)


def shown(store, user='20', conversation='public:3'):
    return store.chat_context('123', conversation, user, True, True)['member_summary']


def rows(store):
    with store.connection() as db:
        return db.execute('SELECT count(*) FROM fri_member_summaries WHERE body<>""').fetchone()[0]


def private_source():
    return {**control_source(), 'channel_id': '9'}


def test_built_only_from_the_members_own_words_where_everyone_could_read(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    chat(store, 'dm only stuff', 'private:9', audience='conversation')
    chat(store, 'restricted channel stuff', 'public:5', audience='conversation')
    chat(store, 'someone else entirely', user='30')
    chat(store, 'someone else again', user='30')
    offer, kept = build(agent)
    assert [row['message'] for row in offer['exchanges']] == ['raid tonight?', 'what build for the raid']
    assert [row['conversation_id'] for row in offer['exchanges']] == ['public:1', 'public:2']
    assert offer['previous'] is None and (offer['model'], offer['effort']) == ('gpt-6-luna', 'medium')
    sent = json.dumps(offer)
    assert 'fri said something' not in sent and 'dm only' not in sent and 'restricted' not in sent and '"20"' not in sent
    assert kept == {'kept': True}
    for conversation in ('public:3', 'public:5', 'private:9'):
        assert shown(store, conversation=conversation)['text'] == 'they planned a raid and asked about builds'
    assert shown(store)['origins'] == ['public:1', 'public:2'] and shown(store)['provenance'] == []
    assert shown(store, '30') is None  # Never another member's.


def test_mentions_links_and_ids_never_reach_the_model_or_the_summary(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'ask <@123456789012345678> about https://discord.com/channels/1/2/3')
    chat(store, 'ok')
    offer, kept = build(agent, 'they asked <@123456789012345678> something')
    assert offer['exchanges'][0]['message'] == 'ask @someone about [discord link]'
    assert kept == {'kept': False} and shown(store) is None
    chat(store, 'more raid talk')
    chat(store, 'and more')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    build(agent, 'word '*400)
    assert 0 < len(shown(store)['text']) <= LENGTH


def test_waits_for_two_settled_exchanges_and_builds_once_a_day(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'one exchange')
    assert offered(agent) is None
    chat(store, 'still chatting', ago=60)
    assert offered(agent) is None  # The chat is still going.
    with store.connection() as db:
        db.execute('UPDATE chat_turns SET at=at-7200')
    first, _ = build(agent)
    assert len(first['exchanges']) == 2
    chat(store, 'the next day stuff')
    chat(store, 'more of it')
    assert offered(agent) is None  # One call a day, even with more to say.
    with store.connection() as db:
        db.execute('UPDATE fri_member_summary_calls SET at=at-?', (DAY+1,))
    second, _ = build(agent, 'they planned a raid and talked about the next day')
    assert [row['message'] for row in second['exchanges']] == ['the next day stuff', 'more of it']
    assert second['previous']['text'] == 'they planned a raid and asked about builds'
    assert shown(store)['text'] == 'they planned a raid and talked about the next day'


def test_a_failed_call_still_counts_and_a_claim_runs_alone(store, tmp_path):
    agent = background(store, tmp_path)
    for user in ('20', '30'):
        chat(store, 'hello there', user=user)
        chat(store, 'and again', user=user)
    offer = offered(agent)
    assert offered(agent) is None  # One build at a time.
    assert finish(agent, offer['id'], '', 'failed', ['public:1'], False) == {'kept': False}
    with pytest.raises(ValueError, match='expired'):
        finish(agent, offer['id'], 'again', 'completed', ['public:1'], False)
    other = offered(agent)
    assert other and other['id'] != offer['id']
    finish(agent, other['id'], 'x', 'failed', [], False)
    assert offered(agent) is None  # Both members used today's call.


def test_off_while_background_work_or_learning_is_off(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'hello there')
    chat(store, 'and again')
    agent.update({'learning_enabled': False}, 1, 'owner')
    assert offered(agent) is None
    agent.update({'learning_enabled': True, 'enabled': False}, 2, 'owner')
    assert offered(agent) is None


@pytest.mark.parametrize('change', ['clear', 'forget', 'other'])
def test_a_stale_build_cannot_overwrite_a_newer_erase(store, tmp_path, change):
    agent = background(store, tmp_path)
    chat(store, 'hello there', provenance=[ASH])
    chat(store, 'and again')
    offer = offered(agent)
    {'clear': lambda: store.clear_chats('20'), 'forget': lambda: store.personal_control('20', 'disable'),
     'other': lambda: store.personal_control('30', 'disable')}[change]()
    with pytest.raises(Conflict, match='Nothing was kept'):
        finish(agent, offer['id'], 'they said hello', 'completed', ['public:1'], False)
    assert rows(store) == 0
    with store.connection() as db:
        assert agent.get(db, CLAIM) is None  # Spent, so the next member isn't held up.


def test_the_finish_keeps_only_what_fri_sent(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'hello there', provenance=[ASH])
    chat(store, 'channel two stuff', 'public:2', provenance=[{'user_id': '40', 'revision': 0}])
    build(agent, keep=['public:2', 'public:99'])
    assert shown(store)['origins'] == ['public:2']
    assert shown(store)['provenance'] == [{'user_id': '40', 'revision': 0}]
    store.personal_control('30', 'disable')  # Not in what was kept.
    assert shown(store)['text']


def test_another_members_privacy_change_takes_a_summary_built_on_their_words(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'replying to ash about raids', provenance=[ASH])
    chat(store, 'and more raids')
    build(agent)
    assert shown(store)['provenance'] == [ASH]
    store.clear_chats('30')
    assert shown(store) is None and rows(store) == 0


@pytest.mark.parametrize('change', ['clear', 'forget', 'erase', 'enable', 'note'])
def test_the_members_own_revision_change_invalidates_it(store, tmp_path, change):
    agent = background(store, tmp_path)
    say(store, 'I like restricted chess', conversation='public:5', audience='conversation')
    chat(store, 'hello there')
    chat(store, 'and again')
    build(agent)
    assert shown(store)
    if change == 'note':  # An edit to a note somewhere else entirely.
        note = entries(store, '123', 'public:5', '20')['entries'][-1]
        edit_note(store, '123', 'public:5', '20', note['id'], 0, {**control_source(), 'channel_id': '5'}, 'remove')
    else:
        {'clear': lambda: store.clear_chats('20'), 'forget': lambda: store.personal_control('20', 'disable'),
         'erase': lambda: store.erase_user('20'), 'enable': lambda: store.personal_control('20', 'enable')}[change]()
    assert shown(store) is None and rows(store) == 0
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    assert offered(agent) is None  # What was said before the change never builds it again.


def test_forget_me_and_memory_off_wipe_it(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'hello there')
    chat(store, 'and again')
    build(agent)
    store.personal_control('20', 'disable')
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM fri_member_summaries WHERE user_id='20'").fetchone()[0] == 0
    assert store.chat_context('123', 'private:9', '20', True, True)['member_summary'] is None


def test_it_expires_thirty_days_after_its_last_refresh(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'hello there')
    chat(store, 'and again')
    build(agent)
    with store.connection() as db:
        db.execute('UPDATE fri_member_summaries SET refreshed=?', (time.time()-TTL+60,))
    assert shown(store)['expires_at'] > time.time()
    with store.connection() as db:
        db.execute('UPDATE fri_member_summaries SET refreshed=?', (time.time()-TTL-1,))
    store.status()  # The health check prunes it, even if nobody asks.
    assert rows(store) == 0 and shown(store) is None


def test_nothing_worth_keeping_keeps_no_text_and_waits_for_new_exchanges(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'hello there')
    chat(store, 'and again')
    assert build(agent, '')[1] == {'kept': False}
    assert shown(store) is None and rows(store) == 0
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    assert offered(agent) is None  # It read those already.


def test_context_and_lists_give_it_only_to_a_fri_that_asks(controls_api):  # noqa: F811
    client, _ = controls_api
    store, agent = client.app.state.store, client.app.state.agent
    agent.update({'enabled': True}, 0, '10')
    chat(store, 'hello there')
    chat(store, 'and again')
    build(agent)
    scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1', 'profiles': True}
    older = client.post('/chat/context', headers=headers('fri'), json=scope).json()
    assert 'member_summary' not in older and 'they planned' not in json.dumps(older)
    newer = client.post('/chat/context', headers=headers('fri'), json={**scope, 'summaries': True}).json()
    assert newer['member_summary']['text'] == 'they planned a raid and asked about builds'
    private = {**scope, 'channel_id': '456', 'conversation_id': 'private:9'}
    assert 'summary' not in client.post('/chat/memory/notes', headers=headers('fri'), json=private).json()
    listed = client.post('/chat/memory/notes', headers=headers('fri'), json={**private, 'summaries': True}).json()
    assert listed['summary']['text'] == 'they planned a raid and asked about builds'
    assert listed['summary']['expires_at'] - listed['summary']['refreshed'] == TTL
    public = client.post('/chat/memory/notes', headers=headers('fri'), json={**scope, 'summaries': True}).json()
    assert public['summary'] is None  # Only a private chat lists it.


def test_my_memory_edits_and_clears_it(controls_api):  # noqa: F811
    client, _ = controls_api
    store, agent = client.app.state.store, client.app.state.agent
    agent.update({'enabled': True}, 0, '10')
    chat(store, 'hello there', provenance=[ASH])
    chat(store, 'and again')
    build(agent)
    body = {'guild_id': '123', 'channel_id': '456', 'conversation_id': 'private:9', 'user_id': '20', 'profiles': True,
            'summaries': True, 'source': private_source(), 'revision': 0}
    def post(**changes):
        return client.post('/chat/memory/summary', headers=headers('fri'), json={**body, **changes})
    assert post(action='replace', text='see https://example.com').status_code == 400
    assert post(action='replace', text='mostly raids', revision=5).status_code == 400
    public = {**body, 'channel_id': '1', 'conversation_id': 'public:1', 'source': control_source()}
    assert client.post('/chat/memory/summary', headers=headers('fri'), json={
        **public, 'action': 'remove'}).status_code == 400
    edited = post(action='replace', text='mostly raids, some builds')
    assert edited.status_code == 200 and edited.json()['summary']['text'] == 'mostly raids, some builds'
    assert edited.json()['revision'] == 1
    with store.connection() as db:  # Like a profile note edit: saved chats across the server go.
        assert db.execute("SELECT count(*) FROM chat_turns WHERE user_id='20'").fetchone()[0] == 0
    now = shown(store)
    assert (now['provenance'], now['origins']) == ([], [])
    store.personal_control('30', 'disable')  # Their own words now.
    assert shown(store)['text'] == 'mostly raids, some builds'
    cleared = post(action='remove', revision=1)
    assert cleared.status_code == 200 and cleared.json()['summary'] is None and cleared.json()['revision'] == 2
    assert rows(store) == 0
    assert post(action='remove', revision=2).status_code == 400  # Nothing left to clear.
    assert post(action='rewrite', revision=2).status_code == 422


def test_claim_and_finish_over_the_api(controls_api):  # noqa: F811
    client, _ = controls_api
    store, agent = client.app.state.store, client.app.state.agent
    agent.update({'enabled': True}, 0, '10')
    chat(store, 'hello there')
    chat(store, 'and again')
    offer = client.post('/agent/member-summary/claim', headers=headers('fri')).json()
    assert client.post('/agent/member-summary/claim', headers=headers('runtime')).status_code == 401
    store.clear_chats('20')
    stale = client.post(f"/agent/member-summary/{offer['id']}/finish", headers=headers('fri'), json={
        'summary': 'they said hello', 'status': 'completed', 'conversations': ['public:1']})
    assert stale.status_code == 409 and rows(store) == 0
    assert client.post('/agent/member-summary/nope/finish', headers=headers('fri'), json={
        'status': 'completed'}).status_code == 400


def test_contract_advertises_optional_member_summaries():
    advertised = contract()
    assert 'member-summaries' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != 'member-summaries']})


def test_the_downgrade_purges_summaries_for_an_older_controller(store, tmp_path, capsys):
    agent = background(store, tmp_path)
    chat(store, 'hello there')
    chat(store, 'and again')
    build(agent)
    with store.connection() as db:
        agent.put(db, CLAIM, {'id': 'x'})
    main(['purge', str(store.path)])
    assert 'removed' in capsys.readouterr().out
    with sqlite3.connect(store.path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'fri_member_summar%'").fetchall()
        assert not db.execute('SELECT 1 FROM agent_state WHERE key IN (?,?)', (CLAIM, NEXT)).fetchall()
        assert db.execute("SELECT count(*) FROM chat_turns WHERE user_id='20'").fetchone()[0] == 2  # Exchanges stay.
    purge(str(store.path))  # Repeatable.
    with pytest.raises(SystemExit, match='Nothing changed'):
        main(['purge', str(tmp_path/'missing.sqlite3')])
    assert not (tmp_path/'missing.sqlite3').exists()
    Store(store.path)  # A newer controller starting again finds an empty table.
    assert rows(store) == 0
