"""HumanMint/gene#262: a learning pass Fri stops gives its scan reservation back, and its slice is read again.

Each pass claims a batch, reserving that much of the day's scan allowance. When Fri stops a pass (a withdrawal touched
what it read, say: fri #385), it finishes it with an error, and the cursor stays, so the next pass reads that slice
again. Only a finish without one gave back the part it didn't scan; one with an error kept all of it, so a few stopped
passes in a row used up the day's allowance with no model request at all. Now an error finish gives it all back, and a
finish without one gives back what it didn't scan, as before.
"""
from dataclasses import replace

from gene.fri_agent_store import AgentStore
from tests.test_fri_learning import learning  # noqa: F401 (the fixture)

STOPPED = 'A message it read was taken back; stopped before its model request.'


def scheduled(learning):  # noqa: F811
    clock, store, settings, _ = learning
    scheduler = AgentStore(store, replace(settings, agent_history_channels='1'))
    with store.connection() as db:
        scheduler.put(db, 'started', clock[0]-7200)
        scheduler.put(db, 'cursors', {'1': {'after_id': '100', 'done_at': clock[0]-3600}})
    return scheduler


def used(scheduler):
    with scheduler.store.connection() as db:
        return scheduler.get(db, 'budget', {}).get('used', 0)


def delivered(scheduler):
    for report in scheduler.reports():
        scheduler.delivered(report['id'])


def test_the_repro_passes_stopped_in_a_row_leave_the_allowance_intact(learning):  # noqa: F811
    """The issue: five passes in a row, each stopped after reading its whole batch. Each kept its reservation, so the
    allowance shrank by a batch each time; now it's as before the first, and each read the same slice."""
    clock, _, _, _ = learning
    scheduler = scheduled(learning)
    slices = []
    for _ in range(5):
        claim = scheduler.claim(manual=True)
        assert 'id' in claim, claim
        slices.append(claim['after_id'])
        scheduler.finish(claim['id'], [], {}, claim['limit'], None, False, {}, error=STOPPED)
        delivered(scheduler)
        clock[0] += 301  # Past the owner's five minutes.
    assert used(scheduler) == 0
    assert len(set(slices)) == 1  # Not marked done: each read the slice again.


def test_a_pass_that_finishes_still_keeps_what_it_scanned(learning):  # noqa: F811
    clock, _, _, _ = learning
    scheduler = scheduled(learning)
    claim = scheduler.claim(manual=True)
    scheduler.finish(claim['id'], [], {}, claim['limit'], None, False, {}, error=STOPPED)
    delivered(scheduler)
    clock[0] += 301
    claim = scheduler.claim(manual=True)
    first, newest = (str(int(claim['after_id'])+n) for n in (1, 3))
    scheduler.finish(claim['id'], [], {}, 3, first, False, {}, newest_id=newest)
    assert used(scheduler) == 3  # What it scanned; the rest of its batch goes back, as before.
