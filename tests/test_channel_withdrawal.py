"""HumanMint/fri#358 D1: a channel or thread deleted in Discord takes what was saved from it.

Deleting a channel or thread deletes every message in it, with no event per message, so nothing withdrawn by message ID
reached what was saved from it: a server-profile note learned there still reached a later private-panel request. Fri
now asks the controller to withdraw the channel itself. Everything held from it, and from its threads, goes as a
deleted message's does, and a record of the channel's ID (never words) keeps anything still on its way from it out,
under an older controller too."""

import json
import os
import subprocess
import sys
import time

import pytest

from gene.fri_channel_withdrawal import withdraw as withdraw_channels
from gene.fri_protocol import compatible, contract
from gene.store import Store
from tests.test_api import headers
from tests.test_erasure_rollback import DEPLOYED, extracted  # noqa: F401
from tests.test_fri_endings import source
from tests.test_turn_withdrawal import SCOPE, post_turn, turn, world  # noqa: F401

OLDER = (DEPLOYED, '13dad88')  # The release a rollback goes back to, and Gene main before this.

THREAD = '70'  # A thread under channel 51.


def withdraw(client, *channels, guild='123'):
    return client.post('/chat/channels/withdraw', headers=headers('fri'),
                       json={'guild_id': guild, 'channel_ids': list(channels)})


def said(client, text, channel='51', conversation=None, user='20', audience='server', **extra):
    """Member `user` says `text` in `conversation` (channel `channel`'s own, or a thread under it), noted for them."""
    conversation = conversation or 'public:' + channel
    where = conversation.split(':')[1]
    body = turn(source(user=user, channel=where), text, user=user, channel_id=channel, conversation_id=conversation,
                audience=audience, notes=[{'category': 'games', 'evidence': text}], **extra)
    answer = post_turn(client, body)
    assert answer.status_code == 200, answer.text
    return body


def notes(store, conversation='private:20', user='20'):
    return sorted(note['evidence'] for note in
                  store.chat_context('123', conversation, user, True)['personal_memory']['notes'])


def turns(store, conversation, user='20'):
    return [row['input'] for row in store.chat_context('123', conversation, user, True)['turns']]


def count(store, table, where='1', *args):
    with store.connection() as db:
        return db.execute(f'SELECT count(*) FROM {table} WHERE {where}', args).fetchone()[0]


def test_the_contract_offers_channel_withdrawals():
    assert 'channel-withdrawals' in contract()['capabilities'] and compatible(contract())


def test_a_deleted_channel_takes_what_was_saved_from_it(world):  # noqa: F811
    """The issue's repro: a server-profile note learned in a channel, which is then deleted with no event per message,
    no longer reaches a private-panel request. Its exchanges, a conversation note, a question it left open, research
    it permitted, a member summary drawing on it, its history summary and explore notes go too; another channel's
    stay."""
    client, store = world
    said(client, 'i like minecraft')
    said(client, 'i like chess', channel='52')
    said(client, 'i play valorant', audience='conversation')
    said(client, 'fri play something', clarification={'goal': 'play', 'topic': '', 'question': 'which song?'})
    now = time.time()
    with store.connection() as db:
        db.execute("INSERT INTO web_requests VALUES ('123','public:51','20','9001',0,?,'chess openings')", (now,))
        db.execute("INSERT INTO web_research(guild_id,user_id,channel_id,created,receipt) "
                   "VALUES ('123','20','public:51',?,'{}')", (now,))
        db.execute("INSERT INTO fri_member_summaries VALUES ('123','20','talks about minecraft',0,'[]',"
                   "'[\"public:51\"]',?,?)", (now, now))
        db.execute("INSERT INTO fri_member_summaries VALUES ('123','30','talks about chess',0,'[]',"
                   "'[\"public:52\"]',?,?)", (now, now))
        db.execute('CREATE TABLE IF NOT EXISTS agent_summaries (channel_id TEXT PRIMARY KEY, body TEXT NOT NULL, '
                   "authors TEXT NOT NULL, at REAL NOT NULL, revisions TEXT NOT NULL DEFAULT '{}')")
        db.execute("INSERT INTO agent_summaries(channel_id,body,authors,at) VALUES ('51','minecraft talk','[]',?)", (now,))
        db.execute("INSERT INTO agent_summaries(channel_id,body,authors,at) VALUES ('52','chess talk','[]',?)", (now,))
    assert notes(store) == ['i like chess', 'i like minecraft']
    assert withdraw(client, '51').json() == {'ok': True, 'channels': ['51'], 'removed': 3}
    assert notes(store) == ['i like chess']
    assert notes(store, 'public:51') == ['i like chess'] and turns(store, 'public:51') == []
    assert turns(store, 'public:52') == ['i like chess']
    assert store.chat_context('123', 'public:51', '20', True)['pending'] is None
    for table in ('web_requests', 'web_research'):
        assert count(store, table, "channel_id='public:51'") == 0
    assert count(store, 'fri_member_summaries', "user_id='20'") == 0
    assert count(store, 'fri_member_summaries', "user_id='30'") == 1
    assert count(store, 'agent_summaries', "channel_id='51'") == 0 and count(store, 'agent_summaries') == 1


def test_a_deleted_thread_takes_what_was_saved_from_it_and_leaves_its_channel(world):  # noqa: F811
    client, store = world
    said(client, 'i like minecraft', conversation='public:' + THREAD)
    said(client, 'i like chess')
    assert withdraw(client, THREAD).json() == {'ok': True, 'channels': [THREAD], 'removed': 1}
    assert notes(store) == ['i like chess'] and turns(store, 'public:51') == ['i like chess']
    assert turns(store, 'public:' + THREAD) == []


def test_a_deleted_channel_takes_its_threads_too(world):  # noqa: F811
    """Discord deletes a channel's threads with it, and sends no event for one Fri doesn't hold in its cache (an
    archived one, say). Each turn names the channel a thread is under, and the controller keeps that, so the channel's
    withdrawal takes its threads' too. One whose save didn't say (an older controller's) can be named later."""
    client, store = world
    said(client, 'i like minecraft', conversation='public:' + THREAD)
    store.save_chat('123', 'public:71', '20', 0, 'i like osu', 'noted', 'model', 'high', audience='server',
                    provenance=[], notes=[{'category': 'games', 'evidence': 'i like osu'}],
                    source=source(user='20', channel='71'))  # As an older controller saved it: no parent kept.
    said(client, 'i like chess', channel='52')
    assert client.post('/chat/channels/parents', headers=headers('fri'),
                       json={'guild_id': '123', 'parents': {'71': '51'}}).json() == {'ok': True}
    assert withdraw(client, '51').json() == {'ok': True, 'channels': ['51', THREAD, '71'], 'removed': 2}
    assert notes(store) == ['i like chess']


def test_what_was_still_on_its_way_from_a_deleted_channel_keeps_nothing(world):  # noqa: F811
    """A reply whose save was still on its way when the channel went keeps nothing, nor passes its check: the record
    of the channel stops it, as a deleted message's fence does."""
    client, store = world
    late = turn(source(channel='51'), 'i like minecraft', audience='server',
                notes=[{'category': 'games', 'evidence': 'i like minecraft'}])
    assert withdraw(client, '51').json() == {'ok': True, 'channels': ['51'], 'removed': 0}
    assert client.post('/chat/turn/check', headers=headers('fri'), json=late).status_code == 409
    assert post_turn(client, late).json() == {'ok': True, 'first_memory': False, 'kept': False}
    assert notes(store) == [] and turns(store, 'public:51') == []
    thread = turn(source(channel=THREAD), 'i like chess', conversation_id='public:' + THREAD)
    assert withdraw(client, THREAD).status_code == 200
    assert post_turn(client, thread).json() == {'ok': True, 'first_memory': False, 'kept': False}


def test_a_reply_elsewhere_that_carried_a_deleted_channels_message_keeps_only_a_breadcrumb(world):  # noqa: F811
    """As for a deleted message (gene #141): a reply in another channel whose model request carried the member's
    message from the deleted channel may repeat it."""
    client, store = world
    first = said(client, 'i like minecraft')
    said(client, 'what was i saying', channel='52', reply='you like minecraft', drew_on=[first['source']['id']])
    withdraw(client, '51')
    with store.connection() as db:
        reply = db.execute("SELECT reply FROM chat_turns WHERE channel_id='public:52'").fetchone()[0]
    assert reply != 'you like minecraft'
    late = turn(source(channel='52'), 'and now?', drew_on=[first['source']['id']])
    assert client.post('/chat/turn/check', headers=headers('fri'), json=late).status_code == 409


def test_the_controller_says_which_channels_it_holds_anything_from(world):  # noqa: F811
    """For Fri's check of channels it may have missed the deletion of (see Fri's fri_channel_withdrawal): each channel
    or thread anything saved came from, with the channel a thread is under where it's known; none withdrawn."""
    client, store = world
    said(client, 'i like minecraft')
    said(client, 'i like chess', conversation='public:' + THREAD)
    said(client, 'i like osu', channel='52')
    held = client.post('/chat/channels/held', headers=headers('fri'), json={'guild_id': '123'}).json()
    assert held == {'channels': [{'id': '51', 'parent': None}, {'id': '52', 'parent': None},
                                 {'id': THREAD, 'parent': '51'}]}
    withdraw(client, '52')
    held = client.post('/chat/channels/held', headers=headers('fri'), json={'guild_id': '123'}).json()
    assert [row['id'] for row in held['channels']] == ['51', THREAD]


@pytest.mark.parametrize('body', [{'guild_id': '999', 'channel_ids': ['51']}])
def test_only_fri_and_only_its_server(world, body):  # noqa: F811
    client, _ = world
    assert client.post('/chat/channels/withdraw', headers=headers('runtime'), json=body).status_code == 401
    assert client.post('/chat/channels/withdraw', headers=headers('fri'), json=body).status_code == 403
    for invalid in ({'channel_ids': []}, {'channel_ids': ['general']}, {'channel_ids': [str(i) for i in range(101)]}):
        assert client.post('/chat/channels/withdraw', headers=headers('fri'),
                           json={**body, 'guild_id': '123', **invalid}).status_code == 422


def test_a_withdrawn_channel_is_kept_out_by_the_database_itself(world):  # noqa: F811
    """An older controller, in a rollback window, knows nothing of the record: what it would save from a withdrawn
    channel, an exchange, a note or a question, goes as it's written. Elsewhere nothing changes."""
    client, store = world
    withdraw(client, '51', THREAD)
    with store.connection() as db:
        for conversation in ('public:51', 'public:' + THREAD, 'public:52'):
            db.execute('INSERT INTO chat_turns(guild_id,channel_id,user_id,input,reply,model,effort,at) '
                       "VALUES ('123',?,'20','hi','hey','model','high',?)", (conversation, time.time()))
            db.execute('INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at,origin) '
                       "VALUES ('123',?,'20','games','i like chess',?,?)", (conversation, time.time(), conversation))
            db.execute("INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at,origin) "
                       "VALUES ('123','server','20','games',?,?,?)", ('i like ' + conversation, time.time(),
                                                                     conversation))
            db.execute("INSERT INTO chat_pending VALUES ('123',?,'20','1',?,0,'{}')", (conversation, time.time()))
        db.execute("UPDATE personal_notes SET origin='public:51' WHERE evidence='i like public:52'")
    assert count(store, 'chat_turns') == 1 and count(store, 'chat_pending') == 1
    assert notes(store, 'public:52') == ['i like chess']


@pytest.mark.parametrize('sha', OLDER)
def test_an_older_controller_starts_and_keeps_nothing_from_a_withdrawn_channel(tmp_path, extracted, sha):  # noqa: F811
    """A rollback window: the deployed release, and Gene main before this, start on a database this one wrote, a
    channel withdrawn in it, as their own code does. Their saves from that channel, an exchange and a profile note,
    keep nothing; elsewhere they save as before."""
    code = extracted(sha)
    if code is None:
        pytest.skip(f'{sha} is not in this checkout')
    path = tmp_path/'gene.sqlite3'
    store = Store(path)
    withdraw_channels(store, '123', ['51'])
    saves = [{'conversation': 'public:'+channel, 'text': text, 'source': source(channel=channel)}
             for channel, text in (('51', 'i like minecraft'), ('52', 'i like chess'))]
    script = ('import json, sys\nfrom pathlib import Path\nfrom gene.store import Store\n'
              'store = Store(Path(sys.argv[1]))\n'
              'for save in json.loads(sys.argv[2]):\n'
              '    store.save_chat("123", save["conversation"], "20", 0, save["text"], "noted", "model", "high",\n'
              '                    notes=[{"category": "games", "evidence": save["text"]}], source=save["source"],\n'
              '                    provenance=[], audience="server")\n')
    done = subprocess.run([sys.executable, '-c', script, str(path), json.dumps(saves)], cwd=code,
                          env={**os.environ, 'PYTHONPATH': str(code)}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
    store = Store(path)
    assert notes(store) == ['i like chess']
    assert turns(store, 'public:51') == [] and turns(store, 'public:52') == ['i like chess']
