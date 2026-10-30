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
DAILY = 50  # Builds a day across the server, however many members are active.
CLAIM_TIMEOUT = 600
SKEW = 300  # Seconds a Discord message's time may run ahead of the controller's clock.
IDLE = 600  # Seconds before looking again when nobody is due.
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
    ''')


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


def depend_write(db, table, item, scope, dependencies):
    """A tool's write of the model's words keeps the summaries its turn may repeat through Fri's earlier replies, with
    their generations, so it's retired once one goes (see retire_writes), as a saved reply is (fri #216). Each goes on
    record (see keep), and so is that member's privacy revision now, which a start checks (see settle). Not the writer's
    own: work and reminders drawn on that stay, as they asked for them (gene #112). A draft edited again keeps what it
    had, as the edit may still hold those words."""
    from .store import Store
    key = dict(WRITES)[table]
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
    cancelled, and keep no words unless one is going out now. A reminder not yet sending is cancelled, and none keeps
    its text: delivery already holds what it sends. where: about `d`, each of their dependencies."""
    for table, key in writes(db):
        items = f'(SELECT {key} FROM {table} d WHERE {where})'
        if table == 'fri_work_dependencies':
            db.execute(f'UPDATE fri_work SET title=?,body=?,version=version+1 WHERE id IN {items}',
                       (WORK_TITLE, WORK_RETIRED, *args))
            db.execute("UPDATE fri_posts SET state=CASE WHEN state IN ('pending','authorized','claimed') THEN 'cancelled' "
                       f"ELSE state END,body=CASE WHEN state='sending' THEN body ELSE '' END WHERE work_id IN {items}",
                       args)
        else:
            db.execute("UPDATE fri_reminders SET state=CASE WHEN state IN ('pending','scheduled','claimed') THEN "
                       f"'cancelled' ELSE state END,body='' WHERE id IN {items}", args)
        db.execute(f'DELETE FROM {table} WHERE {key} IN {items}', args)


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
            body=CASE WHEN state='sending' THEN body ELSE '' END
            WHERE work_id IN (SELECT work_id FROM fri_work_dependencies d WHERE {moved});
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
    triggers = (('privacy_revision_inserted', 'AFTER INSERT ON chat_revisions', body),
                ('privacy_revision_moved', 'AFTER UPDATE OF revision ON chat_revisions', body),
                ('message_fences_expire', 'AFTER INSERT ON gene_runtime_health', expire),
                ('message_fences_expire_turns', 'AFTER INSERT ON chat_turns', expire),
                ('message_fences_expire_pending', 'AFTER INSERT ON chat_pending', expire),
                ('message_fences_expire_grants', 'AFTER INSERT ON mention_grants', expire),
                ('message_fences_expire_state', 'AFTER INSERT ON agent_state', expire),
                ('message_fences_expire_state_moved', 'AFTER UPDATE ON agent_state', expire),
                ('message_fences_expire_sessions', 'AFTER INSERT ON agent_sessions', expire),
                ('message_fences_expire_sessions_moved', 'AFTER UPDATE ON agent_sessions', expire),
                ('message_fences_expire_usage', 'AFTER INSERT ON chat_usage', expire))
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


def summary_triggers(db):
    """A member's summary goes (a withdrawal, an expiry, a privacy change, another member's through its provenance) or
    is replaced by a rebuild that wasn't sent it, and an older controller in a rollback window does that without knowing
    what depends on it elsewhere (fri #193, fri #216). These triggers, which it runs too, retire at once what this
    controller retires then (see advance and erase): the member's own saved replies that drew on it, other members'
    replies depending on it with any question they left open, and saved work and reminders depending on it with all
    their links (gene #147, #161, #162). A rebuild sent the summary it replaces keeps its words, revision, origins,
    provenance and a later `through`, so a withdrawal of anything the old one drew on still finds it: what depends on
    it stays, as a covered advance carries it on. A purge drops them with the table; their bodies name only tables
    nothing drops."""
    quoted = {name: text.replace("'", "''") for name, text in
              (('retired', RETIRED), ('title', WORK_TITLE), ('work', WORK_RETIRED))}
    turns = ("chat_turns.guild_id=OLD.guild_id AND ((chat_turns.user_id=OLD.user_id AND chat_turns.summary_generation IS "
             "NOT NULL) OR (chat_turns.summary_dependencies IS NOT NULL AND EXISTS (SELECT 1 FROM "
             "json_each(chat_turns.summary_dependencies) d WHERE json_extract(d.value,'$.user_id')=OLD.user_id)))")

    def depending(table, key, item):  # One of their items depends on the summary.
        return (f'EXISTS (SELECT 1 FROM {table} d WHERE d.{key}={item} AND d.guild_id=OLD.guild_id AND '
                'd.user_id=OLD.user_id)')
    work, reminders = WRITES
    # Correlated EXISTS throughout: SQLite 3.53 skipped a row-value IN in this body under the rebuild trigger's WHEN.
    body = f"""
        DELETE FROM chat_pending WHERE EXISTS (SELECT 1 FROM chat_turns WHERE chat_turns.guild_id=chat_pending.guild_id
            AND chat_turns.user_id=chat_pending.user_id
            AND json_extract(NULLIF(chat_turns.source,''),'$.id')=chat_pending.source_id AND {turns});
        UPDATE chat_turns SET reply='{quoted['retired']}',verbatim=0,summary_generation=NULL,summary_dependencies=NULL
            WHERE {turns};
        UPDATE fri_work SET title='{quoted['title']}',body='{quoted['work']}',version=version+1
            WHERE {depending(*work, 'fri_work.id')};
        UPDATE fri_posts SET state=CASE WHEN state IN ('pending','authorized','claimed') THEN 'cancelled' ELSE state END,
            body=CASE WHEN state='sending' THEN body ELSE '' END WHERE {depending(*work, 'fri_posts.work_id')};
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
    triggers = (('member_summary_gone', 'AFTER DELETE ON fri_member_summaries FOR EACH ROW'),
                ('member_summary_replaced', f'AFTER UPDATE ON fri_member_summaries FOR EACH ROW WHEN NOT ({covered})'))
    # Replaced, so a body changed since an earlier controller made them takes effect, in one transaction, so a start cut
    # off between them, or an older controller starting meanwhile, never finds one missing (as erasure_triggers does).
    began = not db.in_transaction
    if began:
        db.execute('BEGIN IMMEDIATE')
    for name, event in triggers:
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
        db.execute(f'CREATE TRIGGER {name} {event} BEGIN {body} END')
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
    advance(db, guild, user)


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


def _current(db, row):
    from .store import Store
    return (row['refreshed'] >= time.time()-TTL and Store._privacy_state(db, row['user_id']) == (row['revision'], True)
            and all(Store._privacy_state(db, s['user_id']) == (s['revision'], True) for s in json.loads(row['provenance'])))


def read(db, guild, user):
    """The member's summary row, or None. An empty body is a build that found nothing worth keeping."""
    row = db.execute('SELECT * FROM fri_member_summaries WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
    if row and not _current(db, row):
        drop(db, guild, user)  # Expired, say, while a reply holding it was still being written.
        return None
    return row


def lapsed(db, guild, user):
    """Whether the member had a summary that lapsed (expired, say, with nothing pruning it yet). It goes now, which
    moves their generation, so a turn or write that drew on it can't keep its words (gene #113)."""
    row = db.execute('SELECT 1 FROM fri_member_summaries WHERE guild_id=? AND user_id=?', (guild, user)).fetchone()
    return bool(row) and read(db, guild, user) is None


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
        if (pending and now-pending['at'] < CLAIM_TIMEOUT) or now < agent.get(db, NEXT, 0):
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
            previous = read(db, guild, user)
            # Only words said since the member's latest privacy change: one invalidates what came before.
            turns = [dict(row) for row in db.execute(
                f'SELECT id,channel_id,input,provenance,at FROM chat_turns WHERE {READABLE} AND at>? '
                'AND speaker_revision=? ORDER BY at DESC, id DESC LIMIT ?',
                (guild, user, previous['through'] if previous else 0, revision, EXCHANGES))][::-1]
            turns = agent.store._current_turns(db, turns)
            if len(turns) < NEW:
                continue
            sources = {}
            for turn in turns:
                sources.setdefault(turn['channel_id'], set()).update(
                    (s['user_id'], s['revision']) for s in turn['provenance'])
            shown = view(previous)
            db.execute('INSERT INTO fri_member_summary_calls VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET at=excluded.at',
                       (user, now))  # Every call counts, whatever happens to it.
            record = {'id': uuid.uuid4().hex, 'at': now, 'user_id': user, 'revision': revision,
                      'through': max(turn['at'] for turn in turns),
                      'sources': {c: sorted(map(list, s)) for c, s in sources.items()},
                      'previous': {k: shown[k] for k in ('provenance', 'origins')} if shown else None}
            agent.put(db, CLAIM, record)
            return {'id': record['id'], 'model': config['summary_model'], 'effort': config['summary_effort'],
                    'previous': {'text': shown['text'], 'origins': shown['origins']} if shown else None,
                    'exchanges': [{'conversation_id': turn['channel_id'], 'message': neutral(turn['input'])}
                                  for turn in turns]}
        agent.put(db, NEXT, now+IDLE)
        return None


def finish(agent, claim_id, summary, status, conversations, previous):
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
        return {'kept': bool(text)}


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
        for fence in fences():
            db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) ON CONFLICT(guild_id,channel_id,'
                       'user_id,digest) DO UPDATE SET at=excluded.at', (guild, SERVER, user, fence, time.time()))
        cleared(store, db, guild, user, SERVER, revision)
        if action == 'replace':
            now = time.time()
            db.execute("INSERT INTO fri_member_summaries VALUES (?,?,?,?,'[]','[]',?,?)",
                       (guild, user, text, store._privacy_state(db, user)[0], now, now))
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
            db.execute('DELETE FROM agent_state WHERE key IN (?,?)', (CLAIM, NEXT))
    finally:
        db.close()


class Finish(BaseModel):
    summary: str = Field(default='', max_length=4*LENGTH)
    status: str = Field(pattern=r'^(completed|failed|incomplete|interrupted|invalid_output|skipped)$')
    conversations: list[str] = Field(default_factory=list, max_length=EXCHANGES)
    previous: bool = False


def register(app, agent, auth):
    @app.post('/agent/member-summary/claim', dependencies=[auth('fri')])
    def claim_summary():
        return claim(agent)

    @app.post('/agent/member-summary/{claim_id}/finish', dependencies=[auth('fri')])
    def finish_summary(claim_id: str, body: Finish):
        if not re.fullmatch(r'[a-f0-9]{32}', claim_id):
            raise ValueError('That summary build expired')
        return finish(agent, claim_id, body.summary, body.status, body.conversations, body.previous)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2 or argv[0] != 'purge':
        raise SystemExit('usage: python -m gene.fri_member_summary purge <database>')
    purge(argv[1])
    print('Member summaries removed.')


if __name__ == '__main__':
    main()
