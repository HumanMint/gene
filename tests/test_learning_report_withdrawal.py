"""HumanMint/gene#244: a learning report, its card and the channel summary keep nothing from a message taken back.

A learning pass in #1 reads two messages of member 11's, saves a stated preference from the first and summarizes both.
Its report (the summary and the exact disclosure line), the report's card and the channel's summary then reach the
owner's DM, the owner's learning tool and Fri's context in that channel. Once a message the pass read is withdrawn
(edited or deleted, by ID or by its exchange's scope), none of that may keep its words: the report and card keep a
wordless breadcrumb, delivered or not, and the channel summary goes. One that read neither stays.

Reports and summaries from before this (or saved since by an older controller) don't say what they read: a report goes
to its breadcrumb if the message was sent within the range it examined, and a summary goes if the message was sent
before it was made.
"""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_agent_store import AgentStore, snowflake_floor
from tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from tests.test_saved_withdrawal import withdraw_saved

LIKE, OTHER, SUMMARY = 'I love trance', 'see you at the rave at nine', 'They planned a trance night'


def passed(learning):  # noqa: F811
    """A finished pass over #1: member 11's two messages, a preference saved from the first, both summarized."""
    clock, store, settings, _ = learning
    settings = replace(settings, agent_history_channels='1')
    scheduler, started = AgentStore(store, settings), clock[0]-7200
    with store.connection() as db:
        scheduler.put(db, 'started', started)
        scheduler.put(db, 'cursors', {'1': {'before': '100', 'done_at': clock[0]-3600}})
    claim = scheduler.claim()
    said, other = (str(int(snowflake_floor(started+offset))+1) for offset in (600, 700))
    records = [{'id': said, 'user_id': '11', 'role_id': '90', 'revision': 0, 'text': LIKE, 'at': started+600},
               {'id': other, 'user_id': '11', 'role_id': '90', 'revision': 0, 'text': OTHER, 'at': started+700}]
    result = {'summary': SUMMARY, 'notes': [{'source_id': said, 'category': 'music', 'evidence': LIKE}]}
    scheduler.finish(claim['id'], records, result, 2, said, True, {}, newest_id=other)
    [report] = scheduler.reports()
    assert SUMMARY in report['body'] and f'11 / music: {LIKE}' in report['body']
    assert report['card']['summary'] == SUMMARY
    assert scheduler.context('1', '11')['summary'] == SUMMARY
    return scheduler, report['id'], said, other


def unsent(scheduler, at):
    """A message ID sent at `at` (Discord's time) that the pass never read."""
    return str(int(snowflake_floor(at))+7)


def kept_words(scheduler, report_id):
    """Whether any of the pass's words are still kept: its report (pending or not), card, channel summary, or the
    learning tool's evidence."""
    report = scheduler.report(report_id)
    overview = scheduler.overview()
    text = str([report, overview['reports'], overview['summaries'], scheduler.context('1', '11')['summary']])
    return any(words in text for words in (LIKE, SUMMARY, 'trance'))


def withdraw(store, settings, message, how):
    if how == 'by ID, over the API':
        assert withdraw_saved(TestClient(create_app(settings)), message).status_code == 200
    elif how in ('deleted by ID', 'edited by ID'):
        store.withdraw_saved('123', [message], deleted=how.startswith('deleted'))
    else:  # Its exchange's scope, as Fri withdraws one it saved.
        store.withdraw_turn('123', 'public:1', '11', message, deleted=False)


HOWS = ['by ID, over the API', 'deleted by ID', 'edited by ID', 'by its scope']


@pytest.mark.parametrize('how', HOWS)
@pytest.mark.parametrize('which', ['the disclosure', 'another message the pass read'])
def test_the_repro_a_withdrawn_message_leaves_no_words_in_the_report_its_card_or_the_summary(learning, how, which):  # noqa: F811
    """The issue: the message withdrawn through the existing API, and reports() still returned the summary and the
    exact disclosure line, ready for delivery. The other message holds no note, so on main nothing was even looked at."""
    _, store, settings, _ = learning
    scheduler, report_id, said, other = passed(learning)
    withdraw(store, settings, said if which == 'the disclosure' else other, how)
    assert not kept_words(scheduler, report_id)
    [pending] = scheduler.reports()
    assert pending['id'] == report_id  # Still delivered, as a breadcrumb: the owner sees the pass ran.
    assert 'taken back' in pending['body'] and pending['body'].startswith('Learning pass ')
    assert pending['card']['summary'] == '' and pending['card']['findings'] == [] and pending['card']['withdrawn']
    assert pending['card']['channel_id'] == '1'


def test_a_delivered_report_keeps_only_its_breadcrumb(learning):  # noqa: F811
    _, store, settings, _ = learning
    scheduler, report_id, said, _ = passed(learning)
    scheduler.delivered(report_id)
    store.withdraw_saved('123', [said], deleted=True)
    assert not kept_words(scheduler, report_id) and 'taken back' in scheduler.report(report_id)['body']


def test_a_message_the_pass_never_read_leaves_everything(learning):  # noqa: F811
    clock, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    for how in (True, False):
        store.withdraw_saved('123', [unsent(scheduler, clock[0]-7200+650)], deleted=how)
    assert kept_words(scheduler, report_id)
    assert scheduler.reports()[0]['card']['summary'] == SUMMARY


def unrecorded(store):
    """As a report and summary from before this, or saved by an older controller since: nothing says what they read."""
    with store.connection() as db:
        db.execute('DELETE FROM fri_learning_sources')


@pytest.mark.parametrize('when', ['within the range the report examined', 'after it, before the summary was made',
                                  'after the summary was made'])
def test_an_older_report_and_summary_are_held_where_they_may_have_read_it(learning, when):  # noqa: F811
    """By ID, with no channel to go by (see Sol round 1's B1 below): held back, not shown or used, where it may have
    read the message."""
    clock, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    unrecorded(store)
    started = clock[0]-7200
    at = {'within the range the report examined': started+650, 'after it, before the summary was made': started+3600,
          'after the summary was made': clock[0]+60}[when]
    store.withdraw_saved('123', [unsent(scheduler, at)])
    report = scheduler.report(report_id)
    assert (SUMMARY in report['body']) is (when != 'within the range the report examined')
    assert (scheduler.context('1', '11')['summary'] == SUMMARY) is (when == 'after the summary was made')
    assert stored(store) == (True, True)  # Held, never destroyed.


def stored(store):
    """Whether the report's words and the channel summary are still in the database, shown or not: in their rows, or
    set aside where they're held (gene #252)."""
    with store.connection() as db:
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN "
                                               "('agent_reports','fri_learning_held')")]
        return (SUMMARY in str([tuple(row) for table in tables for row in db.execute(f'SELECT * FROM {table}')]),
                db.execute("SELECT 1 FROM agent_summaries WHERE channel_id='1'").fetchone() is not None)


WHERES = ['another channel, by its scope', 'a DM, by its scope', 'the same channel, by its scope', 'by ID',
          'by ID, over the API']


@pytest.mark.parametrize('where', WHERES)
def test_r1_b1_an_older_report_is_never_destroyed_for_a_message_it_may_not_have_read(learning, where):  # noqa: F811
    """Sol round 1's B1: the deployed controller's pass over #1 read messages at T and T+100, and records nothing of
    them. Withdrawing another message, unread, sent at T+50 in #2, in a DM, or by ID, matched its send time alone,
    and breadcrumbed the report and deleted the summary for good. Main keeps both. Now one with no record of what it
    read is only held back (not shown, used or delivered with its words), and only where the message may have been
    said: by its scope, in that channel alone; by ID, with no channel to go by, wherever its time falls in range."""
    clock, store, settings, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    unrecorded(store)
    message = unsent(scheduler, clock[0]-7200+650)
    if where.startswith('by ID'):
        withdraw(store, settings, message, 'deleted by ID' if where == 'by ID' else 'by ID, over the API')
    else:
        conversation = {'another channel, by its scope': 'public:2', 'a DM, by its scope': 'private:52',
                        'the same channel, by its scope': 'public:1'}[where]
        store.withdraw_turn('123', conversation, '11', message, deleted=True)
    assert stored(store) == (True, True)
    if where.startswith(('another', 'a DM')):
        assert kept_words(scheduler, report_id)
    else:
        assert not kept_words(scheduler, report_id)
        [pending] = scheduler.reports()
        assert 'Held back' in pending['body'] and pending['card']['summary'] == '' and pending['card']['held']


@pytest.mark.parametrize('how', HOWS)
def test_r1_b2_an_archive_observation_goes_with_its_source(learning, how):  # noqa: F811
    """Sol round 1's B2 (pre-existing): an archive search's observation kept its source's exact words after that
    message was withdrawn, for the owner's learning tool (overview's historical notes) and the channel's historical
    topics. It goes, in the same transaction. Another message leaves it."""
    from tests.test_fri_reports import complete
    clock, store, settings, scheduler, _ = complete(learning)

    def observed():
        with store.connection() as db:
            return [row[0] for row in db.execute('SELECT source_id FROM fri_explore_notes')]
    [source] = observed()
    assert scheduler.overview()['historical_notes'] and scheduler.context('1', '11')['historical_topics']
    withdraw(store, settings, unsent(scheduler, clock[0]-86400+30), 'deleted by ID')
    assert observed() == [source]
    withdraw(store, settings, source, how)
    assert observed() == []
    assert not scheduler.overview()['historical_notes'] and not scheduler.context('1', '11')['historical_topics']


def test_an_interrupted_report_has_no_words_to_take(learning):  # noqa: F811
    clock, store, settings, scheduler = learning
    with store.connection() as db:
        scheduler.put(db, 'learning_claim', {'id': 'c'*32, 'at': clock[0]-3*86400, 'limit': 1})
    assert 'interrupted' in scheduler.claim(manual=True)['blocked']
    [report] = scheduler.reports()
    assert 'interrupted' in report['body']
    store.withdraw_saved('123', [unsent(scheduler, clock[0]-3*86400)], deleted=True)
    assert scheduler.reports() == [report]


@pytest.mark.parametrize('channel', ['1', '2'])
def test_a_withdrawn_channel_takes_its_reports_and_summary(learning, channel):  # noqa: F811
    """For channel and thread withdrawal (gene #243): the same cleanup, by channel."""
    from gene.fri_learning_sources import withdraw as withdraw_learning
    _, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    with store.connection() as db:
        withdraw_learning(db, channels=[channel])
    assert kept_words(scheduler, report_id) is (channel == '2')


def sources(store):
    with store.connection() as db:
        return sorted(tuple(row) for row in db.execute('SELECT kind,item FROM fri_learning_sources'))


def test_what_each_read_goes_with_it_whichever_controller_changes_it(learning):  # noqa: F811
    """An erasure (which removes every report) and an older controller's next summary of the channel take the record
    of what they read with them, so a later report or summary is never matched against another's."""
    _, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    assert sources(store) == [('report', report_id), ('summary', '1')]
    with store.connection() as db:  # An older controller's next summary of #1.
        db.execute("INSERT INTO agent_summaries(channel_id,body,authors,at,revisions) VALUES ('1','x','[]',1,'{}') "
                   'ON CONFLICT(channel_id) DO UPDATE SET body=excluded.body,at=excluded.at')
    assert sources(store) == [('report', report_id)]
    store.personal_control('11', 'disable')
    assert sources(store) == []
