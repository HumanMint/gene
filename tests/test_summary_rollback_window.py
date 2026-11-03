"""gene #147, #148, #161, #162: what depends on a member's summary while an older controller serves this database.

A rollback window: an older controller, the live release say, knows nothing of the summaries other members' saved
replies, drafts and reminders depend on (fri #193, fri #216). Its withdrawals, erasures and rebuilds drop or replace a
member's summary the way they always have, and leave other members' copies of its words in place until this code
starts again and settles. Triggers on the summaries' own table, which it runs too, retire those copies the moment the
summary goes, as this controller does. A start also stops trusting a dependency whose summary an older controller took
without moving its generation (gene #148).
"""

import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.store import Store
from tests.test_dependent_writes import drafted, posted
from tests.test_fri_endings import source
from tests.test_saved_dependencies import (
    SAID,
    dependencies,
    dm,
    holds,
    reminder,
    rows,
    work,
    written,
)
from tests.test_summary_dependencies import ECHOED, depending, repeated, saved_reply
from tests.test_summary_generation_retention import QUOTING, quoted, retired, summarized
from tests.test_turn_withdrawal import (
    MARKER,
    built,
    claimed,
    post_turn,
    readable,
    save,
    summary_generation_now,
    turn,
    withdraw,
)

ASKED = 'the '+MARKER+' song from their raid, or another one?'


def make(path):
    settings = Settings(path, '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32,
                        workshop_channel_id='50', fri_token='fri-'+'x'*32, agent_channels='51,52',
                        test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


@pytest.fixture
def world(tmp_path):
    return make(tmp_path/'window.sqlite3')


@pytest.fixture
def older(monkeypatch):
    """The live release (`34dbbb3`), before gene #137 and #154, as far as summaries go: a summary going retires the
    member's own replies that drew on it, never what depends on it elsewhere, and a retired reply keeps any dependencies
    it had, as it knows of none. A covered rebuild carries only the member's own replies on to the new generation (gene
    #170). Its functions below are that release's own; everything else is this controller's code, so the same events
    are also run with the real older code (see the PR)."""
    import gene.fri_member_summary as summaries

    def advance(db, guild, user, covered=False):
        db.execute('INSERT INTO fri_member_summary_generations VALUES (?,?,1) ON CONFLICT(guild_id, user_id) '
                   'DO UPDATE SET generation=generation+1', (guild, user))
        if covered:
            db.execute('UPDATE chat_turns SET summary_generation=(SELECT generation FROM fri_member_summary_generations '
                       'WHERE guild_id=? AND user_id=?) WHERE guild_id=? AND user_id=? AND summary_generation IS NOT NULL',
                       (guild, user, guild, user))
        else:
            retire(db, 'guild_id=? AND user_id=?', (guild, user))

    def retire(db, where, args=()):
        turns = f'({where}) AND summary_generation IS NOT NULL'
        db.execute("DELETE FROM chat_pending WHERE (guild_id,user_id,source_id) IN (SELECT guild_id,user_id,"
                   f"json_extract(NULLIF(source,''),'$.id') FROM chat_turns WHERE {turns})", args)
        db.execute(f'UPDATE chat_turns SET reply=?,verbatim=0,summary_generation=NULL WHERE {turns}',
                   (summaries.RETIRED, *args))

    @contextmanager
    def running():
        with monkeypatch.context() as patched:
            for name in ('retire_dependents', 'retire_writes'):
                patched.setattr(summaries, name, lambda *args, **kwargs: None)
            patched.setattr(summaries, 'advance', advance)
            patched.setattr(summaries, 'retire', retire)
            yield
    return running


def asking(client, generation, message_id='800', on='20'):
    """Member 30's music request in #51, whose reply repeats member `on`'s summary-derived one and leaves a question
    pending."""
    body = turn(source(age=1, user='30', channel='51'), 'fri play a song', user='30', reply=ASKED,
                reply_message_id=message_id, verbatim=True, summary_dependencies=depending(generation, on),
                provenance=[{'user_id': on, 'revision': 0}],
                clarification={'goal': 'play', 'topic': '', 'question': ASKED})
    saved = post_turn(client, body)
    assert saved.status_code == 200, saved.text


def verbatim(store, message_id):
    with store.connection() as db:
        return db.execute('SELECT verbatim FROM chat_turns WHERE reply_message_id=?', (message_id,)).fetchone()[0]


def pending(store, user='30'):
    with store.connection() as db:
        return db.execute('SELECT count(*) FROM chat_pending WHERE user_id=?', (user,)).fetchone()[0]


def inputs(store, user='30'):
    with store.connection() as db:
        return [row[0] for row in db.execute('SELECT input FROM chat_turns WHERE user_id=? ORDER BY id', (user,))]


def triggers(store):
    with store.connection() as db:
        return sorted(row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger'"))


def without_triggers(store):
    """A database a release before these triggers wrote, and an older controller ran on."""
    with store.connection() as db:
        for name in triggers(store):
            db.execute(f'DROP TRIGGER {name}')


def drawing_on(store, user='40', conversation='public:52', on='20'):
    """Member `user`'s readable exchanges whose words drew on member `on`'s, so a summary of them names `on` too."""
    for text, age in (('i main a healer like they said', 7300), ('see you at the raid', 7250)):
        save(store, text, user=user, conversation=conversation, age=age, audience='server',
             provenance=[{'user_id': on, 'revision': 0}])
        with store.connection() as db:
            db.execute('UPDATE chat_turns SET at=? WHERE id=(SELECT max(id) FROM chat_turns)', (time.time()-age+1,))


def rebuilding(client, store, conversation='public:52'):
    """New readable exchanges of member 20's (in #52, unless said) and a build of them claimed: its claim, for finish."""
    readable(store, 'new plans over here', 5000, conversation=conversation)
    readable(store, 'and more of them', 4900, conversation=conversation)
    with store.connection() as db:
        db.execute("DELETE FROM fri_member_summary_calls WHERE user_id='20'")
    return claimed(client.app.state.agent)


def test_an_older_controllers_withdrawal_retires_replies_depending_on_that_summary_at_once(world, older):
    """gene #147: A's source is withdrawn on an older controller. A's own reply on A's summary goes, as it always did,
    and now so do B's reply repeating it and B's pending question, then and there rather than at the next start here.
    B's own words stay, and so does an ordinary reply."""
    client, store = world
    taken, generation = summarized(client, store)
    quoted(store, generation, conversation='public:51')
    asking(client, generation)
    plain = turn(source(age=2, user='40', channel='51'), 'hi', user='40', reply='hey', reply_message_id='802',
                 verbatim=True)
    assert post_turn(client, plain).status_code == 200
    assert pending(store) == 1
    with older():
        assert withdraw(client, taken).json()['removed'] == 1
    assert retired(saved_reply(store, '700')) and retired(saved_reply(store))
    assert verbatim(store, '800') == 0 and pending(store) == 0
    assert inputs(store) == ['fri play a song']
    assert (saved_reply(store, '802'), verbatim(store, '802')) == ('hey', 1)


def test_an_older_controllers_withdrawal_retires_writes_depending_on_that_summary_at_once(world, older):
    """gene #161: no privacy change, only a withdrawal, so gene #160's triggers don't fire. B's draft and reminder keep
    nothing of A's summary straight away and lose their links. B's other writes stay, and so does one drawn on B's own
    summary (gene #112)."""
    client, store = world
    taken, generation = summarized(client, store)
    kept_work = written(client, '/workspace/save', work(dm(1), depending(generation)))
    kept_reminder = written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    other_work = written(client, '/workspace/save', work(dm(3), body='my own list'))
    own = written(client, '/social/reminders/prepare', {**reminder(dm(4), depending(0, '30')), 'body': 'own '+SAID})
    with older():
        withdraw(client, taken)
    works, reminders = rows(store, 'fri_work'), rows(store, 'fri_reminders')
    assert works[kept_work['id']]['version'] == 2 and MARKER not in works[kept_work['id']]['body']
    assert (reminders[kept_reminder['id']]['state'], reminders[kept_reminder['id']]['body']) == ('cancelled', '')
    assert works[other_work['id']]['body'] == 'my own list' and reminders[own['id']]['body'] == 'own '+SAID
    assert dependencies(store) == []


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_an_older_controllers_provenance_drop_retires_what_depends_on_that_summary_at_once(world, older, erase):
    """gene #162: B's summary drew on A's words. An older controller's privacy change of A takes B's summary through its
    provenance, moving B's generation but not B's revision, so gene #160's triggers don't fire. C's reply, draft and
    reminder depending on B's summary keep nothing of it at once."""
    client, store = world
    summarized(client, store)
    drawing_on(store)
    built(client.app.state.agent, 'they main a healer')
    with store.connection() as db:
        assert '"20"' in db.execute("SELECT provenance FROM fri_member_summaries WHERE user_id='40'").fetchone()[0]
    theirs = summary_generation_now(store, '40')
    repeated(client, theirs, message_id='801', on='40')
    written(client, '/workspace/save', work(dm(1), depending(theirs, '40')))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(theirs, '40')))
    with older():
        if erase == 'disable':
            store.personal_control('20', 'disable')
        else:
            getattr(store, erase)('20')
    assert summary_generation_now(store, '40') != theirs
    assert retired(saved_reply(store, '801')) and not holds(store) and dependencies(store) == []


def test_an_older_controllers_rebuild_not_sent_the_old_summary_retires_what_depends_on_it_at_once(world, older):
    """A rebuild that wasn't sent the summary it replaces drops its lineage: a later withdrawal of what the old one drew
    on wouldn't find the new one. This controller retires what depended on the old one then, and now so does an older
    controller's."""
    from gene.fri_member_summary import finish
    client, store = world
    _, generation = summarized(client, store)
    asking(client, generation)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    claim_id, conversations, _ = rebuilding(client, store)
    with older():
        assert finish(client.app.state.agent, claim_id, 'they made new plans', 'completed', conversations, False)['kept']
    assert retired(saved_reply(store)) and pending(store) == 0
    assert not holds(store) and dependencies(store) == []


def test_a_rebuild_sent_the_old_summary_keeps_what_depends_on_it_where_it_was_carried_on(world):
    """The guard for the triggers' rebuild case: one sent the old summary keeps its lineage, so nothing this controller
    (or Gene main) carried on to the new generation goes. The live release moves the generation without carrying
    dependents on, so they go then, as the next start here would retire them (gene #170)."""
    from gene.fri_member_summary import finish
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    claim_id, conversations, _ = rebuilding(client, store)
    assert finish(client.app.state.agent, claim_id, 'they made new plans', 'completed', conversations, True)['kept']
    assert saved_reply(store) == ECHOED and holds(store)
    with store.connection() as db:  # The live release's own rebuild, sent the summary it replaces.
        db.execute("UPDATE fri_member_summary_generations SET generation=generation+1 WHERE user_id='20'")
        db.execute("UPDATE fri_member_summaries SET body='they made more plans',origins=json_insert(origins,'$[#]',"
                   "'public:999'),through=through+1,refreshed=? WHERE user_id='20'", (time.time(),))
    assert retired(saved_reply(store)) and not holds(store) and dependencies(store) == []
    Store(store.path)
    assert retired(saved_reply(store)) and not holds(store)


def test_what_an_older_controllers_withdrawal_leaves_alone(world, older):
    """Another member's summary's dependents, an ordinary reply, members' own words, a write drawn on the writer's own
    summary, and saved work in a channel, which is the member's own words from their own message."""
    client, store = world
    taken, generation = summarized(client, store)
    readable(store, 'i main a healer', 7300, user='40', conversation='public:52')
    readable(store, 'see you at the raid', 7250, user='40', conversation='public:52')
    built(client.app.state.agent, 'they main a healer')
    theirs = summary_generation_now(store, '40')
    repeated(client, theirs, message_id='801', age=3, on='40')
    other = written(client, '/workspace/save', work(dm(1), depending(theirs, '40')))
    own = written(client, '/social/reminders/prepare', {**reminder(dm(2), depending(0, '30')), 'body': 'own '+SAID})
    said = source(age=3, user='30', channel='51')
    public = written(client, '/workspace/save', {**work(said, depending(generation)), 'channel_id': '51',
                                                  'conversation_id': 'public:51', 'request_text': 'save '+SAID})
    plain = turn(source(age=2, user='30', channel='51'), 'hi', user='30', reply='hey', reply_message_id='802',
                 verbatim=True)
    assert post_turn(client, plain).status_code == 200
    repeated(client, generation)
    with older():
        withdraw(client, taken)
    assert retired(saved_reply(store))
    assert (saved_reply(store, '801'), verbatim(store, '801')) == (ECHOED, 1)
    assert (saved_reply(store, '802'), verbatim(store, '802')) == ('hey', 1)
    assert inputs(store) == ['what did they plan', 'hi', 'what did they plan']
    works, reminders = rows(store, 'fri_work'), rows(store, 'fri_reminders')
    assert works[other['id']]['body'] == SAID and reminders[own['id']]['body'] == 'own '+SAID
    assert works[public['id']]['body'] == SAID
    assert [row[2] for row in dependencies(store)] == ['40']  # Only the draft depending on member 40's.


def snapshot(store):
    """What a member could read back, and what a later summary change would still find: every saved exchange with the
    generations it drew on, pending question, draft, post in each delivery state, reminder, link with the item it
    belongs to, summary and generation. Items by the order they were written: their IDs differ between databases."""
    with store.connection() as db:
        def table(sql):
            return sorted(tuple(row) for row in db.execute(sql))
        work, reminders = ({row[0]: n for n, row in enumerate(db.execute(f'SELECT id FROM {name} ORDER BY rowid'))}
                           for name in ('fri_work', 'fri_reminders'))
        return {
            'turns': table('SELECT user_id,channel_id,input,reply,verbatim,summary_generation,summary_dependencies '
                           'FROM chat_turns'),
            'pending': table("SELECT user_id,conversation_id,json_extract(body,'$.question') FROM chat_pending"),
            'work': sorted((work[row['id']], row['user_id'], row['title'], row['body'], row['version'], row['state'])
                           for row in db.execute('SELECT * FROM fri_work')),
            'posts': sorted((work[row['work_id']], row['state'], row['body'], row['message_id'], row['lease'] != '')
                            for row in db.execute('SELECT * FROM fri_posts')),
            'reminders': sorted((reminders[row['id']], row['user_id'], row['body'], row['state'])
                                for row in db.execute('SELECT * FROM fri_reminders')),
            'links': sorted([('work', work[row['work_id']], *row[1:]) for row in db.execute(
                'SELECT work_id,guild_id,user_id,generation,revision FROM fri_work_dependencies')] + [
                ('reminder', reminders[row['reminder_id']], *row[1:]) for row in db.execute(
                    'SELECT reminder_id,guild_id,user_id,generation,revision FROM fri_reminder_dependencies')]),
            'summaries': table('SELECT user_id,body,revision,provenance,origins FROM fri_member_summaries'),
            'generations': table('SELECT guild_id,user_id,generation FROM fri_member_summary_generations'),
        }


def scene(client, store):
    """Member 20's summary and member 40's, which drew on 20's words. 20's own reply on theirs; 30's reply depending on
    20's with a question pending; 50's reply depending on 40's; 30's draft depending on 20's, reminder depending on 40's,
    and own reminder; 60's ordinary reply. The owner's draft depending on 20's, with one-off posts sent, sending,
    claimed, authorized and pending, and the owner's own draft with a pending post. The message 20's summary drew on."""
    taken, generation = summarized(client, store)
    drawing_on(store)
    built(client.app.state.agent, 'they main a healer')
    theirs = summary_generation_now(store, '40')
    quoted(store, generation, conversation='public:51')
    asking(client, generation)
    repeated(client, theirs, user='50', message_id='801', on='40', provenance=[{'user_id': '40', 'revision': 0}])
    assert pending(store) == 1
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(theirs, '40')))
    written(client, '/social/reminders/prepare', {**reminder(dm(3), depending(0, '30')), 'body': 'own '+SAID})
    plain = turn(source(age=2, user='60', channel='51'), 'hi', user='60', reply='hey', reply_message_id='802',
                 verbatim=True)
    assert post_turn(client, plain).status_code == 200
    draft = drafted(client, generation, age=4)
    sent, lease = posted(client, draft, 5, 'sending')
    written(client, f'/workspace/posts/{sent}/finish', {'lease': lease, 'state': 'sent', 'message_id': '987'})
    for age, state in ((6, 'sending'), (7, 'claimed'), (8, 'authorized'), (9, 'pending')):
        posted(client, draft, age, state)
    own = written(client, '/workspace/save', {**work(dm(10, '9'), body='my own plan'), 'user_id': '9'})
    posted(client, own, 11, 'pending')
    return taken


EVENTS = ['withdraw', 'disable', 'expire', 'rebuild', 'same-origin rebuild', 'covered rebuild']


def happen(client, store, taken, event):
    """rebuild: one not sent the summary it replaces, from new exchanges elsewhere, so its lineage is lost. same-origin
    rebuild: not sent it either, but from new exchanges where the old one's were, so its origins and provenance cover
    the old one's anyway (gene #170). covered rebuild: sent it, as the daily rebuild is."""
    from gene.fri_member_summary import finish
    if event == 'withdraw':
        withdraw(client, taken)
    elif event == 'disable':
        store.personal_control('20', 'disable')
    elif event == 'expire':
        from tests.test_summary_generation_retention import lapsed
        lapsed(store)
        store.status()  # Its prune drops a lapsed summary.
    else:
        claim_id, conversations, _ = rebuilding(client, store,
                                                'public:51' if event == 'same-origin rebuild' else 'public:52')
        assert finish(client.app.state.agent, claim_id, 'they made new plans', 'completed', conversations,
                      event == 'covered rebuild')['kept']


@pytest.mark.parametrize('event', EVENTS)
def test_an_older_controller_with_the_triggers_leaves_what_a_start_here_would(tmp_path, older, event):
    """No data loss, and nothing left for later: each summary event, run by the live release on a database with the
    triggers, leaves exactly what the same event run on one without them leaves once this controller starts. A start
    after it then finds nothing more to do."""
    legacy, started = make(tmp_path/'older.sqlite3'), make(tmp_path/'started.sqlite3')
    taken = scene(*legacy)
    with older():
        happen(*legacy, taken, event)
    taken = scene(*started)
    without_triggers(started[1])
    with older():
        happen(*started, taken, event)
    Store(started[1].path)
    after = snapshot(legacy[1])
    assert after == snapshot(started[1])
    Store(legacy[1].path)
    assert snapshot(legacy[1]) == after


@pytest.mark.parametrize('event', EVENTS)
def test_an_older_controller_with_the_triggers_leaves_what_this_one_leaves(tmp_path, older, event):
    """Never more than this controller retires: the same members and writes, each summary event run by this code on a
    database without the triggers, and by the live release on one with them, leave the same state behind. The one
    exception is the covered rebuild: the live release can't carry what depends on it on to the new generation, so it
    goes then, as a start here would retire it (above). A same-origin rebuild not sent the old summary keeps its lineage
    by structure alone; this controller retires what depended on the old one then, and so does the live release now."""
    newer, legacy = make(tmp_path/'newer.sqlite3'), make(tmp_path/'older.sqlite3')
    taken = scene(*newer)
    without_triggers(newer[1])
    happen(*newer, taken, event)
    taken = scene(*legacy)
    with older():
        happen(*legacy, taken, event)
    mine, theirs = snapshot(newer[1]), snapshot(legacy[1])
    carried = event == 'covered rebuild'
    assert (theirs == mine) != carried
    # The event reached 30's reply and question here unless the rebuild carried them on; under the live release, always.
    assert (ASKED in str(mine['turns']), pending(newer[1])) == (carried, int(carried))
    assert (ASKED in str(theirs['turns']), pending(legacy[1])) == (False, 0)
    # The owner's post that was sending keeps its state and lease, and none keeps the draft's words (gene #169).
    assert (1, 'sending', '', '', True) in theirs['posts'] and MARKER not in str(theirs['posts'])


def test_a_pre_generation_controllers_clear_retires_what_depends_on_that_summary_at_once(world):
    """gene #148: a controller from before summary generations clears A's memory elsewhere. A's revision moves and A's
    summary goes, but A's generation row, which it doesn't know about, stays. B's reply depending on A's summary keeps
    nothing of it straight away, and a start here finds nothing left to trust."""
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation, provenance=[])
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    with store.connection() as db:  # Its statements: the summary goes, the revision moves, generations stay.
        db.execute("DELETE FROM fri_member_summaries WHERE user_id='20'")
        db.execute("INSERT INTO chat_revisions VALUES ('20',1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1")
        assert db.execute("SELECT generation FROM fri_member_summary_generations WHERE user_id='20'").fetchone()[0] == 0
    assert retired(saved_reply(store)) and not holds(store)
    Store(store.path)
    assert retired(saved_reply(store)) and not holds(store)


@pytest.mark.parametrize('cleared', [True, False], ids=['memory-cleared', 'withdrawn'])
def test_a_start_retires_what_depends_on_a_summary_an_older_controller_took_without_its_generation(world, cleared):
    """gene #148 at a start: the database had no triggers while that controller ran (a release before them wrote it),
    and it took A's summary without moving A's generation, clearing A's memory or withdrawing A's message. The generation
    row still matches, but a dependency counts only while its summary is still there under its member's current
    revision, so a start retires B's reply, draft and reminder, and A's own reply on it."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation, conversation='public:51')
    repeated(client, generation, provenance=[])
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    without_triggers(store)
    with store.connection() as db:
        db.execute("DELETE FROM fri_member_summaries WHERE user_id='20'")
        if cleared:
            db.execute("INSERT INTO chat_revisions VALUES ('20',1) ON CONFLICT(user_id) "
                       "DO UPDATE SET revision=revision+1")
    assert saved_reply(store) == ECHOED and saved_reply(store, '700') == QUOTING and holds(store)
    Store(store.path)
    assert retired(saved_reply(store)) and retired(saved_reply(store, '700'))
    assert not holds(store) and dependencies(store) == []


def test_an_upgrade_keeps_what_depends_on_a_current_summary(world):
    """The guard for the start: old rows carry nothing new, and those whose summary is still current all stay, so an
    upgrade retires nothing a member could still read. The triggers are made as it starts."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation, conversation='public:51')
    repeated(client, generation)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    without_triggers(store)
    Store(store.path)
    assert saved_reply(store) == ECHOED and saved_reply(store, '700') == QUOTING
    assert holds(store) and len(dependencies(store)) == 2
    assert {'member_summary_gone', 'member_summary_replaced', 'member_summary_carried'} <= set(triggers(store))


def test_a_purge_before_a_rollback_takes_the_triggers_with_the_summaries(world):
    """A controller without member summaries never sees them: the purge drops their table, and the triggers on it."""
    from gene.fri_member_summary import purge
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    purge(store.path)
    assert not {'member_summary_gone', 'member_summary_replaced', 'member_summary_carried'} & set(triggers(store))
    assert retired(saved_reply(store))
    with store.connection() as db:  # The older controller's own privacy change still runs.
        db.execute("DELETE FROM chat_turns WHERE user_id='30'")
        db.execute("INSERT INTO chat_revisions VALUES ('30',1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1")


def test_a_start_replaces_a_trigger_an_earlier_controller_made(world):
    """A body changed since takes effect at the next start, every trigger replaced in one transaction, and one an
    earlier controller never made (gene #170's) is made."""
    client, store = world
    with store.connection() as db:
        db.execute('DROP TRIGGER member_summary_gone')
        db.execute("CREATE TRIGGER member_summary_gone AFTER DELETE ON fri_member_summaries BEGIN SELECT 1; END")
        db.execute('DROP TRIGGER member_summary_replaced')
        db.execute('DROP TRIGGER member_summary_carried')
    Store(store.path)
    with store.connection() as db:
        bodies = dict(db.execute("SELECT name, sql FROM sqlite_master WHERE name LIKE 'member_summary_%'").fetchall())
    assert set(bodies) == {'member_summary_gone', 'member_summary_replaced', 'member_summary_carried'}
    assert 'chat_turns' in bodies['member_summary_gone'] and 'SELECT 1;' not in bodies['member_summary_gone']
