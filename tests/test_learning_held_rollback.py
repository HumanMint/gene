"""HumanMint/gene#252: a learning report or channel summary held back (gene #247) keeps no words where an older
controller reads it.

#247 holds a report or summary that has no record of what it read (from before it, or saved by an older controller since)
where a message taken back may have been one it read: it's shown, delivered and used only as a wordless placeholder, its
words kept until it expires or is replaced. It held one by its mark alone, in a table an older controller doesn't know,
so after a rollback the deployed release's reports API, the owner's learning tool and Fri's context returned the words
again. Now the words move to a table of their own, and the tables every controller reads hold the placeholder: the
report's breadcrumb, its card without its summary, findings or term, and a summary with no words or revisions, which an
older controller counts as stale. The words go with the report or summary, by triggers, whichever controller removes or
replaces it, and with an erasure.
"""
import json
import os
import subprocess
import sys
import threading
from time import time as real_time

import pytest

from gene.fri_learning_sources import withdraw as withdraw_learning
from gene.store import Store
from tests.test_erasure_rollback import extracted  # noqa: F401 (the fixture)
from tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from tests.test_learning_report_withdrawal import (
    LIKE,
    SUMMARY,
    kept_words,
    passed,
    unrecorded,
    unsent,
    withdraw,
)

DEPLOYED = '77442dc'  # v1.0.6, the release a rollback goes back to.
BEFORE = '016867c'  # Gene main before this, with #247: it held by the mark alone.
WORDS = (LIKE, SUMMARY, 'trance')


def legacy_words(store):
    """Whether any of the pass's words are in the tables every controller reads."""
    with store.connection() as db:
        text = str([tuple(row) for table in ('agent_reports', 'agent_report_cards', 'agent_summaries')
                    for row in db.execute(f'SELECT * FROM {table}')])
    return any(words in text for words in WORDS)


def set_aside(store):
    """The words held back, by report or summary."""
    with store.connection() as db:
        return {(row['kind'], row['item']): json.loads(row['kept'])
                for row in db.execute('SELECT kind,item,kept FROM fri_learning_held')}


def held(learning, how='deleted by ID'):  # noqa: F811
    """#247's case: a report and summary with no record of what they read, held for a message sent within the range the
    report examined. Returns the pass's scheduler and report, and the report as it was before."""
    clock, store, settings, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    [before] = scheduler.reports()
    unrecorded(store)
    message = unsent(scheduler, clock[0]-7200+650)
    if how == 'by its scope, in the same channel':
        store.withdraw_turn('123', 'public:1', '11', message, deleted=True)
    else:
        withdraw(store, settings, message, how)
    return scheduler, report_id, before


HOWS = ['deleted by ID', 'edited by ID', 'by ID, over the API', 'by its scope, in the same channel']


@pytest.mark.parametrize('how', HOWS)
def test_the_repro_a_held_report_and_summary_keep_no_words_where_an_older_controller_reads(learning, how):  # noqa: F811
    """The issue: held by the mark alone, the report's summary and disclosure, its card's summary and the channel summary
    stayed where they were. Now they're set aside, never destroyed, and this Gene shows the placeholder as before."""
    _, store, _, _ = learning
    scheduler, report_id, _ = held(learning, how)
    assert not legacy_words(store)
    kept = set_aside(store)
    assert set(kept) == {('report', report_id), ('summary', '1')}
    assert SUMMARY in kept['report', report_id]['body'] and LIKE in kept['report', report_id]['body']
    assert json.loads(kept['report', report_id]['card'])['summary'] == SUMMARY
    assert kept['summary', '1']['body'] == SUMMARY and json.loads(kept['summary', '1']['revisions']) == {'11': 0}
    assert not kept_words(scheduler, report_id)
    [pending] = scheduler.reports()
    assert pending['id'] == report_id and 'Held back' in pending['body'] and pending['body'].startswith('Learning pass ')
    assert pending['card']['held'] and pending['card']['summary'] == '' and pending['card']['findings'] == []


def test_a_held_cards_counts_and_fields_stay_for_fri_to_render(learning):  # noqa: F811
    """Fri's report card reads its counts, channel, range and next check (fri_reports.summary): they all stay."""
    scheduler, _, before = held(learning)
    [after] = scheduler.reports()
    words = {'summary', 'findings', 'term'}
    assert {key: value for key, value in after['card'].items() if key not in words | {'held'}} == {
        key: value for key, value in before['card'].items() if key not in words}


READ = '''
import json, sys, time
real = time.time
time.time = lambda: real()+float(sys.argv[2])
from pathlib import Path
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
settings = Settings(Path(sys.argv[1]), '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32,
                    workshop_channel_id='456', fri_token='fri-'+'x'*32, agent_channels='1,2',
                    agent_history_channels='1', agent_role_id='90')
client, headers = TestClient(create_app(settings)), {'Authorization': 'Bearer fri-'+'x'*32}
owner = {'guild_id': '123', 'user_id': '10'}
read = {'pending': client.get('/agent/reports', headers=headers),
        'view': client.post('/agent/reports/'+sys.argv[3]+'/view', headers=headers, json=owner),
        'learning tool': client.post('/agent/learning/overview', headers=headers, json=owner),
        'context': client.post('/agent/context', headers=headers,
                               json={'guild_id': '123', 'user_id': '11', 'channel_id': '1'})}
print(json.dumps({name: [response.status_code, response.json()] for name, response in read.items()}))
'''


def as_deployed(code, learning, report_id):  # noqa: F811
    """What the deployed release's own readers return on this database, on the fixture's clock: its reports API (the
    pending outbox and a report's view), the owner's learning tool and Fri's context in #1."""
    clock, store, _, _ = learning
    done = subprocess.run([sys.executable, '-c', READ, str(store.path), str(clock[0]-real_time()), report_id],
                          cwd=code, env={**os.environ, 'PYTHONPATH': str(code)}, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_deployed_genes_readers_show_no_held_words_and_this_one_shows_the_placeholder_again(learning, extracted):  # noqa: F811
    """The issue's steps, with 77442dc's own code: this Gene holds the report and summary; the deployed release, started
    on the same database, returns none of their words from its reports API, the learning tool or context, and drops the
    summary as stale, its words with it; this Gene, started again, shows the report's placeholder, its words still set
    aside."""
    code = extracted(DEPLOYED)
    if code is None:
        pytest.skip(f'{DEPLOYED} is not in this checkout')
    _, store, _, _ = learning
    scheduler, report_id, _ = held(learning)
    read = as_deployed(code, learning, report_id)
    assert all(status == 200 for status, _ in read.values()), read
    assert not any(words in json.dumps(read) for words in WORDS), read
    assert 'Held back' in read['view'][1]['body'] and read['pending'][1][0]['card']['summary'] == ''
    assert read['context'][1]['summary'] == '' and read['learning tool'][1]['summaries'] == []
    assert set(set_aside(store)) == {('report', report_id)}  # Its context read dropped the stale summary, words too.
    Store(store.path)  # This Gene starts again.
    assert not legacy_words(store) and not kept_words(scheduler, report_id)
    [pending] = scheduler.reports()
    assert 'Held back' in pending['body'] and pending['card']['held']
    assert SUMMARY in set_aside(store)['report', report_id]['body']


HOLD = '''
import sys
from pathlib import Path
from gene.store import Store
Store(Path(sys.argv[1])).withdraw_saved('123', [sys.argv[2]])
'''


@pytest.mark.parametrize('how', ['its own code', 'its statements'])
def test_one_held_by_the_mark_alone_has_its_words_set_aside_at_this_genes_next_start(learning, extracted, how):  # noqa: F811
    """Gene main before this (016867c) held them by the mark alone, as a release with it would after a rollback from
    this one: started again, this Gene sets their words aside."""
    clock, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    unrecorded(store)
    message = unsent(scheduler, clock[0]-7200+650)
    if how == 'its own code':
        code = extracted(BEFORE)
        if code is None:
            pytest.skip(f'{BEFORE} is not in this checkout')
        done = subprocess.run([sys.executable, '-c', HOLD, str(store.path), message], cwd=code,
                              env={**os.environ, 'PYTHONPATH': str(code)}, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr[-2000:]
    else:
        with store.connection() as db:  # 016867c's hold(), for each.
            for kind, item in (('report', report_id), ('summary', '1')):
                db.execute('INSERT INTO fri_learning_sources VALUES (?,?,NULL,1) ON CONFLICT(kind, item) DO UPDATE SET '
                           'held=1', (kind, item))
    assert legacy_words(store)
    Store(store.path)
    assert not legacy_words(store) and set(set_aside(store)) == {('report', report_id), ('summary', '1')}
    assert not kept_words(scheduler, report_id) and 'Held back' in scheduler.reports()[0]['body']
    Store(store.path)  # Again: nothing more moves, and what's set aside is still the words.
    assert SUMMARY in set_aside(store)['report', report_id]['body']
    assert set_aside(store)['summary', '1']['body'] == SUMMARY


def test_two_starts_at_once_set_the_words_aside_once(learning, monkeypatch):  # noqa: F811
    """Two processes starting on one database together: the second waits while the first sets the words aside. Were it
    not to, it would set them aside first, and the first, reading the placeholder its summary had become, would put that
    in their place."""
    import gene.fri_learning_sources as sources
    _, store, _, _ = learning
    scheduler, report_id, _, _ = passed(learning)
    unrecorded(store)
    with store.connection() as db:  # 016867c's hold(), for each.
        for kind, item in (('report', report_id), ('summary', '1')):
            db.execute('INSERT INTO fri_learning_sources VALUES (?,?,NULL,1)', (kind, item))
    hold, other = sources.hold, []

    def first(db, kind, item):
        if not other:  # The other start, while this one sets aside, for as long as it may take.
            other.append(threading.Thread(target=Store, args=(store.path,)))
            other[0].start()
            other[0].join(2)
        hold(db, kind, item)
    monkeypatch.setattr(sources, 'hold', first)
    Store(store.path)
    other[0].join()
    assert set_aside(store)['summary', '1']['body'] == SUMMARY
    assert SUMMARY in set_aside(store)['report', report_id]['body'] and not legacy_words(store)


def older_summary(store):
    """The deployed release's next summary of #1 (its finish, 77442dc), over the placeholder."""
    with store.connection() as db:
        db.execute('INSERT INTO agent_summaries(channel_id,body,authors,at,revisions) VALUES (?,?,?,?,?) '
                   'ON CONFLICT(channel_id) DO UPDATE SET body=excluded.body,authors=excluded.authors,'
                   'at=excluded.at,revisions=excluded.revisions', ('1', 'They talked about lunch', '["12"]', 1, '{"12": 0}'))


def cleared(what, clock, store, scheduler, report_id):
    if what == 'memory turned off':
        store.personal_control('11', 'disable')
    elif what == 'chats cleared':
        store.clear_chats('11')
    elif what == "an older controller's next summary":
        older_summary(store)
    elif what == 'the channel withdrawn':
        with store.connection() as db:
            withdraw_learning(db, channels=['1'])
    else:
        if what == "a delivered report's week":
            scheduler.delivered(report_id)
        clock[0] += 2*86400+1 if what == "the summary's two days" else 7*86400+1
        scheduler.state()


CLEARED = ['memory turned off', 'chats cleared', "the summary's two days", "a delivered report's week",
           "an undelivered report's week (gene #250)", "an older controller's next summary", 'the channel withdrawn']
LEFT = {"the summary's two days": {'report'}, "an older controller's next summary": {'report'}}


@pytest.mark.parametrize('what', CLEARED)
def test_what_is_set_aside_goes_with_what_it_was_for(learning, what):  # noqa: F811
    """Erasure, expiry, a replaced summary and a withdrawn channel take both copies: what an older controller reads and
    what's set aside."""
    clock, store, _, _ = learning
    scheduler, report_id, _ = held(learning)
    cleared(what, clock, store, scheduler, report_id)
    assert {kind for kind, _ in set_aside(store)} == LEFT.get(what, set())
    assert not legacy_words(store)
