"""gene #249: a member summary whose recorded sources (fri#333) name an edited or deleted message goes, whatever else
holds the message: its exchanges may be trimmed and no note may hold it, and the summary still read it. So does a build
in flight whose claim names it. A summary with no recorded sources goes by where and when, under an exchange holding
the message, as before. An unrelated message still costs nobody their summary.
"""
import json
import time

import pytest

from gene.fri_member_summary import CLAIM, generation
from tests.test_fri_endings import source
from tests.test_member_summaries import background, build, chat, offered, shown
from tests.test_summary_sources import sources, tomorrow


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


@pytest.mark.parametrize('rebuild', ['first build', 'a rebuild', 'a rebuild, its earlier summary gone since'])
def test_a_build_in_flight_that_read_the_message_is_called_off(store, tmp_path, rebuild):
    """Its claim names the message, as an exchange it was sent or one its earlier summary drew on, though nothing else
    holds it now: it's called off, and its finish keeps nothing. That earlier summary may have gone since the claim
    (lapsed, say): the claim still carries what it read."""
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    if rebuild != 'first build':
        build(agent)
        chat(store, 'the raid went well', ago=5400)
        chat(store, 'next raid friday', ago=5400)
        tomorrow(store)
        message = sources(store)[0]['id']  # Only its earlier summary read this one.
    offered(agent)
    claim = claim_of(store)
    if rebuild == 'first build':
        message = claim['messages'][0]['id']
    elif rebuild == 'a rebuild, its earlier summary gone since':
        from gene.fri_member_summary import drop
        with store.connection() as db:
            drop(db, '123', '20')
        assert claim_of(store)
    with store.connection() as db:
        db.execute("DELETE FROM chat_turns WHERE json_extract(source,'$.id')=?", (message,))
    store.withdraw_saved('123', [message], deleted=False)
    assert claim_of(store) is None


# gene #253.

def in_flight(store, tmp_path):
    """Member 20's build claimed, reading two exchanges; the first one's message, its exchange trimmed since, and no
    note learned from it. Returns the agent, the offer and that message."""
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    offer = offered(agent)
    message = claim_of(store)['messages'][0]['id']
    with store.connection() as db:
        db.execute("DELETE FROM chat_turns WHERE json_extract(source,'$.id')=?", (message,))
    return agent, offer, message


def finished(agent, offer):
    from gene.fri_member_summary import finish
    conversations = sorted({row['conversation_id'] for row in offer['exchanges']})
    assert finish(agent, offer['id'], 'they planned a raid', 'completed', conversations, False) == {'kept': True}
    return True


@pytest.mark.parametrize('when', ['between its two reads', 'after its read'])
def test_a_build_finishing_while_an_edit_is_looked_up_doesnt_survive(store, tmp_path, monkeypatch, when):
    """gene #253 item 1: the build finishes while the withdrawal looks for a summary or a claim naming the message. Read
    apart, the summary's record before it finished and the claim after, neither named it, so the withdrawal returned
    without the write lock, and the finished summary kept the message's old words. Read as one snapshot, one of them
    names it, and the summary is taken under the write lock, as when it finishes after that read."""
    from gene import fri_member_summary
    from gene.fri_agent_store import AgentStore
    agent, offer, message = in_flight(store, tmp_path)
    ran = []
    if when == 'between its two reads':  # As main read them: the claim second, outside any transaction.
        get = AgentStore.get

        def getting(db, key, default=None):
            if key == CLAIM and not db.in_transaction and not ran:
                ran.append(finished(agent, offer))
            return get(db, key, default)
        monkeypatch.setattr(AgentStore, 'get', staticmethod(getting))
    else:  # After the lookup, before the write lock.
        naming = fri_member_summary.naming

        def looked(db, *args):
            answer = naming(db, *args)
            if not ran:
                ran.append(finished(agent, offer))
            return answer
        monkeypatch.setattr(fri_member_summary, 'naming', looked)
    store.withdraw_saved('123', [message], deleted=False)
    assert shown(store) is None and claim_of(store) is None
    assert ran or when == 'between its two reads'  # Read as one, there's no between.


def test_an_edit_under_another_server_leaves_a_claim_of_this_one(store, tmp_path):
    """gene #253 item 2: a build in the configured server claims M, and a withdrawal of M by ID comes under another
    server (the configured test one, say): its claim is left, as its saved summaries are. Under its own, it's called
    off as before."""
    _, _, message = in_flight(store, tmp_path)
    store.withdraw_saved('777', [message], deleted=False)
    assert claim_of(store) and claim_of(store)['guild_id'] == '123'
    store.withdraw_saved('123', [message], deleted=False)
    assert claim_of(store) is None


def test_a_claim_naming_no_server_is_called_off_under_any(store, tmp_path):
    """One written before claims named their server (by an older controller, in a rollback window) can't be placed:
    it's called off whichever server the withdrawal names, as before."""
    from gene.fri_agent_store import AgentStore
    _, _, message = in_flight(store, tmp_path)
    with store.connection() as db:
        record = AgentStore.get(db, CLAIM)
        record.pop('guild_id', None)
        AgentStore.put(db, CLAIM, record)
    store.withdraw_saved('777', [message], deleted=False)
    assert claim_of(store) is None


def test_a_summary_dropped_under_another_server_leaves_this_ones_claim(store, tmp_path):
    """Its member's summary under the other server goes, as it read the message there; the build in flight here, for the
    same member, stays (gene #253)."""
    from gene.fri_member_summary import drawn
    _, _, message = in_flight(store, tmp_path)
    with store.connection() as db:  # As if a summary of member 20's had been kept under server 777 too.
        db.execute("INSERT INTO fri_member_summaries VALUES ('777','20','raids',0,'[]','[\"public:7\"]',?,1)",
                   (time.time(),))
        db.execute("INSERT INTO fri_member_summary_sources VALUES ('777','20',1,?)",
                   (json.dumps([{'id': message, 'conversation_id': 'public:7', 'saved_at': 1}]),))
        assert drawn(db, '777', '20', db.execute("SELECT * FROM fri_member_summaries WHERE guild_id='777'").fetchone())
    store.withdraw_saved('777', [message], deleted=False)
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM fri_member_summaries WHERE guild_id='777'").fetchone()
    assert claim_of(store)
