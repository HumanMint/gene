"""gene #249: a member summary whose recorded sources (fri#333) name an edited or deleted message goes, whatever else
holds the message: its exchanges may be trimmed and no note may hold it, and the summary still read it. So does a build
in flight whose claim names it. A summary with no recorded sources goes by where and when, under an exchange holding
the message, as before. An unrelated message still costs nobody their summary.
"""
import json

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
