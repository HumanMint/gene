"""gene #149 (2): a message withdrawn by its ID alone while nothing stored holds it yet still leaves a fence.

Fri asks about every edit and deletion it can't place by ID (fri #174). When nothing holds the message yet (a save that
carries it still on its way, or a learning batch reading it), the controller used to answer success and keep nothing, so
the save kept the words or the batch learned a note from them. Now a deletion keeps a fence holding only the guild, the
message ID, its time and its kind, for as long as the other fences last. So does an edit while a learning batch is in
flight, which is the only thing an edit's fence stops: a save of the member's own edited message is the rerun that
answers it as it reads now (fri #126), and Fri withdraws an old save that lands after it (fri #224). The fence names its
author when the controller knows who that is, from a ping's grant or a research permit, so their "forget me" removes it.
"""
import json
import sqlite3
import sys
import time
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from gene import store as controller
from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore
from gene.fri_member_summary import erasure_triggers
from gene.store import Conflict, Store
from tests.test_fri_endings import source
from tests.test_saved_withdrawal import withdraw_saved
from tests.test_turn_withdrawal import MARKER

LIKE = 'i like lavender bicycles'


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'fences.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', agent_history_channels='51', agent_role_id='admin',
                        test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    client.settings = settings
    return client, client.app.state.store


def reply(store, *, user='30', drew_on=(), said=None, text='what did they mean'):
    said = said or source(age=2, user=user, channel='51')
    return store.save_chat('123', 'public:51', user, 0, text, 'about that: '+MARKER, 'model', 'high', source=said,
                           provenance=[{'user_id': '20', 'revision': 0}], drew_on=list(drew_on), verbatim=True)


def replies(store, user='30'):
    return [turn['reply'] for turn in store.chat_context('123', 'public:51', user)['turns']]


def notes(store, user='20'):
    return [note['evidence'] for note in store.chat_context('123', 'public:51', user)['personal_memory']['notes']]


def fences(store):
    with store.connection() as db:
        return sorted(tuple(row) for row in db.execute(
            'SELECT message_id,deleted,user_id,revision FROM chat_message_fences'))


def batch(client, store):
    """A learning batch claimed, reading two of member 20's messages; finish() completes it with a note from each."""
    agent = AgentStore(store, client.settings)
    agent.update({'enabled': True}, 0, '9')
    claim = agent.claim()
    taken, kept = source(age=200, channel='51'), source(age=190, channel='51')
    records = [{'id': taken['id'], 'user_id': '20', 'role_id': 'admin', 'revision': 0, 'text': LIKE,
                'at': time.time()-200},
               {'id': kept['id'], 'user_id': '20', 'role_id': 'admin', 'revision': 0,
                'text': 'i play chess on weekends', 'at': time.time()-190}]
    def finish():
        agent.finish(claim['id'], records, {'summary': 'chat', 'notes': [
            {'source_id': taken['id'], 'category': 'hobbies', 'evidence': LIKE},
            {'source_id': kept['id'], 'category': 'games', 'evidence': 'i play chess on weekends'}]},
            len(records), '100', False, {'input_tokens': 1, 'output_tokens': 1})
    return taken['id'], finish, claim


# The issue's orderings.

def test_a_reply_that_carried_a_nearby_message_deleted_before_its_save_landed_keeps_nothing(world):
    """Another member's message, in view for member 30's turn, deleted while that turn's save is on its way: nothing
    holds it yet, but the save that lands after keeps nothing, and a check of a turn carrying it refuses."""
    client, store = world
    nearby = source(age=30, channel='51')['id']
    assert withdraw_saved(client, nearby, deleted=True).json() == {'ok': True, 'removed': 0}
    with pytest.raises(Conflict):
        store.check_chat('30', 0, [{'user_id': '20', 'revision': 0}], guild_id='123', drew_on=[nearby])
    assert reply(store, drew_on=[nearby])['kept'] is False
    assert replies(store) == []


def test_a_members_own_message_deleted_before_its_save_landed_keeps_nothing(world):
    client, store = world
    said = source(age=5, channel='51')
    withdraw_saved(client, said['id'], deleted=True)
    with pytest.raises(Conflict):
        store.check_chat('20', 0, [], guild_id='123', messages=[said['id']])
    result = store.save_chat('123', 'public:51', '20', 0, LIKE, 'okay', 'model', 'high', source=said, provenance=[],
                             notes=[{'category': 'hobbies', 'evidence': LIKE}])
    assert result['kept'] is False and notes(store) == [] and replies(store, '20') == []


@pytest.mark.parametrize('deleted', [True, False], ids=['deleted', 'edited'])
def test_a_learning_batch_reading_a_message_withdrawn_by_id_learns_nothing_from_it(world, deleted):
    client, store = world
    taken, finish, _ = batch(client, store)
    assert withdraw_saved(client, taken, deleted=deleted).json() == {'ok': True, 'removed': 0}  # While it reads.
    finish()
    assert notes(store) == ['i play chess on weekends']


# What the fence doesn't do.

def test_an_edit_leaves_the_rerun_that_answers_it_as_it_reads_now(world):
    """An edit's fence never stops a save or check of the member's own message: that's Fri answering it again as
    edited, under the same ID. Not while a learning batch reads, either."""
    client, store = world
    said = source(age=5, channel='51')
    _, finish, _ = batch(client, store)  # In flight, so the edit leaves a fence.
    withdraw_saved(client, said['id'])
    assert [row[:2] for row in fences(store)] == [(said['id'], 0)]
    assert store.check_chat('20', 0, [], guild_id='123', messages=[said['id']]) == {'ok': True}
    result = store.save_chat('123', 'public:51', '20', 0, 'edited: '+LIKE, 'okay', 'model', 'high', source=said,
                             provenance=[])
    assert result.get('kept', True) is True and replies(store, '20') == ['okay']
    finish()


def test_an_edit_leaves_other_members_replies_that_carry_it(world):
    """Only a deletion retires a reply that carried the message (gene #150)."""
    client, store = world
    nearby = source(age=30, channel='51')['id']
    _, finish, _ = batch(client, store)
    withdraw_saved(client, nearby)
    assert reply(store, drew_on=[nearby]).get('kept', True) is True and replies(store) == ['about that: '+MARKER]
    finish()


def test_an_edit_with_no_learning_batch_in_flight_keeps_nothing(world):
    """Nothing could still learn from its old words, so nothing is kept for it, even beside one whose exchange goes."""
    client, store = world
    withdraw_saved(client, source(age=30, channel='51')['id'])
    assert fences(store) == []
    said = source(age=20, channel='51')
    store.save_chat('123', 'public:51', '20', 0, 'explain '+MARKER, 'okay', 'model', 'high', source=said,
                    provenance=[])
    assert withdraw_saved(client, said['id'], source(age=10, channel='51')['id']).json()['removed'] == 1
    assert fences(store) == []


def test_a_batch_claimed_after_a_fence_learns_from_the_message_as_it_reads_now(world, monkeypatch):
    """An edit's fence stops only a batch that was already reading: a later batch reads the edited words."""
    client, store = world
    made = [source(age=200, channel='51'), source(age=190, channel='51')]  # What batch() reads, made up front.
    with store.connection() as db:  # A fence from before this batch began, written before it was claimed.
        db.execute('INSERT INTO chat_message_fences(guild_id,message_id,at,deleted) VALUES (?,?,?,0)',
                   ('123', made[0]['id'], time.time()-1))
    monkeypatch.setattr(sys.modules[__name__], 'source', lambda **_: made.pop(0))
    _, finish, _ = batch(client, store)
    finish()
    assert sorted(notes(store)) == sorted([LIKE, 'i play chess on weekends'])


def test_a_message_something_holds_is_fenced_as_before(world):
    """Guard: a deletion that found the member's exchange fences it under them, as gene #145 does, and keeps no
    separate fence."""
    client, store = world
    said = source(age=30, channel='51')
    store.save_chat('123', 'public:51', '20', 0, 'explain '+MARKER, 'okay', 'model', 'high', source=said,
                    provenance=[])
    assert withdraw_saved(client, said['id'], deleted=True).json()['removed'] == 1
    assert fences(store) == []
    with store.connection() as db:
        assert [tuple(row) for row in db.execute('SELECT user_id FROM chat_turn_deletions')] == [('20',)]


def test_an_ordinary_turn_saves_as_before(world):
    """Guard: a fence on one message doesn't touch a turn that carried another."""
    client, store = world
    withdraw_saved(client, source(age=30, channel='51')['id'], deleted=True)
    other = source(age=40, channel='51')['id']
    assert store.check_chat('30', 0, [{'user_id': '20', 'revision': 0}], guild_id='123', drew_on=[other]) == \
        {'ok': True}
    assert reply(store, drew_on=[other]).get('kept', True) is True


# Whose it is, and how long it lasts.

def permit(store, message, user='20'):
    """A research permit for the turn answering `message`, as fri_research.start writes one."""
    with store.connection() as db:
        db.execute('INSERT INTO web_requests VALUES (?,?,?,?,?,?,?)',
                   ('123', 'public:51', user, message, 0, time.time(), 'lavender bicycles'))


def test_a_fence_names_its_author_when_known_and_their_forget_me_takes_it(world):
    client, store = world
    mine, theirs, nobodys = (source(age=age, channel='51')['id'] for age in (30, 20, 10))
    permit(store, mine)
    permit(store, theirs, user='40')
    withdraw_saved(client, mine, theirs, nobodys, deleted=True)
    assert fences(store) == sorted([(mine, 1, '20', 0), (theirs, 1, '40', 0), (nobodys, 1, None, None)])
    store.personal_control('20', 'disable')
    assert [row[0] for row in fences(store)] == sorted([theirs, nobodys])
    assert [row[0] for row in fences(Store(store.path))] == sorted([theirs, nobodys])


def test_a_known_author_with_memory_off_gets_no_fence(world):
    """Nothing new is written about a member with memory off (gene #145)."""
    client, store = world
    mine = source(age=30, channel='51')['id']
    store.personal_control('20', 'disable')
    permit(store, mine)  # After, since "forget me" erases permits too.
    withdraw_saved(client, mine, deleted=True)
    assert fences(store) == []


def test_a_fence_goes_once_its_authors_revision_moved_at_start(world):
    """An older controller's "forget me" moves the author's revision but leaves this table: the next start drops their
    fences, and keeps the rest."""
    client, store = world
    mine, nobodys = source(age=30, channel='51')['id'], source(age=20, channel='51')['id']
    permit(store, mine)
    withdraw_saved(client, mine, nobodys, deleted=True)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        store.advance_revision(db, '20')
    assert [row[0] for row in fences(Store(store.path))] == [nobodys]


def test_a_fence_lasts_a_week_and_their_number_is_bounded(world, monkeypatch):
    client, store = world
    old = source(age=30, channel='51')['id']
    with store.connection() as db:
        db.execute('INSERT INTO chat_message_fences(guild_id,message_id,at,deleted) VALUES (?,?,?,1)',
                   ('123', old, time.time()-controller.WITHDRAWN_SECONDS-1))
    assert reply(store, drew_on=[old]).get('kept', True) is True  # Expired: as if there were none.
    monkeypatch.setattr(controller, 'MESSAGE_FENCES', 3)
    ids = [source(age=age, channel='51')['id'] for age in (50, 40, 30, 20, 10)]
    for i in ids:
        withdraw_saved(client, i, deleted=True)
    assert [row[0] for row in fences(store)] == sorted(ids[-3:])  # The newest kept.


def test_nothing_it_keeps_holds_the_words(world):
    client, store = world
    nearby = source(age=30, channel='51')['id']
    permit(store, nearby)
    withdraw_saved(client, nearby, deleted=True)
    with store.connection() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM chat_message_fences')]
    assert set(rows[0]) == {'guild_id', 'message_id', 'at', 'deleted', 'user_id', 'revision'}
    assert 'lavender' not in json.dumps(rows) and MARKER not in json.dumps(rows)


def test_a_stale_permit_from_before_their_forget_me_leaves_no_fence(world):
    """Review of gene #159 (round 1): a permit made at the member's revision then, its withdrawal delivered after they
    erased and turned memory back on. The permit's revision is stale, so nothing names them under their new one, and
    as a withdrawal after a privacy change writes nothing about them (gene #145), no fence is kept at all."""
    client, store = world
    mine = source(age=30, channel='51')['id']
    store.personal_control('20', 'disable')
    store.personal_control('20', 'enable')
    permit(store, mine)  # At revision 0, as it was made before, kept by a grant or permit outliving the erasure.
    withdraw_saved(client, mine, deleted=True)
    assert fences(store) == []
    assert fences(Store(store.path)) == []


@pytest.mark.parametrize('read', ['status', 'chat context'])
def test_an_expired_fence_goes_at_the_next_health_check_or_chat_read(world, read):
    """Review of gene #159 (round 1): a week is enforced without another withdrawal or a restart, for fences naming
    an author and for those naming nobody (which no "forget me" can find)."""
    _, store = world
    named, nobodys = source(age=30, channel='51')['id'], source(age=20, channel='51')['id']
    with store.connection() as db:
        for message, user, revision in ((named, '20', 0), (nobodys, None, None)):
            db.execute('INSERT INTO chat_message_fences(guild_id,message_id,at,deleted,user_id,revision) '
                       'VALUES (?,?,?,1,?,?)', ('123', message, time.time()-controller.WITHDRAWN_SECONDS-1, user,
                                                revision))
    store.status() if read == 'status' else store.chat_context('123', 'public:51', '30')
    assert fences(store) == []


OLDER_PRIVACY_CHANGE = "INSERT INTO chat_revisions VALUES ('20',1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1"


@pytest.mark.parametrize('change', ['clear chats', 'older controller'])
def test_a_named_fence_goes_the_moment_its_authors_revision_moves(world, change):
    """Review of gene #159 (round 1): clearing their chats here, or an older controller's privacy change in a rollback
    window, moves the author's revision; the database's own triggers (gene #160) take their fences at once, not at the
    next start. Fences naming nobody, or someone else, stay."""
    client, store = world
    mine, theirs, nobodys = (source(age=age, channel='51')['id'] for age in (30, 20, 10))
    permit(store, mine)
    permit(store, theirs, user='40')
    withdraw_saved(client, mine, theirs, nobodys, deleted=True)
    if change == 'clear chats':
        store.clear_chats('20')
    else:
        with store.connection() as db:
            db.execute(OLDER_PRIVACY_CHANGE)
    assert [row[0] for row in fences(store)] == sorted([theirs, nobodys])


def test_a_start_replaces_an_earlier_definition_of_the_privacy_triggers(world):
    """A trigger body this code changes replaces the one an earlier controller created, which `IF NOT EXISTS` alone
    would keep."""
    client, store = world
    mine = source(age=30, channel='51')['id']
    with store.connection() as db:
        for name, event in (('privacy_revision_inserted', 'INSERT'), ('privacy_revision_moved', 'UPDATE OF revision')):
            db.execute(f'DROP TRIGGER {name}')
            db.execute(f'CREATE TRIGGER {name} AFTER {event} ON chat_revisions BEGIN SELECT 1; END')
    store = Store(store.path)
    permit(store, mine)
    withdraw_saved(client, mine, deleted=True)
    with store.connection() as db:
        db.execute(OLDER_PRIVACY_CHANGE)
    assert fences(store) == []


class Interrupted:
    """A connection whose start is cut off once the first trigger is in place and the second dropped."""
    def __init__(self, db):
        self.db = db

    def __getattr__(self, name):
        return getattr(self.db, name)

    def execute(self, sql, *args):
        if sql.startswith('CREATE TRIGGER privacy_revision_moved'):
            raise sqlite3.OperationalError('cut off')
        return self.db.execute(sql, *args)


def test_a_start_cut_off_while_replacing_the_privacy_triggers_keeps_the_earlier_ones(world):
    """Review of gene #159 (round 2): both triggers are replaced in one transaction, so a start that stops between
    them, or an older controller starting meanwhile, never finds either missing."""
    _, store = world
    with store.connection() as db:
        for name, event in (('privacy_revision_inserted', 'INSERT'), ('privacy_revision_moved', 'UPDATE OF revision')):
            db.execute(f'DROP TRIGGER {name}')
            db.execute(f'CREATE TRIGGER {name} AFTER {event} ON chat_revisions BEGIN SELECT 1; END')
    with closing(sqlite3.connect(store.path)) as raw:
        with pytest.raises(sqlite3.OperationalError), raw:
            erasure_triggers(Interrupted(raw))
    with closing(sqlite3.connect(store.path)) as raw:
        assert dict(raw.execute("SELECT name,sql LIKE '%SELECT 1;%' FROM sqlite_master WHERE type='trigger' AND "
                                "name LIKE 'privacy_%'")) == {
            'privacy_revision_inserted': 1, 'privacy_revision_moved': 1}


OLDER_HEARTBEAT = "INSERT OR REPLACE INTO gene_runtime_health VALUES (1,?,'{}')"


AGENT_STATE = "INSERT INTO agent_state VALUES ('probe','1') ON CONFLICT(key) DO UPDATE SET value=excluded.value"
# The older controller's own statements for a member's chat session in a space, and a model call's usage receipt.
SESSION = ("INSERT INTO agent_sessions VALUES ('30','public:51',?,1,0) ON CONFLICT(user_id,space) "
           "DO UPDATE SET started=excluded.started,turns=excluded.turns,until=excluded.until")
USAGE = "INSERT OR IGNORE INTO chat_usage VALUES (?,'model','high','completed',?,1,0,1,0)"
OLDER = {'heartbeat': (OLDER_HEARTBEAT, lambda: (time.time(),)), 'agent state': (AGENT_STATE, tuple),
         'agent state again': (AGENT_STATE, tuple), 'session': (SESSION, lambda: (time.time(),)),
         'session again': (SESSION, lambda: (time.time(),)), 'usage': (USAGE, lambda: ('a'*32, time.time()))}


@pytest.mark.parametrize('write', ['heartbeat', 'saved turn', 'agent state', 'agent state again', 'session',
                                   'session again', 'usage'])
def test_an_older_controller_expires_fences_with_gene_s_heartbeat_or_as_it_serves_fri(world, write):
    """Review of gene #159 (rounds 2 and 3): an older controller neither reads nor expires fences, but it records
    Gene's runtime heartbeat every few seconds, as this one does, and, with Gene's runtime down, still saves turns and
    Fri's state as it serves Fri. A trigger on each drops fences past their week, names included, whichever
    controller runs. Fresh ones stay. gene #168: with learning off and a member's memory off, a DM reply saves
    nothing, but its session and usage are still written."""
    client, store = world
    if write.endswith('again'):  # An update of a row already there, not an insert.
        with store.connection() as db:
            statement, args = OLDER[write]
            db.execute(statement, args())
    mine, nobodys, fresh = (source(age=age, channel='51')['id'] for age in (30, 20, 10))
    withdraw_saved(client, fresh, deleted=True)  # First: writing a fence here expires old ones itself.
    with store.connection() as db:
        for message, user, revision in ((mine, '20', 0), (nobodys, None, None)):
            db.execute('INSERT INTO chat_message_fences(guild_id,message_id,at,deleted,user_id,revision) '
                       'VALUES (?,?,?,1,?,?)', ('123', message, time.time()-controller.WITHDRAWN_SECONDS-60, user,
                                                revision))
    if write == 'saved turn':
        reply(store)
    else:
        with closing(sqlite3.connect(store.path)) as raw, raw:  # The older controller's own statement, as it runs.
            statement, args = OLDER[write]
            raw.execute(statement, args())
    assert [row[0] for row in fences(store)] == [fresh]


# gene #163: count eviction never drops a fence an active learning batch still needs.

def fill(store, count, after, prefix='9'):
    """`count` fences newer than `after`, as many withdrawals by ID would leave."""
    with store.connection() as db:
        db.executemany('INSERT INTO chat_message_fences(guild_id,message_id,at,deleted) VALUES (?,?,?,0)',
                       [('123', f'{prefix}{i:012d}', after+(i+1)*1e-6) for i in range(count)])


def fenced(store):
    with store.connection() as db:
        return db.execute('SELECT count(*) FROM chat_message_fences').fetchone()[0]


def test_the_issues_repro_a_batch_whose_fence_was_evicted_learns_nothing(world):
    """A batch claims M, M is withdrawn (fenced), newer fences fill the table to its limit, and one more evicts M's. The
    batch, finishing, saves nothing it read: its work was invalidated before the fence went. The table stays bounded."""
    client, store = world
    taken, finish, _ = batch(client, store)
    withdraw_saved(client, taken)  # Edited while the batch reads it: fenced.
    with store.connection() as db:
        at = db.execute('SELECT at FROM chat_message_fences WHERE message_id=?', (taken,)).fetchone()[0]
    fill(store, controller.MESSAGE_FENCES-1, at)
    assert fenced(store) == controller.MESSAGE_FENCES
    withdraw_saved(client, source(age=5, channel='51')['id'])  # One more: M's is the oldest, so it goes.
    assert fenced(store) == controller.MESSAGE_FENCES
    with store.connection() as db:
        assert not db.execute('SELECT 1 FROM chat_message_fences WHERE message_id=?', (taken,)).fetchone()
    finish()
    assert LIKE not in notes(store)
    assert notes(store) == []  # Nothing of the batch: it's read again later, after the cursor it never moved.


def test_eviction_with_no_batch_in_flight_keeps_the_newest(world, monkeypatch):
    client, store = world
    monkeypatch.setattr(controller, 'MESSAGE_FENCES', 3)
    ids = [source(age=age, channel='51')['id'] for age in (50, 40, 30, 20, 10)]
    for i in ids:
        withdraw_saved(client, i, deleted=True)
    assert [row[0] for row in fences(store)] == sorted(ids[-3:]) and fenced(store) == 3


def test_evicting_fences_from_before_a_batch_leaves_it_learning(world, monkeypatch):
    """Only fences a batch could depend on (written since it was claimed) invalidate it; older ones go as before."""
    client, store = world
    monkeypatch.setattr(controller, 'MESSAGE_FENCES', 3)
    for age in (50, 40, 30):
        withdraw_saved(client, source(age=age, channel='51')['id'], deleted=True)
    _, finish, _ = batch(client, store)
    withdraw_saved(client, source(age=5, channel='51')['id'], deleted=True)  # Evicts one from before the claim.
    assert fenced(store) == 3
    finish()
    assert sorted(notes(store)) == sorted([LIKE, 'i play chess on weekends'])


def test_an_older_controllers_eviction_also_stops_the_batch(world):
    """The rule lives in a trigger replaced at start, so a controller rolled back to one that evicts fences by count
    without knowing it still invalidates the batch: its own statement, run against this database."""
    client, store = world
    taken, finish, _ = batch(client, store)
    withdraw_saved(client, taken)
    with closing(sqlite3.connect(store.path)) as raw, raw:  # Gene main 8aad087's eviction, M's the only fence.
        raw.execute('DELETE FROM chat_message_fences WHERE rowid IN (SELECT rowid FROM chat_message_fences '
                    'ORDER BY at DESC, rowid DESC LIMIT -1 OFFSET ?)', (0,))
    finish()
    assert notes(store) == []


def test_a_claim_with_nothing_dropped_since_learns_as_before(world):
    """Guard: a fence dropped from before the claim, or none at all, leaves the batch learning."""
    client, store = world
    old = source(age=50, channel='51')['id']
    withdraw_saved(client, old, deleted=True)
    _, finish, _ = batch(client, store)
    with store.connection() as db:
        db.execute('DELETE FROM chat_message_fences WHERE message_id=?', (old,))
    finish()
    assert sorted(notes(store)) == sorted([LIKE, 'i play chess on weekends'])


def cursors(store, client):
    with store.connection() as db:
        return AgentStore(store, client.settings).get(db, 'cursors', {})


@pytest.mark.parametrize('late', [3601, 8*86400], ids=['past an hour', 'past the fences week'])
def test_a_batch_finishing_after_its_claims_lifetime_learns_nothing(world, monkeypatch, late):
    """gene #179: a batch claims M, M is withdrawn (fenced), and the same claim finishes more than a week later, with
    nothing pruning the fences meanwhile. withdrawn_during() counts only fences inside their week, so the withdrawn
    note was kept. A finish more than an hour after its claim, far inside the fences' week and past any batch Fri
    runs, is refused: nothing it read is kept and its cursor stays, so the slice is read again once the claim is
    cleared."""
    client, store = world
    taken, finish, claim = batch(client, store)
    withdraw_saved(client, taken)  # Edited while the batch reads it: fenced.
    before = cursors(store, client)
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: real()+late)
    with pytest.raises(ValueError, match='expired'):
        finish()
    assert notes(store) == [] and cursors(store, client) == before


def test_a_batch_finishing_inside_its_claims_lifetime_learns_as_before(world, monkeypatch):
    """Guard: a finish inside the ten minutes keeps what it may, as before, and leaves out the withdrawn message."""
    client, store = world
    taken, finish, claim = batch(client, store)
    withdraw_saved(client, taken)
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: real()+590)
    finish()
    assert notes(store) == ['i play chess on weekends']


def test_a_batch_slower_than_the_claims_ten_minutes_still_learns(world, monkeypatch):
    """gene #180 review: Fri gives one model call up to ten minutes, so a whole batch can run past a claim's ten
    minutes. Its finish, with nothing claimed since, keeps what it may, as before (main accepts it too)."""
    client, store = world
    taken, finish, claim = batch(client, store)
    withdraw_saved(client, taken)
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: real()+1201)
    finish()
    assert notes(store) == ['i play chess on weekends']


def set_claim_time(store, client, value):
    agent = AgentStore(store, client.settings)
    with store.connection() as db:
        claim = agent.get(db, 'learning_claim')
        claim['at'] = value
        agent.put(db, 'learning_claim', claim)
    return agent


@pytest.mark.parametrize('value', [None, 'oops', float('nan'), float('inf'), True, 10**400], ids=lambda v: repr(v)[:12])
def test_a_claim_with_a_malformed_time_is_refused_or_interrupted_not_crashed(world, monkeypatch, value):
    """gene #180 review: a claim whose stored time isn't a number crashed the finish (500, the claim kept). It's
    refused like an expired claim, its cursor kept, and the next claim reports it interrupted instead of crashing."""
    client, store = world
    taken, finish, claim = batch(client, store)
    before = cursors(store, client)
    agent = set_claim_time(store, client, value)
    with pytest.raises(ValueError, match='expired'):
        finish()
    assert notes(store) == [] and cursors(store, client) == before
    blocked = agent.claim(manual=True)
    assert blocked == {'blocked': 'The previous pass was interrupted. Its report is pending; its allowance remains reserved.'}


def test_a_claim_dated_far_ahead_is_interrupted_not_left_running(world):
    """gene #180 review: a claim dated two hours ahead (a clock that ran fast, then was corrected) is refused at its
    finish, which can't order fences against it, and the next claim reports it interrupted instead of calling it
    running for two hours more. A claim a little ahead (a small clock step) still counts as running."""
    client, store = world
    taken, finish, claim = batch(client, store)
    agent = set_claim_time(store, client, time.time()+7200)
    with pytest.raises(ValueError, match='expired'):
        finish()
    assert agent.claim(manual=True) == {
        'blocked': 'The previous pass was interrupted. Its report is pending; its allowance remains reserved.'}


def test_a_claim_a_little_ahead_still_counts_as_running(world):
    client, store = world
    batch(client, store)
    agent = set_claim_time(store, client, time.time()+30)
    assert agent.claim(manual=True) == {'blocked': 'A learning pass is already running.'}


# gene #182: a fence written while the clock is set back still stops the batch in flight.

def claim_at(store):
    with store.connection() as db:
        return json.loads(db.execute("SELECT value FROM agent_state WHERE key='learning_claim'").fetchone()[0])['at']


def test_the_issues_repro_a_withdrawal_during_a_backward_clock_step_stops_the_batch(world, monkeypatch):
    """Claim at T, the clock set to T-30, a message the batch read withdrawn, the finish at T+10. withdrawn_during()
    counts only fences at or after the claim, so the note and the summary were kept. A fence dated before the claim in
    flight invalidates it, whatever its message: nothing the batch read is kept."""
    client, store = world
    taken, finish, claim = batch(client, store)
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: claim['at']-30)
    withdraw_saved(client, taken)
    monkeypatch.setattr(time, 'time', lambda: claim['at']+10)
    finish()
    assert notes(store) == []
    with store.connection() as db:
        assert not db.execute('SELECT 1 FROM agent_summaries').fetchone()
    monkeypatch.setattr(time, 'time', real)


def test_an_older_controllers_fence_dated_before_the_claim_also_stops_the_batch(world):
    """Its own insert (gene main 8aad087's statement), run against this database: the rule is a trigger."""
    client, store = world
    taken, finish, claim = batch(client, store)
    with closing(sqlite3.connect(store.path)) as raw, raw:
        raw.execute('INSERT INTO chat_message_fences(guild_id,message_id,at,deleted,user_id,revision) '
                    'VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id,message_id) DO UPDATE SET at=excluded.at',
                    ('123', taken, claim['at']-30, 0, None, None))
    finish()
    assert notes(store) == []


def test_a_fence_moved_back_before_the_claim_also_stops_the_batch(world, monkeypatch):
    """A message fenced before the batch and withdrawn again while the clock is set back keeps its row, at the earlier
    time: that refresh counts as a fence written now."""
    client, store = world
    old = source(age=50, channel='51')['id']
    withdraw_saved(client, old, deleted=True)
    _, finish, claim = batch(client, store)
    monkeypatch.setattr(time, 'time', lambda: claim['at']-30)
    withdraw_saved(client, old, deleted=True)
    monkeypatch.undo()
    finish()
    assert notes(store) == []


def test_a_clock_set_back_with_no_batch_in_flight_changes_nothing(world, monkeypatch):
    """Guard: a fence dated before a claim made later, and an ordinary withdrawal during one, leave it learning as
    withdrawn_during() decides."""
    client, store = world
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: real()-30)
    withdraw_saved(client, source(age=300, channel='51')['id'], deleted=True)
    monkeypatch.setattr(time, 'time', real)
    taken, finish, _ = batch(client, store)
    withdraw_saved(client, taken)  # At or after the claim: only this message is left out.
    finish()
    assert notes(store) == ['i play chess on weekends']


# gene #183: an older controller's finish of a claim older than the fences' week keeps nothing.

def aged(store, claim, days=8):
    """The claim, and the fences written since it, moved back `days`: what a batch finishing that late depends on,
    with nothing pruning meanwhile."""
    with closing(sqlite3.connect(store.path)) as raw, raw:
        raw.execute("UPDATE agent_state SET value=json_set(value,'$.at',json_extract(value,'$.at')-?) "
                    "WHERE key='learning_claim'", (days*86400,))
        raw.execute('UPDATE chat_message_fences SET at=at-? WHERE at>=?', (days*86400, claim['at']))


def older_finish(store, claim, taken):
    """Gene main 8aad087's finish, statement by statement where it matters: it saves the note (its withdrawn_during()
    counts only fences inside their week), moves the cursor (whose write expires the fence), then clears the claim."""
    from gene.fri_memory_store import save
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        save(db, '123', 'public:51', '20', ('hobbies', LIKE), time.time()-200, sources=[taken])
        AgentStore.put(db, 'cursors', {'51': {'before': '100'}})
        AgentStore.put(db, 'learning_claim', None)


def test_the_issues_repro_an_older_finish_of_a_claim_past_the_fences_week_keeps_nothing(world):
    client, store = world
    taken, _, claim = batch(client, store)
    withdraw_saved(client, taken)
    aged(store, claim)
    with pytest.raises(sqlite3.IntegrityError, match='fence it depended on'):
        older_finish(store, claim, taken)
    assert notes(store) == [] and claim_at(store) is not None  # All of it undone: the claim stands.


def test_the_claim_an_older_finish_couldnt_complete_is_cleared_as_interrupted(world):
    """The interruption cleanup, which reports the claim before clearing it, goes through: by this controller's
    claim(), and by an older one's statements."""
    client, store = world
    taken, _, claim = batch(client, store)
    withdraw_saved(client, taken)
    aged(store, claim)
    agent = AgentStore(store, client.settings)
    assert 'interrupted' in agent.claim(manual=True)['blocked']
    with store.connection() as db:
        assert AgentStore.get(db, 'learning_claim') is None
        assert db.execute('SELECT 1 FROM agent_reports WHERE id=?', (claim['id'],)).fetchone()


def test_an_older_finish_inside_the_fences_week_completes_as_before(world):
    """Guard: a claim younger than the fences' week clears as before, from any controller."""
    client, store = world
    taken, _, claim = batch(client, store)
    older_finish(store, claim, source(age=100, channel='51')['id'])
    with store.connection() as db:
        assert AgentStore.get(db, 'learning_claim') is None
