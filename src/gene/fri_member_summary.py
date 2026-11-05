"""Fri's own short summary of what each member talked about with it lately (memory v2, phase 3).

The first model-written text about a member the controller keeps, so it is held to the narrowest rules.
It is built only from the member's own words in exchanges said where everyone in the server could read
them: never a DM, the /fri panel or a restricted channel. It carries whose words those drew on, and goes
when any of them changes their privacy settings or the member's own revision moves (forget me, memory
off, cleared chats, an edit), or when a message of theirs it may have read is withdrawn. It lapses
30 days after its last refresh. Fri builds it in its background loop, since the controller has no
model access: at most one call per member a day, kept only if nothing changed since the claim. A
withdrawal that drops it moves the member's summary generation, so a reply that already drew on it is
refused (gene #91), and a saved reply that drew on it keeps only a breadcrumb (fri #172). Before a rollback to a
controller without it: python -m gene.fri_member_summary purge <database>
"""

import json
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

from .fri_context import ChatSource
from .privacy import clean

TTL = 30*86400
DAY = 86400
SETTLE = 3600  # Built once the member's latest exchange is an hour old, so a day's one call covers the chat.
NEW = 2  # Exchanges since the last build before another is worth a call.
EXCHANGES = 24  # The newest a build reads.
DRAWN_MOST = 100  # The most messages a summary names as drawn on (see finish); past that, not known.
DAILY = 50  # Builds a day across the server, however many members are active.
CLAIM_TIMEOUT = 600
SKEW = 300  # Seconds a Discord message's time may run ahead of the controller's clock.
IDLE = 600  # Seconds before looking again when nobody is due.
# A kept summary's messages are read again from Discord at least this often, rebuilt or not (HumanMint/fri#349).
RECHECK = 12*3600
READS = 50  # The most messages one recheck claim names, past its first summary.
LENGTH = 500
UNSAFE = re.compile(r'<[@#&:/]|://|```|\d{15,}')  # Mentions, links, code and ids never reach a summary.
CLAIM, NEXT = 'member_summary_claim', 'next_member_summary'
# What a saved reply that drew on a summary since gone keeps instead of its words (fri #172).
RETIRED = 'Earlier answer drawing on an older summary of them. Nothing from it is retained here.'
# A saved reply that depends on that member's summary through another reply of Fri's (fri #193); `?`: their ID.
DEPENDS = ("summary_dependencies IS NOT NULL AND EXISTS (SELECT 1 FROM json_each(chat_turns.summary_dependencies) d "
           "WHERE json_extract(d.value,'$.user_id')=?)")
# Saved work and reminders the model wrote that may repeat another member's summary (fri #216): their dependencies'
# tables, and the column naming the item.
WRITES = (('fri_work_dependencies', 'work_id'), ('fri_reminder_dependencies', 'reminder_id'))
# What such a draft keeps once one of those summaries goes, instead of its title and words.
WORK_TITLE = 'Withdrawn draft'
WORK_RETIRED = 'A draft drawing on what Fri remembered about someone, since withdrawn. Nothing from it is retained here.'
# What a saved reply that drew on a summary held back until it's read again (HumanMint/fri#349) reads as meanwhile,
# without being changed (HumanMint/fri#355): once the summary checks out, the reply reads as it was.
HELD_REPLY = 'Earlier answer drawing on a summary of them that is being checked again. Not shown for now.'
# Why a draft or reminder depending on such a summary isn't given or sent meanwhile (HumanMint/fri#355).
HELD_WRITE = 'That draws on what Fri remembered about someone, which is being checked again. Try again in a bit.'
# Whether the summary of the member `{guild}`, `{user}` names (SQL) is held back until it's read again.
HOLDING = ('EXISTS (SELECT 1 FROM fri_member_summaries hs JOIN fri_member_summary_rechecks hr ON hr.guild_id=hs.guild_id '
           'AND hr.user_id=hs.user_id AND hr.through=hs.through AND hr.held WHERE hs.guild_id={guild} AND '
           'hs.user_id={user})')
# The same exchanges a member's other conversations may see (Store._elsewhere).
READABLE = ("guild_id=? AND user_id=? AND audience='server' AND channel_id LIKE 'public:%' AND attributed=1 "
            "AND response_kind='reply'")


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS fri_member_summaries (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, body TEXT NOT NULL, revision INTEGER NOT NULL,
            provenance TEXT NOT NULL, origins TEXT NOT NULL, refreshed REAL NOT NULL, through REAL NOT NULL,
            PRIMARY KEY(guild_id, user_id));
        CREATE TABLE IF NOT EXISTS fri_member_summary_calls (user_id TEXT PRIMARY KEY, at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS fri_member_summary_generations (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, generation INTEGER NOT NULL,
            PRIMARY KEY(guild_id, user_id));
        -- The Discord messages a member's summary drew on, each with where it was said and when its exchange was
        -- saved, for the summary whose `through` it names (HumanMint/fri#333). Its own table, so an older controller
        -- that writes the summary row as it always has still can; one it wrote has none that match, so they're unknown.
        CREATE TABLE IF NOT EXISTS fri_member_summary_sources (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, through REAL NOT NULL, messages TEXT NOT NULL,
            PRIMARY KEY(guild_id, user_id));
        -- It goes with its summary however the summary goes: forget me, clearing chats, erasure, expiry, or a
        -- controller rolled back to one that doesn't know this table deleting or rewriting the summary as it always
        -- did. Triggers live in the database, so they fire for that one too (Sol round 1 of #210).
        CREATE TRIGGER IF NOT EXISTS fri_member_summary_sources_deleted AFTER DELETE ON fri_member_summaries BEGIN
            DELETE FROM fri_member_summary_sources WHERE guild_id=OLD.guild_id AND user_id=OLD.user_id;
        END;
        CREATE TRIGGER IF NOT EXISTS fri_member_summary_sources_replaced AFTER UPDATE ON fri_member_summaries
        WHEN NEW.through IS NOT OLD.through OR NEW.body IS NOT OLD.body BEGIN
            DELETE FROM fri_member_summary_sources WHERE guild_id=OLD.guild_id AND user_id=OLD.user_id
                AND through IS NOT NEW.through;
        END;
        -- One left from before these, or kept by a database restored without them: swept at start.
        DELETE FROM fri_member_summary_sources WHERE NOT EXISTS (SELECT 1 FROM fri_member_summaries s
            WHERE s.guild_id=fri_member_summary_sources.guild_id AND s.user_id=fri_member_summary_sources.user_id
            AND s.through=fri_member_summary_sources.through);
        -- A build in flight's claim names the member's messages, where each was said and when it was saved, and a
        -- rebuild's the earlier summary's too (HumanMint/fri#333). Any privacy change of theirs (forget me, memory
        -- off or on, cleared chats, an edit) moves their revision, which spends the claim, as finish then keeps
        -- nothing: it goes in that change's own transaction, by this controller or a rolled-back one, which runs these
        -- too and doesn't know these names, so never replaces them (Sol round 2 of #210).
        CREATE TRIGGER IF NOT EXISTS fri_member_summary_claim_revised AFTER INSERT ON chat_revisions BEGIN
            DELETE FROM agent_state WHERE key='member_summary_claim' AND json_valid(value)
                AND json_extract(value,'$.user_id')=NEW.user_id;
        END;
        CREATE TRIGGER IF NOT EXISTS fri_member_summary_claim_revision_moved AFTER UPDATE OF revision ON chat_revisions
        BEGIN
            DELETE FROM agent_state WHERE key='member_summary_claim' AND json_valid(value)
                AND json_extract(value,'$.user_id')=NEW.user_id;
        END;
        -- When a kept summary's messages were last read again from Discord, whether it's held back until they are,
        -- and a recheck claimed for it (HumanMint/fri#349). For the summary whose `through` it names, in its own table
        -- as the sources are, and gone with it however it goes, by an older controller too. No row: nobody checked
        -- it, and it shows as before.
        CREATE TABLE IF NOT EXISTS fri_member_summary_rechecks (
            guild_id TEXT NOT NULL, user_id TEXT NOT NULL, through REAL NOT NULL, checked REAL NOT NULL,
            held INTEGER NOT NULL, claim TEXT NOT NULL DEFAULT '', claimed REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(guild_id, user_id));
        CREATE TRIGGER IF NOT EXISTS fri_member_summary_rechecks_deleted AFTER DELETE ON fri_member_summaries BEGIN
            DELETE FROM fri_member_summary_rechecks WHERE guild_id=OLD.guild_id AND user_id=OLD.user_id;
        END;
        CREATE TRIGGER IF NOT EXISTS fri_member_summary_rechecks_replaced AFTER UPDATE ON fri_member_summaries
        WHEN NEW.through IS NOT OLD.through OR NEW.body IS NOT OLD.body BEGIN
            DELETE FROM fri_member_summary_rechecks WHERE guild_id=OLD.guild_id AND user_id=OLD.user_id;
        END;
        DELETE FROM fri_member_summary_rechecks WHERE NOT EXISTS (SELECT 1 FROM fri_member_summaries s
            WHERE s.guild_id=fri_member_summary_rechecks.guild_id AND s.user_id=fri_member_summary_rechecks.user_id
            AND s.through=fri_member_summary_rechecks.through);
        -- Saved work and reminders that drew on their writer's own summary (HumanMint/fri#355, Sol round 1's B3):
        -- kept out of use while that summary is held back (see held_writes), but never retired when it goes, as the
        -- writer asked for them (gene #112), so they're kept apart from the dependencies, which retire. kind: the
        -- dependency table's name (see WRITES).
        CREATE TABLE IF NOT EXISTS fri_own_summary_writes (
            kind TEXT NOT NULL, item TEXT NOT NULL, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
            PRIMARY KEY(kind, item));
    ''')
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for name, table, kind in (('fri_own_summary_writes_work_gone', 'fri_work', 'fri_work_dependencies'),
                              ('fri_own_summary_writes_reminder_gone', 'fri_reminders', 'fri_reminder_dependencies')):
        if table in tables:  # Gone with what they name, whichever controller deletes it.
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {name} AFTER DELETE ON {table} BEGIN DELETE FROM "
                       f"fri_own_summary_writes WHERE kind='{kind}' AND item=OLD.id; END")
    # One whose member's privacy changed without those (a controller from before them, a database restored without
    # them), or one past its time, which no build finishes now: swept at start, whatever it names.
    db.execute("DELETE FROM agent_state WHERE key=? AND json_valid(value) AND json_type(value)='object' AND ("
               "json_extract(value,'$.revision') IS NOT coalesce((SELECT revision FROM chat_revisions r WHERE "
               "r.user_id=json_extract(agent_state.value,'$.user_id')),0) OR EXISTS (SELECT 1 FROM personal_preferences p "
               "WHERE p.user_id=json_extract(agent_state.value,'$.user_id') AND NOT p.enabled) OR "
               "coalesce(json_extract(value,'$.at'),0)<=?)", (CLAIM, time.time()-CLAIM_TIMEOUT))


def advance(db, guild, user, covered=False):
    """The member's summary generation moves: a reply that drew on the summary it had fails its check and save (gene
    #91), and one already saved keeps only a breadcrumb (fri #172), since once that summary is gone a later withdrawal
    finds nothing to move. covered: a rebuild that was sent the summary it replaces keeps its origins, provenance and
    a later `through`, so a withdrawal of anything the old one drew on still finds the new one. The saved replies then
    move to the new generation instead, and the routine daily rebuild doesn't erase Fri's side of a DM."""
    db.execute('INSERT INTO fri_member_summary_generations VALUES (?,?,1) ON CONFLICT(guild_id, user_id) '
               'DO UPDATE SET generation=generation+1', (guild, user))
    if covered:
        db.execute('UPDATE chat_turns SET summary_generation=(SELECT generation FROM fri_member_summary_generations '
                   'WHERE guild_id=? AND user_id=?) WHERE guild_id=? AND user_id=? AND summary_generation IS NOT NULL',
                   (guild, user, guild, user))
        # And other members' replies that depended on it (fri #193).
        db.execute("UPDATE chat_turns SET summary_dependencies=(SELECT json_group_array(CASE WHEN "
                   "json_extract(d.value,'$.user_id')=? THEN json_object('user_id',?,'generation',?) ELSE json(d.value) "
                   f"END) FROM json_each(chat_turns.summary_dependencies) d) WHERE guild_id=? AND {DEPENDS}",
                   (user, user, generation(db, guild, user), guild, user))
        for table, _ in writes(db):  # And saved work and reminders that did (fri #216).
            db.execute(f'UPDATE {table} SET generation=? WHERE guild_id=? AND user_id=?',
                       (generation(db, guild, user), guild, user))
    else:
        retire(db, 'guild_id=? AND user_id=?', (guild, user))
        retire_dependents(db, "chat_turns.guild_id=? AND json_extract(d.value,'$.user_id')=?", (guild, user))
        retire_writes(db, 'd.guild_id=? AND d.user_id=?', (guild, user))


def retire(db, where, args=()):
    """Saved replies that drew on a summary now gone keep a breadcrumb, not its words: a later turn reads them back as
    history (fri #172). Not verbatim, so a reference to Fri's message fails closed, and reply checks pass over it.
    A question such a turn left open goes too, as its words were the model's. The member's own words stay. A turn
    that drew on no summary, or came from an older Fri, says so and stays."""
    _retire(db, f'({where}) AND summary_generation IS NOT NULL', args)


def retire_dependents(db, where, args=()):
    """Saved replies that may repeat another reply of Fri's that drew on a summary now gone (one in the channel's view or
    one a member answered, which their turn carried) keep a breadcrumb too, or a reference or echo of them brings the
    words back to someone else's request (fri #193). where: about `d`, each of a reply's dependencies."""
    _retire(db, f'summary_dependencies IS NOT NULL AND EXISTS (SELECT 1 FROM json_each(chat_turns.summary_dependencies) '
                f'd WHERE {where})', args)


def _retire(db, turns, args=()):
    dependencies = 'summary_dependencies' in {row[1] for row in db.execute('PRAGMA table_info(chat_turns)')}
    db.execute("DELETE FROM chat_pending WHERE (guild_id,user_id,source_id) IN (SELECT guild_id,user_id,"
               f"json_extract(NULLIF(source,''),'$.id') FROM chat_turns WHERE {turns})", args)
    db.execute(f'UPDATE chat_turns SET reply=?,verbatim=0,summary_generation=NULL'
               f'{",summary_dependencies=NULL" if dependencies else ""} WHERE {turns}', (RETIRED, *args))


def writes(db):
    """Those of WRITES this database has: a purge before a rollback can run on one an older controller made."""
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    return [(table, key) for table, key in WRITES if table in present]


def depend_write(db, table, item, scope, dependencies, own=False):
    """A tool's write of the model's words keeps the summaries its turn may repeat through Fri's earlier replies, with
    their generations, so it's retired once one goes (see retire_writes), as a saved reply is (fri #216). Each goes on
    record (see keep), and so is that member's privacy revision now, which a start checks (see settle). Not the writer's
    own: work and reminders drawn on that stay, as they asked for them (gene #112). A draft edited again keeps what it
    had, as the edit may still hold those words.

    own: the turn drew on the writer's own summary (its summary_generation). That, or one of their own among the
    summaries it may repeat, is recorded apart (fri_own_summary_writes), so the write is held while that summary is
    held back (see held_writes), and still never retired for it (HumanMint/fri#355, Sol round 1's B3)."""
    from .store import Store
    key = dict(WRITES)[table]
    if own or any(row['user_id'] == scope['user_id'] for row in dependencies):
        db.execute('INSERT INTO fri_own_summary_writes VALUES (?,?,?,?) ON CONFLICT(kind, item) DO NOTHING',
                   (table, item, scope['guild_id'], scope['user_id']))
    for user, number in sorted({(row['user_id'], row['generation']) for row in dependencies}):
        if user == scope['user_id']:
            continue
        keep(db, scope['guild_id'], user)
        db.execute(f'INSERT INTO {table}({key},guild_id,user_id,generation,revision) VALUES (?,?,?,?,?) '
                   f'ON CONFLICT({key},user_id) DO UPDATE SET generation=excluded.generation,revision=excluded.revision',
                   (item, scope['guild_id'], user, number, Store._privacy_state(db, user)[0]))


def retire_writes(db, where, args=()):
    """Saved work and reminders that may repeat a summary now gone keep nothing of it (fri #216). A draft keeps a
    breadcrumb for its title and words, a version on, so an edit of the old one fails; its posts not yet sending are
    cancelled. A reminder not yet sending is cancelled. None keeps its words, one going out now included, which keeps
    its state and lease so its receipt still lands: delivery already holds what it sends (gene #169). where: about `d`,
    each of their dependencies."""
    for table, key in writes(db):
        items = f'(SELECT {key} FROM {table} d WHERE {where})'
        if table == 'fri_work_dependencies':
            db.execute(f'UPDATE fri_work SET title=?,body=?,version=version+1 WHERE id IN {items}',
                       (WORK_TITLE, WORK_RETIRED, *args))
            db.execute("UPDATE fri_posts SET state=CASE WHEN state IN ('pending','authorized','claimed') THEN 'cancelled' "
                       f"ELSE state END,body='' WHERE work_id IN {items}", args)
        else:
            db.execute("UPDATE fri_reminders SET state=CASE WHEN state IN ('pending','scheduled','claimed') THEN "
                       f"'cancelled' ELSE state END,body='' WHERE id IN {items}", args)
        db.execute(f'DELETE FROM {table} WHERE {key} IN {items}', args)


def current_writes(db, table, items, args=(), held_back=True):
    """Saved work or reminders are about to be shown or sent: a summary one depends on that lapsed (expired, say, with
    nothing pruning it yet) goes first, retiring what they hold of it, as a turn's check does (gene #113, #158). Call it
    first in the transaction: a drop is committed at once, so a refusal that follows can't undo it, and the
    transaction begins again under the write lock for the checks after it. items: SQL selecting those items' IDs.

    held_back: one depending on a summary held back until it's read again is refused, for now (HumanMint/fri#355); a
    list or a delivery claim passes False, and leaves those out itself (see held_writes)."""
    key = dict(WRITES)[table]
    went = False
    for guild, user in db.execute(f'SELECT DISTINCT guild_id,user_id FROM {table} WHERE {key} IN ({items}) '
                                  'ORDER BY guild_id,user_id', args).fetchall():
        went = lapsed(db, guild, user) or went
    if went:
        db.commit()
        db.execute('BEGIN IMMEDIATE')
    if held_back and held_writes(db, table, items, args):
        from .store import Held
        raise Held(HELD_WRITE)
    return went


# The writes every controller makes often, as the triggers acting for one that doesn't know to name them (see
# erasure_triggers): Gene's runtime heartbeat every few seconds, and what serving Fri saves (gene #168).
FREQUENT = (('', 'AFTER INSERT ON gene_runtime_health'), ('_turns', 'AFTER INSERT ON chat_turns'),
            ('_pending', 'AFTER INSERT ON chat_pending'), ('_grants', 'AFTER INSERT ON mention_grants'),
            ('_state', 'AFTER INSERT ON agent_state'), ('_state_moved', 'AFTER UPDATE ON agent_state'),
            ('_sessions', 'AFTER INSERT ON agent_sessions'), ('_sessions_moved', 'AFTER UPDATE ON agent_sessions'),
            ('_usage', 'AFTER INSERT ON chat_usage'))
MIRROR = 'summary_refreshed:'  # agent_state: a member something depends on, and when their summary was refreshed.


def erasure_triggers(db):
    """A member's privacy revision moves (an erasure, memory off, chats cleared), and an older controller in a rollback
    window does that without knowing what newer ones keep about them. These triggers, which it runs too, do what a start
    here does for such a move (see settle and Store._settle_withdrawals) at once: saved work and reminders depending on
    that member's summary keep nothing of it and lose all their links, as retire_writes does, and the deletion records
    and message fences (gene #149) naming them go, so nothing identifying
    them outlives the change (delta review of the 2026-10-30 release). Nothing may drop the tables they name while
    they exist."""
    moved = 'd.user_id=NEW.user_id AND d.revision IS NOT NEW.revision'
    title, retired = (text.replace("'", "''") for text in (WORK_TITLE, WORK_RETIRED))
    body = f"""
        UPDATE fri_work SET title='{title}',body='{retired}',version=version+1
            WHERE id IN (SELECT work_id FROM fri_work_dependencies d WHERE {moved});
        UPDATE fri_posts SET state=CASE WHEN state IN ('pending','authorized','claimed') THEN 'cancelled' ELSE state END,
            body='' WHERE work_id IN (SELECT work_id FROM fri_work_dependencies d WHERE {moved});
        DELETE FROM fri_work_dependencies WHERE work_id IN (SELECT work_id FROM fri_work_dependencies d WHERE {moved});
        UPDATE fri_reminders SET state=CASE WHEN state IN ('pending','scheduled','claimed') THEN 'cancelled'
            ELSE state END,body='' WHERE id IN (SELECT reminder_id FROM fri_reminder_dependencies d WHERE {moved});
        DELETE FROM fri_reminder_dependencies WHERE reminder_id IN
            (SELECT reminder_id FROM fri_reminder_dependencies d WHERE {moved});
        DELETE FROM chat_turn_deletions WHERE user_id=NEW.user_id AND revision IS NOT NEW.revision;
        DELETE FROM chat_message_fences WHERE user_id=NEW.user_id AND revision IS NOT NEW.revision;"""
    from .store import WITHDRAWN_SECONDS
    # Gene #149's week for message fences, kept by any controller: an older one neither reads nor expires them, but
    # records Gene's runtime heartbeat every few seconds (INSERT OR REPLACE, which runs an INSERT trigger), and, as it
    # serves Fri, saves turns, pending questions, a ping's grant and Fri's own state, and a member's chat session and
    # each model call's usage, which a reply that saves nothing (learning off, the member's memory off) still writes
    # (gene #168). Its health checks write nothing, so a controller that does none of those (no heartbeat and no
    # member talking to Fri) keeps them until one does, or this code starts; nothing reads them then.
    expire = (f"DELETE FROM chat_message_fences WHERE at<(julianday('now')-2440587.5)*86400.0-{WITHDRAWN_SECONDS};")
    # A learning batch in flight depends on every message fence written since it was claimed (Store.withdrawn_during).
    # Whatever drops one of those (count eviction at MESSAGE_FENCES, an erasure, an older controller's own eviction),
    # the batch keeps nothing it read: its finish discards its findings and leaves its cursor, so it's read again
    # (gene #163). Older fences, and deletions with no batch in flight, change nothing.
    claimed = "(SELECT json_extract(value,'$.at') FROM agent_state WHERE key='learning_claim' AND json_valid(value))"
    learning = ("UPDATE agent_state SET value=json_set(value,'$.privacy_invalidated',json('true')) "
                "WHERE key='learning_claim' AND json_valid(value) AND json_type(value)='object';")
    # A fence dated before the batch in flight was written while the clock was set back, after the batch was claimed:
    # withdrawn_during() orders fences by time and would miss it, so the batch keeps nothing it read, whichever message
    # it names (gene #182). One dated at or after the claim is left to withdrawn_during(), message by message.
    # Whoever writes it, an older controller too.
    triggers = (('privacy_revision_inserted', 'AFTER INSERT ON chat_revisions', body),
                ('privacy_revision_moved', 'AFTER UPDATE OF revision ON chat_revisions', body),
                *((f'message_fences_expire{suffix}', event, expire) for suffix, event in FREQUENT),
                ('message_fence_dropped_while_learning',
                 f'AFTER DELETE ON chat_message_fences WHEN OLD.at>={claimed}', learning),
                ('message_fence_before_learning_claim',
                 f'AFTER INSERT ON chat_message_fences WHEN NEW.at<{claimed}', learning),
                ('message_fence_moved_before_learning_claim',
                 f'AFTER UPDATE OF at ON chat_message_fences WHEN NEW.at<{claimed}', learning))
    # Replaced, not kept if they exist, so a body changed since an earlier controller made them takes effect; all in
    # one transaction, so a start cut off between them, or an older controller starting meanwhile, never finds one
    # missing.
    began = not db.in_transaction
    if began:
        db.execute('BEGIN IMMEDIATE')
    for name, event, statements in triggers:
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
        db.execute(f'CREATE TRIGGER {name} {event} BEGIN {statements} END')
    if began:
        db.commit()


def expiry_triggers(db):
    """A member's summary lapses (it's past TTL, with nothing pruning it yet), and an older controller in a rollback
    window, one before gene #154, neither checks what depends on it nor knows anything does: it showed a draft or
    reminder holding its words, and claimed and sent its post or the reminder (gene #176). This controller drops the
    summary first (current_writes). For one that doesn't, the database acts:
    - each member a draft or reminder depends on is mirrored in agent_state with when their summary was refreshed,
      kept in step with the summary and gone with it, and once nothing depends on them;
    - a post or reminder about to be claimed or sent while a summary it depends on has lapsed is refused;
    - the next of the frequent writes every controller makes retires what depends on a lapsed summary, as its going
      would (see summary_triggers), so its reads show none of it.
    The refusal and the retirement name only tables nothing drops: a purge before a rollback to a release without
    summaries drops theirs (see purge), and leaves these working. The mirror's own triggers on the dependency tables
    name the summaries, but only a controller that knows summaries writes those."""
    now = "(julianday('now')-2440587.5)*86400.0"

    def key(guild, user):
        return f"'{MIRROR}'||{guild}||':'||{user}"
    lapsed = f"CAST(a.value AS REAL)<{now}-{TTL}"
    work = (f"SELECT d.work_id FROM fri_work_dependencies d JOIN agent_state a ON a.key={key('d.guild_id', 'd.user_id')} "
            f"WHERE {lapsed}")
    reminders = (f"SELECT d.reminder_id FROM fri_reminder_dependencies d JOIN agent_state a ON "
                 f"a.key={key('d.guild_id', 'd.user_id')} WHERE {lapsed}")
    title, retired = (text.replace("'", "''") for text in (WORK_TITLE, WORK_RETIRED))
    unneeded = (f"substr(agent_state.key,1,{len(MIRROR)})='{MIRROR}' AND NOT EXISTS (SELECT 1 FROM fri_work_dependencies d "
                f"WHERE agent_state.key={key('d.guild_id', 'd.user_id')}) AND NOT EXISTS (SELECT 1 FROM "
                f"fri_reminder_dependencies d WHERE agent_state.key={key('d.guild_id', 'd.user_id')})")
    retire = f"""
        UPDATE fri_work SET title='{title}',body='{retired}',version=version+1 WHERE id IN ({work});
        UPDATE fri_posts SET state=CASE WHEN state IN ('pending','authorized','claimed') THEN 'cancelled' ELSE state END,
            body='' WHERE work_id IN ({work});
        DELETE FROM fri_work_dependencies WHERE work_id IN ({work});
        UPDATE fri_reminders SET state=CASE WHEN state IN ('pending','scheduled','claimed') THEN 'cancelled'
            ELSE state END,body='' WHERE id IN ({reminders});
        DELETE FROM fri_reminder_dependencies WHERE reminder_id IN ({reminders});
        DELETE FROM agent_state WHERE {unneeded};"""
    noted = (f"INSERT OR REPLACE INTO agent_state(key,value) SELECT {key('s.guild_id', 's.user_id')},s.refreshed "
             "FROM fri_member_summaries s WHERE s.guild_id=NEW.guild_id AND s.user_id=NEW.user_id;")
    refreshed = f"UPDATE agent_state SET value=NEW.refreshed WHERE key={key('NEW.guild_id', 'NEW.user_id')};"
    due = (f"EXISTS (SELECT 1 FROM agent_state a WHERE a.key>='{MIRROR}' AND a.key<'{MIRROR[:-1]};' AND {lapsed})")
    held = ("SELECT 1 FROM {table} d JOIN agent_state a ON a.key=" + key('d.guild_id', 'd.user_id') +
            " WHERE d.{column}=NEW.{item} AND " + lapsed)
    triggers = (
        ('summary_expiry_noted_work', 'AFTER INSERT ON fri_work_dependencies', noted),
        ('summary_expiry_noted_reminder', 'AFTER INSERT ON fri_reminder_dependencies', noted),
        ('summary_expiry_built', 'AFTER INSERT ON fri_member_summaries', refreshed),
        ('summary_expiry_refreshed', 'AFTER UPDATE ON fri_member_summaries', refreshed),
        ('summary_expiry_gone', 'AFTER DELETE ON fri_member_summaries',
         f"DELETE FROM agent_state WHERE key={key('OLD.guild_id', 'OLD.user_id')};"),
        # Only a move into `claimed` or `sending`: retirement keeps a `sending` item's state as it clears its words.
        ('summary_expired_post_held', "BEFORE UPDATE OF state ON fri_posts WHEN NEW.state IN ('claimed','sending') AND "
         f"OLD.state IS NOT NEW.state AND EXISTS ({held.format(table='fri_work_dependencies', column='work_id', item='work_id')})",
         "SELECT RAISE(ABORT, 'A summary this post depends on has lapsed; do not send');"),
        ('summary_expired_reminder_held', "BEFORE UPDATE OF state ON fri_reminders WHEN NEW.state IN ('claimed','sending') "
         f"AND OLD.state IS NOT NEW.state AND EXISTS ({held.format(table='fri_reminder_dependencies', column='reminder_id', item='id')})",
         "SELECT RAISE(ABORT, 'A summary this reminder depends on has lapsed; do not send');"),
        # The mirror's own writes aren't one of those: a summary lapses as time passes, with no write.
        # Only once a mirrored summary has lapsed, found by a range of the mirror's own keys, so the frequent writes
        # cost nothing more while every summary is current.
        *((f'summaries_lapse{suffix}', event + (f" WHEN substr(NEW.key,1,{len(MIRROR)}) IS NOT '{MIRROR}' AND {due}"
                                                 if event.endswith(' agent_state') else f' WHEN {due}'), retire)
          for suffix, event in FREQUENT))
    # Replaced in one transaction, as the others are, and the mirror written again from what depends on what now.
    began = not db.in_transaction
    if began:
        db.execute('BEGIN IMMEDIATE')
    for name, event, statements in triggers:
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
        db.execute(f'CREATE TRIGGER {name} {event} BEGIN {statements} END')
    db.execute(f"DELETE FROM agent_state WHERE substr(key,1,{len(MIRROR)})='{MIRROR}'")
    db.execute(f"INSERT INTO agent_state(key,value) SELECT {key('s.guild_id', 's.user_id')},s.refreshed "
               "FROM fri_member_summaries s WHERE EXISTS (SELECT 1 FROM fri_work_dependencies d WHERE "
               "d.guild_id=s.guild_id AND d.user_id=s.user_id) OR EXISTS (SELECT 1 FROM fri_reminder_dependencies d "
               "WHERE d.guild_id=s.guild_id AND d.user_id=s.user_id)")
    if began:
        db.commit()


def summary_triggers(db):
    """A member's summary goes (a withdrawal, an expiry, a privacy change, another member's through its provenance) or
    is replaced by a rebuild that wasn't sent it, and an older controller in a rollback window does that without knowing
    what depends on it elsewhere (fri #193, fri #216). These triggers, which it runs too, retire at once what this
    controller retires then (see advance and erase): the member's own saved replies that drew on it, other members'
    replies depending on it with any question they left open, and saved work and reminders depending on it with all
    their links (gene #147, #161, #162).

    A rebuild sent the summary it replaces keeps its words, revision, origins, provenance and a later `through`, so a
    withdrawal of anything the old one drew on still finds it. The row can't tell that from one that wasn't sent it but
    covers the old one's origins and provenance by structure (gene #170), so a covering update retires only what still
    depends on the member at another generation than theirs now: what the controller didn't carry on to it, as a start
    here would (see settle). This controller carries it on for a rebuild sent the old summary and retires it otherwise,
    before the update; the live release carries on only the member's own replies, so the rest goes then. A purge drops
    them with the table, before the generations they read; their bodies name only tables nothing else drops."""
    quoted = {name: text.replace("'", "''") for name, text in
              (('retired', RETIRED), ('title', WORK_TITLE), ('work', WORK_RETIRED))}
    now = ('(SELECT generation FROM fri_member_summary_generations g WHERE g.guild_id=OLD.guild_id AND '
           'g.user_id=OLD.user_id)')

    def body(stale):
        """What depends on the summary goes; stale: only at another generation than the member's now."""
        mine = f' AND chat_turns.summary_generation IS NOT {now}' if stale else ''
        theirs = f" AND json_extract(d.value,'$.generation') IS NOT {now}" if stale else ''
        turns = ("chat_turns.guild_id=OLD.guild_id AND ((chat_turns.user_id=OLD.user_id AND chat_turns.summary_generation "
                 f"IS NOT NULL{mine}) OR (chat_turns.summary_dependencies IS NOT NULL AND EXISTS (SELECT 1 FROM "
                 f"json_each(chat_turns.summary_dependencies) d WHERE json_extract(d.value,'$.user_id')=OLD.user_id"
                 f"{theirs})))")

        def depending(table, key, item):  # One of their items depends on the summary.
            return (f'EXISTS (SELECT 1 FROM {table} d WHERE d.{key}={item} AND d.guild_id=OLD.guild_id AND '
                    f'd.user_id=OLD.user_id{f" AND d.generation IS NOT {now}" if stale else ""})')
        work, reminders = WRITES
        # Correlated EXISTS throughout: SQLite 3.53 skipped a row-value IN in this body under the rebuild trigger's WHEN.
        return f"""
        DELETE FROM chat_pending WHERE EXISTS (SELECT 1 FROM chat_turns WHERE chat_turns.guild_id=chat_pending.guild_id
            AND chat_turns.user_id=chat_pending.user_id
            AND json_extract(NULLIF(chat_turns.source,''),'$.id')=chat_pending.source_id AND {turns});
        UPDATE chat_turns SET reply='{quoted['retired']}',verbatim=0,summary_generation=NULL,summary_dependencies=NULL
            WHERE {turns};
        UPDATE fri_work SET title='{quoted['title']}',body='{quoted['work']}',version=version+1
            WHERE {depending(*work, 'fri_work.id')};
        UPDATE fri_posts SET state=CASE WHEN state IN ('pending','authorized','claimed') THEN 'cancelled' ELSE state END,
            body='' WHERE {depending(*work, 'fri_posts.work_id')};
        DELETE FROM fri_work_dependencies WHERE {depending(*work, 'fri_work_dependencies.work_id')};
        UPDATE fri_reminders SET state=CASE WHEN state IN ('pending','scheduled','claimed') THEN 'cancelled'
            ELSE state END,body='' WHERE {depending(*reminders, 'fri_reminders.id')};
        DELETE FROM fri_reminder_dependencies WHERE {depending(*reminders, 'fri_reminder_dependencies.reminder_id')};"""
    covered = ("NEW.body<>'' AND NEW.revision=OLD.revision AND NEW.through>=OLD.through AND json_valid(OLD.origins) "
               "AND json_valid(NEW.origins) AND json_valid(OLD.provenance) AND json_valid(NEW.provenance) AND NOT EXISTS "
               "(SELECT 1 FROM json_each(OLD.origins) o WHERE NOT EXISTS (SELECT 1 FROM json_each(NEW.origins) n "
               "WHERE n.value IS o.value)) AND NOT EXISTS (SELECT 1 FROM json_each(OLD.provenance) p WHERE NOT EXISTS "
               "(SELECT 1 FROM json_each(NEW.provenance) q WHERE json_extract(q.value,'$.user_id') IS "
               "json_extract(p.value,'$.user_id') AND json_extract(q.value,'$.revision') IS json_extract(p.value,'$.revision')))")
    triggers = (('member_summary_gone', 'AFTER DELETE ON fri_member_summaries FOR EACH ROW', body(False)),
                ('member_summary_replaced', f'AFTER UPDATE ON fri_member_summaries FOR EACH ROW WHEN NOT ({covered})',
                 body(False)),
                ('member_summary_carried', f'AFTER UPDATE ON fri_member_summaries FOR EACH ROW WHEN {covered}',
                 body(True)))
    # Replaced, so a body changed since an earlier controller made them takes effect, in one transaction, so a start cut
    # off between them, or an older controller starting meanwhile, never finds one missing (as erasure_triggers does).
    began = not db.in_transaction
    if began:
        db.execute('BEGIN IMMEDIATE')
    for name, event, statements in triggers:
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
        db.execute(f'CREATE TRIGGER {name} {event} BEGIN {statements} END')
    if began:
        db.commit()


def _standing(guild, user):
    """SQL: the member has a summary, built under their privacy revision now. A dependency on it counts only while so: a
    controller from before generations takes a summary and leaves its generation, which could then still match (gene
    #148). Every way this controller takes one moves or deletes the generation and retires what drew on it."""
    return (f'EXISTS (SELECT 1 FROM fri_member_summaries s WHERE s.guild_id={guild} AND s.user_id={user} AND '
            's.revision=coalesce((SELECT revision FROM chat_revisions r WHERE r.user_id=s.user_id), 0))')


def settle(db):
    """At start: what an older controller left while it ran (a rollback window), which moved generations without
    retiring the replies that drew on them. Its privacy changes deleted the member's generation outright, and a drop
    there can write it again from one, so a reply whose generation can't be confirmed goes too (gene #114): one with
    none on record (a save records it, see keep), or saved under a revision of the member's since moved. One before
    generations took a summary and left its generation, so one drawn on a summary no longer standing goes too (gene
    #148); what drew on a summary still standing, old rows included, stays."""
    retire(db, 'summary_generation IS NOT (SELECT generation FROM fri_member_summary_generations g '
               'WHERE g.guild_id=chat_turns.guild_id AND g.user_id=chat_turns.user_id) OR speaker_revision IS NOT '
               'coalesce((SELECT revision FROM chat_revisions r WHERE r.user_id=chat_turns.user_id), 0) OR NOT '
               + _standing('chat_turns.guild_id', 'chat_turns.user_id'))
    # So does one depending on another member's summary (fri #193), as the older controller never retired those.
    retire_dependents(db, "json_extract(d.value,'$.generation') IS NOT (SELECT generation FROM "
                          "fri_member_summary_generations g WHERE g.guild_id=chat_turns.guild_id AND "
                          "g.user_id=json_extract(d.value,'$.user_id')) OR chat_turns.speaker_revision IS NOT "
                          "coalesce((SELECT revision FROM chat_revisions r WHERE r.user_id=chat_turns.user_id), 0) OR NOT "
                          + _standing('chat_turns.guild_id', "json_extract(d.value,'$.user_id')"))
    # And saved work and reminders that may repeat one (fri #216). An older controller's erasure of the writer took
    # theirs outright. Its privacy change of the member they depend on left the link, and a summary built again since
    # can bring the same generation back, so a moved revision of theirs retires it too (gene #154).
    retire_writes(db, 'd.generation IS NOT (SELECT generation FROM fri_member_summary_generations g '
                      'WHERE g.guild_id=d.guild_id AND g.user_id=d.user_id) OR d.revision IS NOT '
                      'coalesce((SELECT revision FROM chat_revisions r WHERE r.user_id=d.user_id), 0) OR NOT '
                      + _standing('d.guild_id', 'd.user_id'))


def keep(db, guild, user):
    """A reply that drew on the member's summary, or on another reply of Fri's that did, is being saved: its generation
    goes on record, zero included, so a start that finds none knows an older controller erased it (see settle)."""
    db.execute('INSERT INTO fri_member_summary_generations VALUES (?,?,0) ON CONFLICT(guild_id, user_id) DO NOTHING',
               (guild, user))


def drop(db, guild, user):
    db.execute('DELETE FROM fri_member_summaries WHERE guild_id=? AND user_id=?', (guild, user))
    db.execute('DELETE FROM fri_member_summary_sources WHERE guild_id=? AND user_id=?', (guild, user))
    advance(db, guild, user)


def drew_on(db, guild, user, through):
    """The messages the member's summary built through `through` drew on (see fri_member_summary_sources), or None
    where that isn't known."""
    row = db.execute('SELECT through,messages FROM fri_member_summary_sources WHERE guild_id=? AND user_id=?',
                     (guild, user)).fetchone()
    return json.loads(row['messages']) if row and row['through'] == through else None


def drawn(db, guild, user, summary):
    """What a summary row drew on (see drew_on), or None if that isn't known. One that names no conversation it came
    from is the member's own words, from Edit summary in My memory (see edit), an older controller's included, as a
    build always names at least one: it drew on no message (Sol round 1 of #214). One that does came from messages, so
    if it names none, they aren't known (HumanMint/fri#357)."""
    if not json.loads(summary['origins']):
        return []
    return drew_on(db, guild, user, summary['through']) or None


def identified(turn):
    """Whether every message an exchange answered can be named: its own and each of its burst's, a Discord message
    with an ID. One that can't goes into no build, as nothing could read it again (HumanMint/fri#357)."""
    rows = [json.loads(turn['source']) if turn.get('source') else {},
            *((row or {}).get('source') or {} for row in json.loads(turn.get('burst') or '[]'))]
    return all(isinstance(row, dict) and row.get('kind') == 'discord_message' and str(row.get('id', '')).isdigit()
               for row in rows)


def messages_of(turn):
    """The Discord messages an exchange answered: its own and its burst's, by ID."""
    source = json.loads(turn['source']) if turn.get('source') else {}
    ids = [source.get('id'), *((row.get('source') or {}).get('id') for row in json.loads(turn.get('burst') or '[]'))]
    return [str(i) for i in ids if i and str(i).isdigit()]


def prune(db, now=None):
    now = now or time.time()
    for row in db.execute('SELECT guild_id,user_id FROM fri_member_summaries WHERE refreshed<?', (now-TTL,)).fetchall():
        drop(db, row[0], row[1])
    db.execute('DELETE FROM fri_member_summary_calls WHERE at<?', (now-DAY,))


def erase(db, user_id):
    """Any privacy change of the member, or of anyone whose words it drew on, takes it. Their generation goes too:
    their revision moves with it, so a turn that drew on an older summary still fails, on the revision. Another member's
    summary that drew on their words goes too, and its generation moves, as that member's revision doesn't."""
    for row in db.execute("SELECT guild_id,user_id FROM fri_member_summaries WHERE user_id<>? AND EXISTS (SELECT 1 FROM "
                          "json_each(fri_member_summaries.provenance) p WHERE json_extract(p.value, '$.user_id')=?)",
                          (user_id, user_id)).fetchall():
        drop(db, row[0], row[1])
    db.execute('DELETE FROM fri_member_summaries WHERE user_id=?', (user_id,))
    db.execute('DELETE FROM fri_member_summary_generations WHERE user_id=?', (user_id,))
    retire(db, 'user_id=?', (user_id,))  # Their saved chats a change leaves in place, say a note edited elsewhere.
    # And other members' replies depending on it, even one whose provenance didn't name them (fri #193).
    retire_dependents(db, "json_extract(d.value,'$.user_id')=?", (user_id,))
    retire_writes(db, 'd.user_id=?', (user_id,))  # And their work and reminders that did (fri #216).


def generation(db, guild, user):
    """How many times a withdrawal has dropped the member's summary. Chat context returns it with the summary, and a
    turn that drew on that summary sends it back: once it moves, that turn's reply may hold withdrawn words."""
    row = db.execute('SELECT generation FROM fri_member_summary_generations WHERE guild_id=? AND user_id=?',
                     (guild, user)).fetchone()
    return row[0] if row else 0


def withdrawn(db, guild, user, conversation, message_id, saved):
    """A member's message withdrawn (gene #83): their summary, and a build of it in flight, go if either could have
    drawn on it, as its saved exchanges do. saved: when its exchanges just deleted were saved.

    A build reads only exchanges saved by its `through`, in the conversations it records, and a message is sent
    before its exchange is saved. So one sent after that (allowing for clock skew) never reached it, even if its
    exchange has since been trimmed. Not anyone else's summary (fri #101), nor their own edit, which drew on none.
    """
    from .fri_agent_store import AgentStore
    earliest = min([((int(message_id) >> 22)+1420070400000)/1000-SKEW, *saved])

    def drew(conversations, through):
        return conversation in conversations and earliest <= through
    row = db.execute('SELECT origins,through FROM fri_member_summaries WHERE guild_id=? AND user_id=?',
                     (guild, user)).fetchone()
    gone = bool(row) and drew(json.loads(row['origins']), row['through'])
    if gone:  # A repeat finds no summary, so moves nothing.
        drop(db, guild, user)
    record = AgentStore.get(db, CLAIM)
    # The build in flight may have been sent the summary as well as its own exchanges.
    if record and record['user_id'] == user and (gone or drew(
            {*record['sources'], *(record['previous'] or {}).get('origins', [])}, record['through'])):
        AgentStore.put(db, CLAIM, None)


def named(db, guild, messages):
    """The members whose summary's record of what it read (see drew_on) names one of these messages. A record goes with
    its summary, or with the summary it was of once that's replaced, by trigger, whichever controller does it. A
    message's ID, quoted, only ever stands for an `id` in it: the rest are conversations (`public:`, `private:`) and
    times."""
    users = set()
    for message in sorted({str(message) for message in messages if message}):
        users |= {row[0] for row in db.execute('SELECT user_id FROM fri_member_summary_sources WHERE guild_id=? AND '
                                               'instr(messages,?)>0', (guild, json.dumps(message))).fetchall()}
    return sorted(users)


def ours(record, guild):
    """Whether a build in flight's claim is this server's (gene #253). One from before claims named their server, by an
    older controller in a rollback window, can't be placed, so it counts as anyone's."""
    return isinstance(record, dict) and str(record.get('guild_id', guild)) == str(guild)


def claimed(record, guild, messages):
    """Whether a build in flight's claim, this server's (see ours), names one of these messages: an exchange it reads,
    or one its earlier summary drew on (HumanMint/fri#333)."""
    if not ours(record, guild):
        return False
    read = [*(record.get('messages') or ()), *((record.get('previous') or {}).get('messages') or ())]
    return bool({str(item.get('id')) for item in read if isinstance(item, dict)} & {str(m) for m in messages})


def naming(db, guild, messages):
    """Whether withdrawing these messages takes a summary or a build in flight (see taken). Read in one statement, so
    from one snapshot: read apart, a build finishing in between was missed both ways, its summary's record read before
    it finished and its claim after (gene #253). taken looks again under the write lock."""
    wanted = json.dumps([json.dumps(str(message)) for message in messages if message])
    found, record = db.execute(
        'SELECT EXISTS (SELECT 1 FROM fri_member_summary_sources s, json_each(?) m WHERE s.guild_id=? AND '
        'instr(s.messages,m.value)>0), (SELECT value FROM agent_state WHERE key=?)', (wanted, guild, CLAIM)).fetchone()
    return bool(found) or claimed(json.loads(record) if record else None, guild, messages)


def taken(db, guild, messages):
    """Messages edited or deleted, placed against members' summaries by what each recorded it read, whatever else holds
    them: a summary outlasts the exchanges it drew on, and no note need hold a message it read (gene #249). Each summary
    naming one goes, as withdrawn drops one, and so does a build in flight whose claim names one, or whose member's
    summary went. A summary with no record of what it read goes by where and when, under an exchange holding the
    message (see withdrawn), as before."""
    from .fri_agent_store import AgentStore
    users = named(db, guild, messages)
    for user in users:
        drop(db, guild, user)
    record = AgentStore.get(db, CLAIM)
    if ours(record, guild) and (record.get('user_id') in users or claimed(record, guild, messages)):
        AgentStore.put(db, CLAIM, None)


def _current(db, row):
    from .store import Store
    return (row['refreshed'] >= time.time()-TTL and Store._privacy_state(db, row['user_id']) == (row['revision'], True)
            and all(Store._privacy_state(db, s['user_id']) == (s['revision'], True) for s in json.loads(row['provenance'])))


def kept(db, guild, user):
    """The member's summary row, held back or not, or None. An empty body is a build that found nothing worth keeping."""
    row = db.execute('SELECT * FROM fri_member_summaries WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
    if row and not _current(db, row):
        drop(db, guild, user)  # Expired, say, while a reply holding it was still being written.
        return None
    return row


def read(db, guild, user):
    """The member's summary row, or None, as everything that shows or edits one reads it: not while it's held back
    until its messages are read again (HumanMint/fri#349, see recheck), though it stays, and isn't lapsed."""
    row = kept(db, guild, user)
    return None if row and held(db, guild, user) else row


def held(db, guild, user):
    """Whether the member's kept summary is held back until its messages are read again (HumanMint/fri#349). Not just
    the summary: what was made from it is kept out of use meanwhile too (HumanMint/fri#355), a turn that drew on it at
    its checks (Store._summary_unchanged), a saved reply that did as history and as a reference (see holding), a draft
    or reminder depending on it (see held_writes). Nothing is retired for that; only a summary that goes retires them."""
    return bool(db.execute('SELECT '+HOLDING.format(guild='?', user='?'), (guild, user)).fetchone()[0])


def holding(db, guild, drew):
    """Whether any of these summaries (a saved reply's, see Store's summaries) is held back (see held)."""
    return any(held(db, guild, item['user_id']) for item in drew)


def held_writes(db, table, items, args=()):
    """Of saved work or reminders (items: SQL selecting their IDs), those depending on a summary held back (see held):
    left out of lists and deliveries, and refused when asked for by themselves (see current_writes), until it checks
    out. Their writer's own summary counts too (see depend_write's own). table: of WRITES."""
    if table not in {name for name, _ in writes(db)}:
        return set()
    key = dict(WRITES)[table]
    held = {row[0] for row in db.execute(f'SELECT DISTINCT d.{key} FROM {table} d WHERE d.{key} IN ({items}) AND '
                                         + HOLDING.format(guild='d.guild_id', user='d.user_id'), args)}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fri_own_summary_writes'").fetchone():
        held |= {row[0] for row in db.execute(
            f'SELECT o.item FROM fri_own_summary_writes o WHERE o.kind=? AND o.item IN ({items}) AND '
            + HOLDING.format(guild='o.guild_id', user='o.user_id'), (table, *args))}
    return held


def lapsed(db, guild, user):
    """Whether the member had a summary that lapsed (expired, say, with nothing pruning it yet). It goes now, which
    moves their generation, so a turn or write that drew on it can't keep its words (gene #113). One held back for a
    recheck hasn't lapsed: a turn that drew on it before was given it when it showed."""
    row = db.execute('SELECT 1 FROM fri_member_summaries WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
    return bool(row) and kept(db, guild, user) is None


def view(row):
    """What Fri gets. The origins are where its words were said, for Fri to recheck who can read them now."""
    if not row or not row['body']:
        return None
    return {'text': row['body'], 'refreshed': row['refreshed'], 'expires_at': row['refreshed']+TTL,
            'provenance': json.loads(row['provenance']), 'origins': json.loads(row['origins'])}


def checked(text):
    """One short paragraph, or None if it holds what a summary never may."""
    text = ' '.join(clean(text, 4*LENGTH).split())
    if len(text) > LENGTH:
        text = text[:LENGTH].rsplit(' ', 1)[0]
    return None if UNSAFE.search(text) else text


def claim(agent):
    """The next member due a summary, with their own recent words and what Fri kept before, or None."""
    from .fri_reply_checks import neutral
    from .store import Store
    now = time.time()
    with agent.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        config = agent.config(db)
        if not config['enabled'] or not config['learning_enabled']:
            return None
        pending = agent.get(db, CLAIM)
        if pending and now-pending['at'] < CLAIM_TIMEOUT:
            return None
        if pending:  # Past its time, so spent: what it names goes now, whoever is due (Sol round 2 of #210).
            agent.put(db, CLAIM, None)
        if now < agent.get(db, NEXT, 0):
            return None
        prune(db, now)
        if db.execute('SELECT count(*) FROM fri_member_summary_calls').fetchone()[0] >= DAILY:
            agent.put(db, NEXT, now+IDLE)
            return None
        guild = agent.settings.guild_id
        due = db.execute(
            "SELECT t.user_id FROM chat_turns t LEFT JOIN fri_member_summaries s ON s.guild_id=t.guild_id "
            "AND s.user_id=t.user_id WHERE t.guild_id=? AND t.audience='server' AND t.channel_id LIKE 'public:%' "
            "AND t.attributed=1 AND t.response_kind='reply' AND t.at>coalesce(s.through, 0) "
            'AND t.user_id NOT IN (SELECT user_id FROM fri_member_summary_calls) '
            'AND t.user_id NOT IN (SELECT user_id FROM personal_preferences WHERE enabled=0) '
            'GROUP BY t.user_id HAVING count(*)>=? AND max(t.at)<? ORDER BY coalesce(max(s.refreshed), 0), t.user_id',
            (guild, NEW, now-SETTLE)).fetchall()
        for (user,) in due:
            revision = Store._privacy_state(db, user)[0]
            # Held back for a recheck or not: Fri reads what it drew on again itself before carrying it on.
            previous = kept(db, guild, user)
            # Only words said since the member's latest privacy change: one invalidates what came before.
            turns = [dict(row) for row in db.execute(
                f'SELECT id,channel_id,input,provenance,at,source,burst FROM chat_turns WHERE {READABLE} AND at>? '
                'AND speaker_revision=? ORDER BY at DESC, id DESC LIMIT ?',
                (guild, user, previous['through'] if previous else 0, revision, EXCHANGES))][::-1]
            turns = [turn for turn in agent.store._current_turns(db, turns) if identified(turn)]
            if len(turns) < NEW:
                continue
            sources = {}
            for turn in turns:
                sources.setdefault(turn['channel_id'], set()).update(
                    (s['user_id'], s['revision']) for s in turn['provenance'])
            shown = view(previous)
            db.execute('INSERT INTO fri_member_summary_calls VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET at=excluded.at',
                       (user, now))  # Every call counts, whatever happens to it.
            # Each exchange's messages, and those the earlier summary drew on (None if not known), named in the claim
            # for Fri to check against Discord before any of it is sent or carried on (HumanMint/fri#333).
            messages = [{'id': message, 'conversation_id': turn['channel_id'], 'saved_at': turn['at']}
                        for turn in turns for message in messages_of(turn)]
            earlier = drawn(db, guild, user, previous) if shown else None
            record = {'id': uuid.uuid4().hex, 'at': now, 'guild_id': guild, 'user_id': user, 'revision': revision,
                      'through': max(turn['at'] for turn in turns),
                      'sources': {c: sorted(map(list, s)) for c, s in sources.items()},
                      'previous': {k: shown[k] for k in ('provenance', 'origins')} if shown else None,
                      'messages': messages}
            if record['previous'] is not None:
                record['previous']['messages'] = earlier
            agent.put(db, CLAIM, record)
            return {'id': record['id'], 'model': config['summary_model'], 'effort': config['summary_effort'],
                    'previous': {'text': shown['text'], 'origins': shown['origins'], 'messages': earlier}
                    if shown else None,
                    'exchanges': [{'conversation_id': turn['channel_id'], 'message': neutral(turn['input']),
                                   'messages': messages_of(turn), 'saved_at': turn['at']} for turn in turns]}
        agent.put(db, NEXT, now+IDLE)
        return None


def finish(agent, claim_id, summary, status, conversations, previous, messages=None):
    """Keep a finished build, only if no one it draws on, the member included, changed anything since the claim.

    conversations: those Fri still found everyone could read and sent to the model; previous: whether it
    sent the earlier summary too. What it left out is left out of the provenance and origins as well.
    """
    from .store import Conflict, Store
    now = time.time()
    with agent.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        record = agent.get(db, CLAIM)
        if not record or record['id'] != claim_id:
            raise ValueError('That summary build expired')
        agent.put(db, CLAIM, None)
        config = agent.config(db)
        kept = sorted(set(record['sources']) & set(conversations))
        earlier = record['previous'] if previous else None
        text = checked(summary) if status == 'completed' else None
        if text is None or not kept or not config['enabled'] or not config['learning_enabled']:
            return {'kept': False}
        provenance = {(user, revision) for c in kept for user, revision in record['sources'][c]}
        provenance |= {(s['user_id'], s['revision']) for s in (earlier or {}).get('provenance', [])}
        if Store._privacy_state(db, record['user_id']) != (record['revision'], True) or any(
                Store._privacy_state(db, user) != (revision, True) for user, revision in provenance):
            db.commit()  # The claim is spent either way.
            raise Conflict('Memory changed while Fri was summarizing. Nothing was kept.')
        origins = sorted(set(kept) | set((earlier or {}).get('origins', [])))
        if db.execute('SELECT 1 FROM fri_member_summaries WHERE guild_id=? AND user_id=?',
                      (agent.settings.guild_id, record['user_id'])).fetchone():
            # Covered only by a summary later turns can see: an empty one would carry replies on with nothing
            # tagging a reply that repeats them.
            advance(db, agent.settings.guild_id, record['user_id'], covered=earlier is not None and bool(text))
        db.execute('INSERT INTO fri_member_summaries VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE '
                   'SET body=excluded.body,revision=excluded.revision,provenance=excluded.provenance,'
                   'origins=excluded.origins,refreshed=excluded.refreshed,through=excluded.through',
                   (agent.settings.guild_id, record['user_id'], text, record['revision'],
                    json.dumps([{'user_id': u, 'revision': r} for u, r in sorted(provenance)]), json.dumps(origins),
                    now, record['through']))
        # What it drew on: the messages Fri says it sent of those the claim named (all those of the conversations it
        # sent, from a Fri that doesn't say), and the earlier summary's where it was sent too. Unknown for either (a
        # claim from before this, or an earlier summary whose own aren't known): none kept, so the next claim says
        # they aren't known (HumanMint/fri#333).
        drawn = record.get('messages')
        if drawn is not None and messages is not None:
            drawn = [m for m in drawn if m['id'] in set(messages)]
        if drawn is not None and earlier is not None:
            drawn = None if earlier.get('messages') is None else [*earlier['messages'], *drawn]
        if drawn is not None and len(drawn) > DRAWN_MOST:
            drawn = None  # Past what Fri checks in one build: not carried on, so a chain of them starts again.
        if drawn is None:
            db.execute('DELETE FROM fri_member_summary_sources WHERE guild_id=? AND user_id=?',
                       (agent.settings.guild_id, record['user_id']))
        else:
            kept_messages = [m for m in drawn if m['conversation_id'] in origins]
            db.execute('INSERT INTO fri_member_summary_sources VALUES (?,?,?,?) ON CONFLICT(guild_id, user_id) DO UPDATE '
                       'SET through=excluded.through,messages=excluded.messages',
                       (agent.settings.guild_id, record['user_id'], record['through'], json.dumps(kept_messages)))
        if messages is not None:  # A Fri that read every message it sent again first: that's a check (fri #349).
            db.execute('INSERT INTO fri_member_summary_rechecks(guild_id,user_id,through,checked,held) VALUES (?,?,?,?,0) '
                       "ON CONFLICT(guild_id, user_id) DO UPDATE SET through=excluded.through,checked=excluded.checked,"
                       "held=0,claim='',claimed=0", (agent.settings.guild_id, record['user_id'], record['through'], now))
        return {'kept': bool(text)}


def taken_back(db, guild, messages):
    """Whether the controller knows one of these messages was taken back after its exchange was saved: withdrawn,
    deleted or fenced by ID (gene #149), its exchange gone or not. One taken back before is an edit Fri answered again
    under the same ID, and the summary drew on what it says now. Both times are the controller's own."""
    rows = json.dumps([[str(m['id']), m['saved_at']] for m in messages])
    found = ("EXISTS (SELECT 1 FROM {} x WHERE x.guild_id=? AND x.message_id=json_extract(m.value,'$[0]') AND "
             "x.at>json_extract(m.value,'$[1]'))")
    tables = ('chat_turn_withdrawals', 'chat_turn_deletions', 'chat_message_fences')
    return bool(db.execute(f"SELECT 1 FROM json_each(?) m WHERE {' OR '.join(found.format(t) for t in tables)} LIMIT 1",
                           (rows, guild, guild, guild)).fetchone())


def recheck(agent, fresh=False):
    """Kept summaries due a recheck, for Fri to read their messages again from Discord (HumanMint/fri#349): those no
    check covers, those held back until one does, and those last checked over RECHECK ago, at most READS messages past
    the first, none claimed in the last CLAIM_TIMEOUT. A rebuild checks its messages too, but needs new exchanges, so
    one whose exchanges aged out was never checked again. Each names its messages, never its member.

    As it claims: one whose sources aren't known (written before they were recorded, or past DRAWN_MOST) can never be
    checked, so it goes rather than being held back unused until it expires, and the next build starts from what can
    be; one of whose messages the controller knows was taken back goes too, without Fri reading anything (a withdrawal
    by ID finds no exchange holding an aged-out message, so it spares the summary).

    fresh: Fri's first claim since it started, after which deletions it never heard of may have happened. Every summary
    is held back until it's rechecked."""
    now = time.time()
    with agent.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        guild = agent.settings.guild_id
        prune(db, now)
        if fresh:
            db.execute('INSERT INTO fri_member_summary_rechecks(guild_id,user_id,through,checked,held) SELECT guild_id,'
                       "user_id,through,0,1 FROM fri_member_summaries WHERE guild_id=? AND body<>'' ON CONFLICT(guild_id,"
                       "user_id) DO UPDATE SET through=excluded.through,held=1,claim='',claimed=0", (guild,))
        due = db.execute(
            'SELECT s.user_id FROM fri_member_summaries s LEFT JOIN fri_member_summary_rechecks r ON '
            "r.guild_id=s.guild_id AND r.user_id=s.user_id AND r.through=s.through WHERE s.guild_id=? AND s.body<>'' "
            'AND coalesce(r.claimed,0)<=? AND (r.held IS NULL OR r.held OR r.checked<=?) '
            'ORDER BY coalesce(r.held,0) DESC, coalesce(r.checked,0), s.user_id',
            (guild, now-CLAIM_TIMEOUT, now-RECHECK)).fetchall()
        offered, reads = [], 0
        for (user,) in due:
            row = kept(db, guild, user)
            if row is None:
                continue  # Lapsed, and gone.
            messages = drawn(db, guild, user, row)
            if messages is None or taken_back(db, guild, messages):
                drop(db, guild, user)
                continue
            if not messages:  # The member's own words: nothing to read again, so checked now, and shown.
                db.execute('INSERT INTO fri_member_summary_rechecks(guild_id,user_id,through,checked,held) VALUES '
                           "(?,?,?,?,0) ON CONFLICT(guild_id, user_id) DO UPDATE SET through=excluded.through,"
                           "checked=excluded.checked,held=0,claim='',claimed=0", (guild, user, row['through'], now))
                continue
            if offered and reads+len(messages) > READS:
                break
            token = uuid.uuid4().hex
            db.execute('INSERT INTO fri_member_summary_rechecks VALUES (?,?,?,0,0,?,?) ON CONFLICT(guild_id, user_id) '
                       'DO UPDATE SET claim=excluded.claim,claimed=excluded.claimed',
                       (guild, user, row['through'], token, now))
            offered.append({'id': token, 'messages': messages})
            reads += len(messages)
        return {'summaries': offered}


def rechecked(agent, recheck_id, status):
    """How a recheck went (see recheck). ok: every message is still there as it was saved, so the summary shows, and
    isn't due again for RECHECK. unchecked: Discord couldn't say (a message unreadable now, a request failing, or a
    withdrawal of one still waiting in Fri's queue): held back until a later recheck says ok. changed: one is gone, or
    edited since its exchange was saved, and Fri has withdrawn it as it does: the summary goes now, as a withdrawal of
    it would take it. A recheck of a summary replaced since is refused as expired: the new one stands. An ok rests on
    reads made since the claim, so one of its messages the controller has since recorded as taken back takes the
    summary all the same (Sol round 1 of #214)."""
    now = time.time()
    with agent.store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        guild = agent.settings.guild_id
        row = db.execute('SELECT user_id,through FROM fri_member_summary_rechecks WHERE guild_id=? AND claim=?',
                         (guild, recheck_id)).fetchone()
        if row is None:
            raise ValueError('That summary recheck expired')
        if status == 'ok':
            summary = kept(db, guild, row['user_id'])
            messages = drawn(db, guild, row['user_id'], summary) if summary else None
            if messages is None or taken_back(db, guild, messages):
                status = 'changed'
        if status == 'changed':
            drop(db, guild, row['user_id'])
        else:
            db.execute("UPDATE fri_member_summary_rechecks SET claim='',checked=CASE WHEN ? THEN ? ELSE checked END,"
                       'held=? WHERE guild_id=? AND user_id=?', (status == 'ok', now, status != 'ok', guild,
                                                                  row['user_id']))
        return {'ok': True}


def edit(store, guild, conversation, user, revision, source, action, text=''):
    """The member's own edit or clear, from My memory in a private chat, where only they see it.

    Like a profile note's, since it reached every conversation in the server: their saved chats there go,
    and the revision moves, so nothing built on the old words (a build in flight included) outlives it.
    An edit is in their own words, so it draws on no one and shows wherever a summary may.
    """
    source = ChatSource.model_validate(source).check(guild, conversation, user)
    if not -5 <= time.time()-source.timestamp().timestamp() <= 300:
        raise ValueError('Use a fresh memory control')
    if not conversation.startswith('private:'):
        raise ValueError('Change your summary from My memory in your DMs')
    if action == 'replace':
        text = checked(text)
        if not text or len(text) < 5:
            raise ValueError('Keep it to a short line, without links, mentions or code.')
    elif action != 'remove' or text:
        raise ValueError('Choose Edit or Clear for your summary')
    from .fri_memory_store import SERVER, cleared, entries, fences
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        if revision != (current[0] if current else 0):
            raise ValueError('Memory changed. Open My memory again.')
        if not view(read(db, guild, user)):
            raise ValueError('There’s no summary to change now. Open My memory again.')
        # A server-wide removal fence, as a profile note edit records: nothing they sent before this, queued or
        # still in a burst, is kept or learned afterwards, so it can't rebuild what they cleared.
        for fence in fences():  # Never earlier than it was, even with Gene's clock stepped back.
            db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) ON CONFLICT(guild_id,channel_id,'
                       'user_id,digest) DO UPDATE SET at=max(at,excluded.at)', (guild, SERVER, user, fence, time.time()))
        cleared(store, db, guild, user, SERVER, revision)
        if action == 'replace':
            now = time.time()
            db.execute("INSERT INTO fri_member_summaries VALUES (?,?,?,?,'[]','[]',?,?)",
                       (guild, user, text, store._privacy_state(db, user)[0], now, now))
            # Their own words draw on no message: known, and empty, so a recheck keeps them (Sol round 1 of #214).
            db.execute("INSERT INTO fri_member_summary_sources VALUES (?,?,?,'[]') ON CONFLICT(guild_id, user_id) "
                       'DO UPDATE SET through=excluded.through,messages=excluded.messages', (guild, user, now))
    return entries(store, guild, conversation, user, summaries=True)


def purge(path):
    """Run before rolling the controller back to a release that can neither show nor erase summaries."""
    if not Path(path).is_file():
        raise SystemExit(f'Nothing changed. {path} is not a controller database.')
    db = sqlite3.connect(path)
    try:
        with db:
            # An older controller can't tell which saved replies drew on a summary, so none keeps its words.
            columns = {row[1] for row in db.execute('PRAGMA table_info(chat_turns)')}
            if 'summary_dependencies' in columns:  # Nor which depended on another member's (fri #193).
                retire_dependents(db, '1')
            retire_writes(db, '1')  # Nor which work and reminders did (fri #216).
            if 'summary_generation' in columns:
                retire(db, '1')
            db.execute('DROP TABLE IF EXISTS fri_member_summaries')
            db.execute('DROP TABLE IF EXISTS fri_member_summary_calls')
            db.execute('DROP TABLE IF EXISTS fri_member_summary_generations')
            db.execute('DROP TABLE IF EXISTS fri_member_summary_sources')
            db.execute('DROP TABLE IF EXISTS fri_member_summary_rechecks')
            for name in ('fri_member_summary_claim_revised', 'fri_member_summary_claim_revision_moved',
                         'fri_own_summary_writes_work_gone', 'fri_own_summary_writes_reminder_gone'):
                db.execute(f'DROP TRIGGER IF EXISTS {name}')  # Nothing left for them to clear.
            db.execute('DROP TABLE IF EXISTS fri_own_summary_writes')  # Nothing is held without summaries.
            db.execute('DELETE FROM agent_state WHERE key IN (?,?)', (CLAIM, NEXT))
            # Nothing mirrored outlives the summaries (see expiry_triggers): their refresh times go with them.
            db.execute('DELETE FROM agent_state WHERE substr(key,1,?)=?', (len(MIRROR), MIRROR))
    finally:
        db.close()


class Finish(BaseModel):
    summary: str = Field(default='', max_length=4*LENGTH)
    status: str = Field(pattern=r'^(completed|failed|incomplete|interrupted|invalid_output|skipped)$')
    conversations: list[str] = Field(default_factory=list, max_length=EXCHANGES)
    previous: bool = False
    # The messages Fri sent, of those the claim named (HumanMint/fri#333); None from a Fri that doesn't say.
    messages: list[str] | None = Field(default=None, max_length=EXCHANGES*10+DRAWN_MOST)


class Recheck(BaseModel):
    fresh: bool = False


class Rechecked(BaseModel):
    status: str = Field(pattern=r'^(ok|unchecked|changed)$')


def register(app, agent, auth):
    @app.post('/agent/member-summary/claim', dependencies=[auth('fri')])
    def claim_summary():
        return claim(agent)

    @app.post('/agent/member-summary/recheck', dependencies=[auth('fri')])
    def recheck_summaries(body: Recheck):
        return recheck(agent, body.fresh)

    @app.post('/agent/member-summary/recheck/{recheck_id}', dependencies=[auth('fri')])
    def summary_rechecked(recheck_id: str, body: Rechecked):
        if not re.fullmatch(r'[a-f0-9]{32}', recheck_id):
            raise ValueError('That summary recheck expired')
        return rechecked(agent, recheck_id, body.status)

    @app.post('/agent/member-summary/{claim_id}/finish', dependencies=[auth('fri')])
    def finish_summary(claim_id: str, body: Finish):
        if not re.fullmatch(r'[a-f0-9]{32}', claim_id):
            raise ValueError('That summary build expired')
        return finish(agent, claim_id, body.summary, body.status, body.conversations, body.previous,
                      body.messages)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != 'purge':
        raise SystemExit('usage: python -m gene.fri_member_summary purge <database>')
    purge(argv[1])
    print('Member summaries removed.')


if __name__ == '__main__':
    main()
