"""HumanMint/gene#250: a learning report's words and card last a week, delivered or not.

Fri delivers a report to the owner after each pass; until then it's pending, and a pending report pauses learning, so the
owner doesn't lose sight of it. Only a delivered report expired: one that never reached the owner (a DM that failed,
say) kept its summary, disclosures and card for good, held back (gene #247) or not, and learning stayed paused, with no
pass to deliver it after. Now, past its week, a report keeps only a wordless breadcrumb, still pending so the owner
still hears a pass ran. It no longer pauses learning, and it goes out with the next pass's report.
"""
import pytest

from tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from tests.test_learning_held_rollback import held, legacy_words, set_aside
from tests.test_learning_report_withdrawal import SUMMARY, passed

WEEK = 7*86400
PAUSED = 'The previous learning report still needs to reach the owner.'


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
    [pending] = scheduler.reports()
    assert not legacy_words(store) and not set_aside(store)
    assert pending['id'] == report_id and pending['body'].startswith(f'Learning pass {report_id[:8]}\nChannel: <#1>\n')
    assert 'a week' in pending['body'] and 'Held back' not in pending['body']
    card = pending['card']
    assert card['expired'] and card['summary'] == '' and card['findings'] == [] and card['term'] == ''
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


def words_kept(store, report_id):
    """Whether the report's words are still stored: in its row, or set aside."""
    return legacy_words(store) or SUMMARY in set_aside(store).get(('report', report_id), {}).get('body', '')


def test_an_expired_report_goes_out_with_the_next_passs_report(learning):  # noqa: F811
    """Fri delivers the outbox after each pass. Were the expired breadcrumb all it handed over, the next pass's report
    would wait a week to pause learning and expire in its turn: both go out, oldest first."""
    clock, _, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    clock[0] += 8*86400
    claim = scheduler.claim(manual=True)
    scheduler.finish(claim['id'], [], {}, 0, None, True, {})
    assert [report['id'] for report in scheduler.reports()] == [report_id, claim['id']]
    for report in scheduler.reports():
        scheduler.delivered(report['id'])
    assert not scheduler.reports() and not scheduler.state()['report_pending']
    clock[0] += 301  # Past the owner's five minutes.
    assert 'blocked' not in scheduler.claim(manual=True)


def test_one_already_wordless_keeps_its_own_breadcrumb(learning):  # noqa: F811
    """A report its withdrawal already took, or an interrupted pass's, has no words to expire: it stays as it is."""
    clock, store, _, _ = learning
    scheduler, report_id, said, _ = passed(learning)
    store.withdraw_saved('123', [said], deleted=True)
    [taken] = scheduler.reports()
    clock[0] += 8*86400
    assert scheduler.reports() == [taken] and 'taken back' in taken['body']
