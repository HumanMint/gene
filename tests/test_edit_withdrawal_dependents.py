"""What an edited or deleted message's words reached besides its own exchange and notes (gene #246, #249).

1. gene #249: a member summary whose recorded sources (fri#333) name the message goes, whatever else holds the message:
   its exchanges may be trimmed and no note may hold it, and the summary still read it. So does a build in flight whose
   claim names it. A summary with no recorded sources goes by where and when, under an exchange holding the message, as
   before.
2. gene #246: another member's saved reply whose model request carried the message (`drew_on`) keeps only a breadcrumb
   once the message is edited, as it does once it's deleted, and so does one that carried Fri's reply to it. With no
   versions of the message, what drew on it can't be told from what it says now. A save landing late, from a turn that
   began before the edit, keeps nothing.
"""
import json
import os
import subprocess
import sys
import time

import pytest

from gene import store as controller
from gene.fri_member_summary import CLAIM, generation
from tests.test_erasure_rollback import extracted  # noqa: F401 (the fixture)
from tests.test_fri_endings import source
from tests.test_member_summaries import background, build, chat, offered, shown
from tests.test_saved_withdrawal import withdraw_saved
from tests.test_summary_sources import sources, tomorrow
from tests.test_turn_withdrawal import MARKER, withdraw
from tests.test_withdrawal_sources import learn, replies, turn, world  # noqa: F401 (the fixture)

# gene #249: summaries.

def summarized(store, tmp_path):
    """Member 20's summary of two exchanges, its sources recorded; the first exchange's message, its exchange trimmed
    since, as a seventh exchange trims it, and no note learned from it."""
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    build(agent)
    assert shown(store)
    message = sources(store)[0]['id']
    with store.connection() as db:
        db.execute("DELETE FROM chat_turns WHERE json_extract(source,'$.id')=?", (message,))
    return agent, message


def generations(store, user='20'):
    with store.connection() as db:
        return generation(db, '123', user)


@pytest.mark.parametrize('deleted', [False, True], ids=['edited', 'deleted'])
def test_a_summary_thats_an_edited_messages_only_remaining_dependency_goes(store, tmp_path, deleted):
    """The issue's repro: nothing but the summary holds the message now, so the withdrawal found nothing and left it,
    and its old words reached a later request."""
    _, message = summarized(store, tmp_path)
    before = generations(store)
    store.withdraw_saved('123', [message], deleted=deleted)
    assert shown(store) is None and generations(store) == before + 1


def test_a_summary_that_never_read_the_message_stays(store, tmp_path):
    summarized(store, tmp_path)
    before = generations(store)
    store.withdraw_saved('123', [source(age=7200, channel='1')['id']], deleted=False)  # In its conversation, unread.
    assert shown(store) and generations(store) == before


def test_a_summary_whose_sources_arent_known_goes_by_where_and_when_as_before(store, tmp_path):
    """One an older controller built names nothing it read: by ID alone, nothing places the message against it, as
    before; an exchange holding the message places it by where and when (see fri_member_summary.withdrawn)."""
    _, message = summarized(store, tmp_path)
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_sources')
    store.withdraw_saved('123', [message], deleted=False)
    assert shown(store)


def claim_of(store):
    with store.connection() as db:
        row = db.execute('SELECT value FROM agent_state WHERE key=?', (CLAIM,)).fetchone()
    return json.loads(row[0]) if row else None


@pytest.mark.parametrize('rebuild', [False, True], ids=['first build', 'a rebuild'])
def test_a_build_in_flight_that_read_the_message_is_called_off(store, tmp_path, rebuild):
    """Its claim names the message, as an exchange it was sent or one its earlier summary drew on, though nothing else
    holds it now: it's called off, and its finish keeps nothing."""
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    if rebuild:
        build(agent)
        chat(store, 'the raid went well', ago=5400)
        chat(store, 'next raid friday', ago=5400)
        tomorrow(store)
        message = sources(store)[0]['id']  # Only its earlier summary read this one.
    offered(agent)
    claim = claim_of(store)
    if not rebuild:
        message = claim['messages'][0]['id']
    with store.connection() as db:
        db.execute("DELETE FROM chat_turns WHERE json_extract(source,'$.id')=?", (message,))
    store.withdraw_saved('123', [message], deleted=False)
    assert claim_of(store) is None


# gene #246: replies that carried it.

def reply_to(store, drew_on, *, user='30', age=2, text='what did they mean'):
    said = source(age=age, user=user, channel='51')
    return store.save_chat('123', 'public:51', user, 0, text, 'about that: '+MARKER, 'model', 'high', source=said,
                           provenance=[{'user_id': '20', 'revision': 0}], drew_on=list(drew_on), verbatim=True)


@pytest.mark.parametrize('how', ['its exchange', 'by ID', 'by ID, nothing else holding it'])
def test_an_edit_retires_another_members_reply_that_carried_it(world, how):  # noqa: F811
    """The issue's repro: member 30's saved reply quoted member 20's message as it first read; the message is edited,
    and member 30's next request no longer carries those words. A reply that carried Fri's reply to that one goes too."""
    client, store = world
    if how == 'by ID, nothing else holding it':
        taken = source(age=30, channel='51')['id']  # Seen in the channel, never said to Fri.
    else:
        taken, _ = learn(store, 'explain '+MARKER, age=30)
    other_reply = str(int(taken)+1)  # Fri's reply to member 30, on Discord.
    turn(store, 'what did they mean', drew_on=[taken], reply_id=other_reply, age=20)
    turn(store, 'and that?', user='40', drew_on=[other_reply], age=10)
    if how == 'its exchange':
        withdraw(client, taken)  # An edit: no `deleted`.
    else:
        withdraw_saved(client, taken)
    assert replies(store) == [controller.DRAWN] and replies(store, '40') == [controller.DRAWN]
    assert MARKER not in json.dumps(store.chat_context('123', 'public:51', '30'))


@pytest.mark.parametrize('how', ['its exchange', 'by ID'])
def test_a_reply_that_carried_it_saved_after_the_edit_keeps_nothing(world, how):  # noqa: F811
    """A save landing after the edit, from a turn that began before it (its model request read the old words): it keeps
    nothing, as one after a deletion doesn't. The check before it's delivered stays as it was: Fri holds back a reply
    whose message was edited while it was written, by its own check."""
    client, store = world
    taken, _ = learn(store, 'explain '+MARKER, age=30)
    if how == 'its exchange':
        withdraw(client, taken)
    else:
        withdraw_saved(client, taken)
    assert store.check_chat('30', 0, [{'user_id': '20', 'revision': 0}], guild_id='123', drew_on=[taken]) == {'ok': True}
    assert reply_to(store, [taken])['kept'] is False and replies(store) == []


@pytest.mark.parametrize('how', ['its exchange', 'by ID'])
def test_a_reply_from_a_turn_that_began_well_after_the_edit_saves(world, how):  # noqa: F811
    """Its model request read the message as it is now: saved as any reply is."""
    client, store = world
    taken, _ = learn(store, 'explain '+MARKER, age=1200)
    if how == 'its exchange':
        withdraw(client, taken)
    else:
        withdraw_saved(client, taken)
    with store.connection() as db:  # The edit, ten minutes back.
        db.execute('UPDATE chat_turn_withdrawals SET at=?', (time.time()-600,))
        db.execute('UPDATE chat_message_fences SET at=?', (time.time()-600,))
    assert reply_to(store, [taken], age=10).get('kept', True) is True and replies(store) == ['about that: '+MARKER]


# Rollback: the live release starts on what this wrote.

LIVE = '77442dc'  # v1.0.6, the release a rollback goes back to.


def test_the_live_gene_starts_on_what_this_wrote_and_saves_as_before(world, extracted):  # noqa: F811
    """What this writes is rows of what was there: a fence by an edited message's ID alone, a breadcrumbed reply, a
    summary dropped. The live Gene, its own code, starts on that database, and saves the member's own edited message
    again as before (it reads only a deletion's fence for a save)."""
    code = extracted(LIVE)
    if code is None:
        pytest.skip(f'{LIVE} is not in this checkout')
    client, store = world
    taken, _ = learn(store, 'explain '+MARKER, age=30)
    turn(store, 'what did they mean', drew_on=[taken], age=20)
    nearby = source(age=10, channel='51')
    withdraw_saved(client, taken, nearby['id'])  # Edits: one placed, one fenced by its ID.
    assert replies(store) == [controller.DRAWN]
    script = ('import json, sys\nfrom pathlib import Path\nfrom gene.store import Store\n'
              'store = Store(Path(sys.argv[1]))\n'
              "print(json.dumps(store.save_chat('123', 'public:51', '20', 0, 'edited: hello', 'okay', 'model', 'high', "
              "source=json.loads(sys.argv[2]), provenance=[])))\n")
    done = subprocess.run([sys.executable, '-c', script, str(store.path), json.dumps(nearby)], cwd=code,
                          env={**os.environ, 'PYTHONPATH': str(code)}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout.strip().splitlines()[-1]).get('kept', True) is True
    assert replies(store, '20')[-1] == 'okay' and replies(store) == [controller.DRAWN]
