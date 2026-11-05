"""HumanMint/gene#250: a learning report's words and card last a week, delivered or not.

Fri delivers a report to the owner after each pass; until then it's pending, and a pending report pauses learning, so the
owner doesn't lose sight of it. Only a delivered report expired: one that never reached the owner (a DM that failed,
say) kept its summary, disclosures and card for good, held back (gene #247) or not, and learning stayed paused, with no
pass to deliver it after. Now, past its week, a report keeps only a wordless breadcrumb, still pending so the owner
still hears a pass ran. It no longer pauses learning, and it goes out with the next pass's report.
"""
import pytest

from tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from tests.test_learning_held_rollback import WORDS, held, legacy_words, set_aside
from tests.test_learning_report_withdrawal import SUMMARY, passed, unrecorded, unsent

WEEK = 7*86400
PAUSED = 'The previous learning report still needs to reach the owner.'
EXPIRED = 'Its findings expired before they could be delivered.'
INTERRUPTED = 'Learning pass interrupted. Reserved scan allowance retained; no automatic replay.'


def setup(learning, which):  # noqa: F811
    if which == 'held back (gene #247)':
        scheduler, report_id, before = held(learning)
    else:
        scheduler, report_id, _, _ = passed(learning)
        [before] = scheduler.reports()
    return scheduler, report_id, before


WHICH = ['as written', 'held back (gene #247)']


@pytest.mark.parametrize('which', WHICH)
@pytest.mark.parametrize('read', ['state', 'reports'])
def test_the_repro_an_undelivered_report_keeps_no_words_past_its_week(learning, which, read):  # noqa: F811
    """The issue: left undelivered for eight days, then state(): its body and card were still stored, set aside or not,
    and it still paused learning. Fri's delivery after a pass reads the outbox alone, so that expires it too."""
    clock, store, _, _ = learning
    scheduler, report_id, before = setup(learning, which)
    clock[0] += 8*86400
    if read == 'state':
        assert scheduler.state()['report_pending']  # Still to be delivered, as a breadcrumb.
        assert not legacy_words(store) and not set_aside(store)  # And the channel summary's two days are past.
    [pending] = scheduler.reports()
    assert not report_words(store, report_id)
    assert pending['id'] == report_id and pending['body'].startswith(f'Learning pass {report_id[:8]}\nChannel: <#1>\n')
    assert pending['body'].endswith(EXPIRED) and 'Held back' not in pending['body']
    card = pending['card']
    # Sol round 1's S1: the card says why it has no findings, where Fri shows a summary (fri_reports.summary).
    assert card['expired'] and card['summary'] == EXPIRED and card['findings'] == [] and card['term'] == ''
    assert 'held' not in card
    assert {key: value for key, value in card.items() if key not in {'summary', 'findings', 'term', 'expired'}} == {
        key: value for key, value in before['card'].items() if key not in {'summary', 'findings', 'term', 'held'}}
    assert 'blocked' not in scheduler.claim(manual=True)  # It no longer pauses learning.


@pytest.mark.parametrize('which', WHICH)
def test_inside_its_week_a_pending_report_still_pauses_learning_and_keeps_its_words(learning, which):  # noqa: F811
    clock, store, _, _ = learning
    scheduler, report_id, _ = setup(learning, which)
    clock[0] += WEEK-60
    scheduler.state()
    assert scheduler.claim(manual=True) == {'blocked': PAUSED}
    assert words_kept(store, report_id) and scheduler.reports()[0]['id'] == report_id


def report_words(store, report_id):
    """Whether the report's words are still stored: in its row or card, or set aside."""
    with store.connection() as db:
        row = db.execute('SELECT r.body,c.body FROM agent_reports r LEFT JOIN agent_report_cards c ON c.report_id=r.id '
                         'WHERE r.id=?', (report_id,)).fetchone()
    return (row is not None and any(words in str(tuple(row)) for words in WORDS)) or (
        ('report', report_id) in set_aside(store))


def words_kept(store, report_id):
    """Whether the report's words are still stored: in its row, or set aside."""
    return legacy_words(store) or SUMMARY in set_aside(store).get(('report', report_id), {}).get('body', '')


def sent_by_fri(scheduler, during_first=None, passes=3):
    """What Fri sends of the outbox after each pass, as main and 1.0.6 do (fri_agent._send_reports): each report one read
    hands over, in turn, marked delivered after its send. `during_first`: what happens during the first send. Returns
    each read's reports."""
    reads, sends = [], 0
    for _ in range(passes):
        reads.append(scheduler.reports())
        for report in reads[-1]:
            sends += 1
            if during_first and sends == 1:
                during_first()
            scheduler.delivered(report['id'])
    return reads


def test_r1_b1_one_report_per_read_so_a_hold_during_a_send_reaches_the_next(learning):  # noqa: F811
    """Sol round 1's B1: an interrupted pass's receipt, eight days old, ahead of a fresh report with no record of what it
    read. The outbox handed both over in one read; a withdrawal during the first send held the fresh one back, and Fri
    sent the words it already had. One report a read now, so each goes out as it is when it's read."""
    clock, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    unrecorded(store)
    with store.connection() as db:
        db.execute('INSERT INTO agent_reports VALUES (?,?,?,0)', ('e'*32, clock[0]-8*86400, INTERRUPTED))
    withdrawn = []

    def withdraw():  # A message the fresh report may have read, taken back.
        withdrawn.append(len(withdrawn))
        store.withdraw_saved('123', [unsent(scheduler, clock[0]-7200+650)])
    reads = sent_by_fri(scheduler, withdraw)
    assert withdrawn and all(len(read) <= 1 for read in reads)
    after = sum(reads, [])[1:]  # Each sent after the withdrawal.
    assert after and not any(words in str(after) for words in WORDS)


def test_a_report_in_its_week_goes_out_before_one_past_it(learning):  # noqa: F811
    """A report past its week can still be pending when the outbox is read (an older controller's, or an interrupted
    pass's receipt written after it): the one in its week goes first, so a newer report is never a read behind one past
    its week, and learning, which waits on it, resumes."""
    clock, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    with store.connection() as db:
        db.execute('INSERT INTO agent_reports VALUES (?,?,?,0)', ('e'*32, clock[0]-8*86400, INTERRUPTED))
    assert [[report['id'] for report in read] for read in sent_by_fri(scheduler)] == [[report_id], ['e'*32], []]
    clock[0] += 301
    assert 'blocked' not in scheduler.claim(manual=True)


def test_reports_past_their_week_go_with_the_next_passs_own(learning):  # noqa: F811
    """Fri reads the outbox once after each pass, and sends one report a read: one past its week ahead of each pass's
    own would keep every report a pass behind, and pausing learning. Each pass's report says how many past their week
    were still to be delivered, and those go with it: nothing of them is left to send, and its own goes out at once."""
    clock, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    with store.connection() as db:
        db.execute('INSERT INTO agent_reports VALUES (?,?,?,0)', ('e'*32, clock[0]-60, INTERRUPTED))
    clock[0] += 8*86400
    claim = scheduler.claim(manual=True)
    scheduler.finish(claim['id'], [], {}, 0, None, True, {})
    assert not report_words(store, report_id)  # At once, before anything reads the outbox.
    [[sent], []] = sent_by_fri(scheduler, passes=2)
    assert sent['id'] == claim['id'] and 'Earlier reports that expired before they could be delivered: 2' in sent['body']
    assert not report_words(store, report_id) and not scheduler.state()['report_pending']
    clock[0] += 301  # Past the owner's five minutes.
    assert 'blocked' not in scheduler.claim(manual=True)


@pytest.mark.parametrize('which', ['taken back', 'interrupted'])
def test_one_already_wordless_keeps_its_own_breadcrumb(learning, which):  # noqa: F811
    """A report its withdrawal already took, or an interrupted pass's, has no words to expire: it stays as it is."""
    clock, store, _, scheduler = learning
    if which == 'taken back':
        scheduler, _, said, _ = passed(learning)
        store.withdraw_saved('123', [said], deleted=True)
    else:
        with store.connection() as db:
            scheduler.put(db, 'learning_claim', {'id': 'c'*32, 'at': clock[0]-3*86400, 'limit': 1})
        assert 'interrupted' in scheduler.claim(manual=True)['blocked']
    [report] = scheduler.reports()
    clock[0] += 8*86400
    assert scheduler.reports() == [report] and which in report['body']


def test_a_report_past_its_week_holds_nothing_of_its_channel_and_a_held_one_still_does(learning):  # noqa: F811
    """Fri checks the channels the controller holds anything from for a deletion it may have missed (gene #243). A
    report held back still holds its words, set aside; one past its week holds none."""
    from gene.fri_channel_withdrawal import held as holding
    clock, store, _, _ = learning
    scheduler, report_id, _ = held(learning)
    with store.connection() as db:  # Nothing else of #1: its note and the channel summary.
        db.execute('DELETE FROM personal_notes')
        db.execute("DELETE FROM agent_summaries WHERE channel_id='1'")
    assert '1' in [channel['id'] for channel in holding(store, '123')['channels']]
    clock[0] += 8*86400
    scheduler.reports()
    assert '1' not in [channel['id'] for channel in holding(store, '123')['channels']]
