"""Turning memory off keeps one metadata-only cutoff, the time it was turned off, across turning it back on: nothing
the member said before it is learned or stamped with a later revision (HumanMint/fri#321)."""

import time
from dataclasses import replace

from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_agent_store import AgentStore, snowflake_floor
from gene.fri_memory_store import HORIZON, removed
from tests.test_api import headers
from tests.test_fri_learning import learning as learning


def profile(client, user='11'):
    """The member's fence as Fri reads it for learning (/agent/context)."""
    response = client.post('/agent/context', headers=headers('fri'), json={'guild_id': '123', 'user_id': user,
                                                                           'channel_id': '1'})
    assert response.status_code == 200, response.text
    return response.json()


def test_memory_turned_off_and_on_again_keeps_its_cutoff(learning):
    """fri #321: turning memory off erased the member's removals with everything else, so turned back on, their profile
    said enabled, a new revision and no removal cutoff, and Fri stamped their older words with that revision. The time
    it was turned off stays as their cutoff."""
    clock, store, settings, _ = learning
    client = TestClient(create_app(settings))
    before = profile(client)
    assert before['removed_at'] == 0
    store.personal_control('11', 'disable')
    turned_off = time.time()
    clock[0] += 60
    store.personal_control('11', 'enable')
    answer = profile(client)
    assert answer['enabled'] and answer['revision'] == 2
    assert answer['removed_at'] == turned_off
    assert profile(client, '14')['removed_at'] == 0  # Nobody else's.
    assert set(answer) == set(before)  # The same answer an older Fri reads, with the same fields.


def test_nothing_from_before_memory_was_turned_off_is_learned_again(learning):
    """What a learning pass finishes with is held to the same cutoff: a message from before memory was turned off,
    stamped with the revision after it was turned on again, teaches nothing. Gene's other cutoffs (see removed) are
    unchanged."""
    clock, store, settings, scheduler = learning
    started = clock[0]-7200
    scheduler = AgentStore(store, replace(settings, agent_history_channels='1'))
    with store.connection() as db:
        scheduler.put(db, 'started', started)
        scheduler.put(db, 'cursors', {'1': {'before': '100', 'done_at': clock[0]-3600}})
    claim = scheduler.claim()
    source_id = str(int(snowflake_floor(started+600))+1)
    store.personal_control('11', 'disable')
    store.personal_control('11', 'enable')
    records = [{'id': source_id, 'user_id': '11', 'role_id': '90', 'revision': 2, 'text': 'I love trance',
                'at': started+600}]
    result = {'summary': 'Music chat', 'notes': [
        {'source_id': source_id, 'category': 'music', 'evidence': 'I love trance'}]}
    saved = scheduler.finish(claim['id'], records, result, 1, source_id, False, {}, newest_id=source_id)
    assert saved['saved'] == 0 and not store.chat_context('123', 'public:1', '11')['personal_memory']['notes']
    with store.connection() as db:
        assert removed(db, '123', 'public:1', '11') is None


def test_an_erasure_cutoff_holds_no_words_and_lasts_as_long_as_archive_search_reaches(learning):
    """It's the member's ID and a time, nothing more, kept as long as a wordless removal is (HORIZON), then dropped."""
    clock, store, settings, _ = learning
    client = TestClient(create_app(settings))
    store.personal_control('11', 'disable')
    store.personal_control('11', 'enable')
    with store.connection() as db:
        assert [tuple(row) for row in db.execute('SELECT * FROM personal_erasures')] == [('11', time.time())]
    clock[0] += HORIZON-60
    store.chat_context('123', 'public:1', '11')  # Retention runs as a context is read.
    assert profile(client)['removed_at']
    clock[0] += 120
    store.chat_context('123', 'public:1', '11')
    assert profile(client)['removed_at'] == 0
