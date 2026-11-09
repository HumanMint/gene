"""HumanMint/gene#262: a learning pass Fri stops before its model is asked gives its scan reservation back, and its slice
is read again.

Each pass claims a batch, reserving that much of the day's scan allowance. When Fri stops a pass (a withdrawal touched
what it read, say: fri #385), it finishes it with an error, and the cursor stays, so the next pass reads that slice
again. Only a finish without one gave back the part it didn't scan; one with an error kept all of it, so a few stopped
passes in a row used up the day's allowance with no model request at all. Now an error finish that says it came before
the model was asked (before_model, from a Fri that knows to say so) gives it all back. Any other error keeps it, as
before, since the model may have read the batch (Sol round 1 of #264, S1: an incomplete answer after the call gave it
all back, so passes in a row sent the model more than the day's allowance). A finish without one gives back what it
didn't scan, as before.
"""
from dataclasses import replace

from gene.fri_agent_store import AgentStore, snowflake_floor
from gene.store import Store
from tests.test_fri_learning import learning  # noqa: F401 (the fixture)

STOPPED = 'A message it read was taken back; stopped before its model request.'
INCOMPLETE = 'Summary did not complete; no derived memory saved.'
USAGE = {'input_tokens': 1200, 'output_tokens': 40, 'reasoning_output_tokens': 0}  # What an incomplete answer used.


def scheduled(learning, **config):  # noqa: F811
    """New messages in #1 to read, from an hour ago."""
    clock, store, settings, _ = learning
    scheduler = AgentStore(store, replace(settings, agent_history_channels='1'))
    with store.connection() as db:
        scheduler.put(db, 'started', clock[0]-7200)
        scheduler.put(db, 'cursors', {'1': {'after_id': snowflake_floor(clock[0]-3600), 'done_at': clock[0]-3600}})
        if config:
            scheduler.put(db, 'config', {**scheduler.get(db, 'config', {}), **config})
    return scheduler


def cursors(scheduler):
    with scheduler.store.connection() as db:
        return scheduler.get(db, 'cursors')


def used(scheduler):
    with scheduler.store.connection() as db:
        return scheduler.get(db, 'budget', {}).get('used', 0)


def delivered(scheduler):
    for report in scheduler.reports():
        scheduler.delivered(report['id'])


def test_the_repro_passes_stopped_in_a_row_leave_the_allowance_intact(learning):  # noqa: F811
    """The issue: five passes in a row, each stopped after reading its whole batch and before its model was asked. Each
    kept its reservation, so the allowance shrank by a batch each time; now it's as before the first, and each read the
    same slice."""
    clock, _, _, _ = learning
    scheduler = scheduled(learning)
    before, slices = cursors(scheduler), []
    for _ in range(5):
        claim = scheduler.claim(manual=True)
        assert 'id' in claim, claim
        slices.append(claim['after_id'])
        scheduler.finish(claim['id'], [], {}, claim['limit'], None, False, {}, error=STOPPED, before_model=True)
        delivered(scheduler)
        clock[0] += 301  # Past the owner's five minutes.
    assert used(scheduler) == 0
    assert len(set(slices)) == 1 and cursors(scheduler) == before  # Not marked done: each read the slice again.


def test_the_s1_repro_a_pass_that_failed_after_its_model_was_asked_keeps_its_reservation(learning):  # noqa: F811
    """Sol round 1 of #264, S1: batches of 5, 30 a day, eight manual passes, each ended by an incomplete answer that used
    tokens, the database opened again between them. Each gave its reservation back, so 40 messages reached the model
    and the day's allowance stayed unused. Now each keeps it, as main does: the seventh is refused, with 30 used. Its
    cursor stays, so the slice is still read again once the allowance allows."""
    clock, _, settings, _ = learning
    settings = replace(settings, agent_history_channels='1')
    scheduler = scheduled(learning, history_batch=5, history_daily_messages=30)
    before, sent = cursors(scheduler), 0
    for _ in range(8):
        scheduler = AgentStore(Store(settings.db), settings)  # Opened again.
        claim = scheduler.claim(manual=True)
        if 'id' not in claim:
            break
        sent += claim['limit']
        scheduler.finish(claim['id'], [], {}, claim['limit'], None, False, dict(USAGE), error=INCOMPLETE)
        delivered(scheduler)
        clock[0] += 301
    assert (sent, used(scheduler)) == (30, 30)
    assert 'allowance is used up' in str(claim)
    assert cursors(scheduler) == before


def test_an_error_from_an_older_fri_keeps_its_reservation_as_on_main(learning):  # noqa: F811
    """An older Fri never says when it stopped: its error finish keeps the reservation, whatever the error says."""
    scheduler = scheduled(learning)
    claim = scheduler.claim(manual=True)
    scheduler.finish(claim['id'], [], {}, claim['limit'], None, False, {}, error=STOPPED)
    assert used(scheduler) == claim['limit']


def test_before_model_on_a_finish_without_an_error_changes_nothing(learning):  # noqa: F811
    scheduler = scheduled(learning)
    claim = scheduler.claim(manual=True)
    first, newest = (str(int(claim['after_id'])+n) for n in (1, 3))
    scheduler.finish(claim['id'], [], {}, 3, first, False, {}, newest_id=newest, before_model=True)
    assert used(scheduler) == 3


def test_a_pass_that_finishes_still_keeps_what_it_scanned(learning):  # noqa: F811
    clock, _, _, _ = learning
    scheduler = scheduled(learning)
    claim = scheduler.claim(manual=True)
    scheduler.finish(claim['id'], [], {}, claim['limit'], None, False, {}, error=STOPPED, before_model=True)
    delivered(scheduler)
    clock[0] += 301
    claim = scheduler.claim(manual=True)
    first, newest = (str(int(claim['after_id'])+n) for n in (1, 3))
    scheduler.finish(claim['id'], [], {}, 3, first, False, {}, newest_id=newest)
    assert used(scheduler) == 3  # What it scanned; the rest of its batch goes back, as before.


def test_the_finish_endpoint_takes_before_model_and_an_older_fri_sends_none(learning):  # noqa: F811
    """Through the API: the flag's optional, so an older Fri's finish reads as before."""
    from gene.fri_agent_api import Finish
    assert Finish(records=[], result={}, scanned=0, done=False, counters={}).before_model is False
    assert Finish(records=[], result={}, scanned=0, done=False, counters={}, before_model=True).before_model is True
