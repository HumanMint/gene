"""Channels and threads deleted in Discord (HumanMint/fri#358 D1).

Deleting a channel or a thread deletes every message in it, and Discord sends no event for each one, so nothing Fri
withdraws by message ID reaches what was saved from it: a server-profile note learned there still reached a later
private-panel request. Fri asks for the channel instead (see withdraw). Everything the controller holds from it, and
from its threads, goes as a deleted message's does, whatever Fri's own cache still knows: exchanges, notes, a question
left open, research, member and history summaries drawing on it, what exploring learned there.

The controller keeps which channel each thread is under, from the turns Fri saves there (see remember_parent) or as Fri
names them (see parents), so a channel's withdrawal takes threads Fri no longer holds in its cache too. And it keeps a
record of each channel withdrawn, its ID and when, never words (see withdrawn): a save still on its way from it keeps
nothing, as one from a deleted message's fence keeps nothing. The database itself keeps a withdrawn channel's exchanges,
notes and questions out (see TRIGGERS), so an older controller, in a rollback window, writes none there either.
"""

import json
import sqlite3
import time

CAPABILITY = 'channel-withdrawals'
KEPT_SECONDS = 90*86400  # As long as a note: the longest anything withdrawn here could otherwise have stayed.
MOST = 100  # Channels and threads named at once.
TABLES = '''
CREATE TABLE IF NOT EXISTS chat_channel_withdrawals (
    guild_id TEXT NOT NULL, channel_id TEXT NOT NULL, at REAL NOT NULL, PRIMARY KEY(guild_id, channel_id));
CREATE TABLE IF NOT EXISTS chat_thread_parents (
    guild_id TEXT NOT NULL, thread_id TEXT NOT NULL, parent_id TEXT NOT NULL, at REAL NOT NULL,
    PRIMARY KEY(guild_id, thread_id));
CREATE INDEX IF NOT EXISTS chat_thread_parents_by_parent ON chat_thread_parents(guild_id, parent_id);
'''
# A public conversation's channel or thread is the part after "public:". Correlated EXISTS with the ID worked out of
# NEW, so each is one primary-key lookup (and runs alike on SQLite 3.40, production's, and later ones).
WITHDRAWN_HERE = ("{column} LIKE 'public:%' AND EXISTS (SELECT 1 FROM chat_channel_withdrawals w "
                  "WHERE w.guild_id=NEW.guild_id AND w.channel_id=substr(NEW.{column}, 8))")
TRIGGERS = f'''
CREATE TRIGGER IF NOT EXISTS chat_turn_in_withdrawn_channel AFTER INSERT ON chat_turns
WHEN NEW.{WITHDRAWN_HERE.format(column='channel_id')}
BEGIN DELETE FROM chat_turns WHERE id=NEW.id; END;
CREATE TRIGGER IF NOT EXISTS personal_note_from_withdrawn_channel AFTER INSERT ON personal_notes
WHEN (NEW.{WITHDRAWN_HERE.format(column='channel_id')}) OR (NEW.{WITHDRAWN_HERE.format(column='origin')})
BEGIN DELETE FROM personal_notes WHERE id=NEW.id; END;
CREATE TRIGGER IF NOT EXISTS personal_note_moved_to_withdrawn_channel AFTER UPDATE OF channel_id, origin ON personal_notes
WHEN (NEW.{WITHDRAWN_HERE.format(column='channel_id')}) OR (NEW.{WITHDRAWN_HERE.format(column='origin')})
BEGIN DELETE FROM personal_notes WHERE id=NEW.id; END;
CREATE TRIGGER IF NOT EXISTS chat_pending_in_withdrawn_channel AFTER INSERT ON chat_pending
WHEN NEW.{WITHDRAWN_HERE.format(column='conversation_id')}
BEGIN DELETE FROM chat_pending WHERE guild_id=NEW.guild_id AND conversation_id=NEW.conversation_id
    AND user_id=NEW.user_id; END;
'''
TRIGGER_NAMES = ('chat_turn_in_withdrawn_channel', 'personal_note_from_withdrawn_channel',
                 'personal_note_moved_to_withdrawn_channel', 'chat_pending_in_withdrawn_channel')
# Tables holding what a public conversation saved, by conversation ("public:<id>"), and those keyed by the bare ID,
# with no server named (history summaries and exploring, which read only Fri's own server).
BY_CONVERSATION = (('chat_pending', 'conversation_id'), ('web_requests', 'channel_id'), ('web_research', 'channel_id'),
                   ('mention_grants', 'conversation_id'))
BY_CHANNEL = ('agent_summaries', 'fri_explore_notes', 'fri_explore_members', 'fri_explore_queries', 'fri_explore_seen')


def initialize(db):
    """The record of channels withdrawn and of threads' channels, and the triggers that keep a withdrawn one out. Run
    once the tables they act on exist, with every column they name."""
    db.executescript(TABLES + TRIGGERS)


def marks(values):
    return ','.join('?'*len(values))


def channel_of(conversation):
    """The channel or thread of a public conversation, or None for a private one."""
    kind, _, channel = str(conversation or '').partition(':')
    return channel if kind == 'public' and channel.isdigit() else None


def withdrawn(db, guild, channels):
    """Whether any of these channels or threads (bare IDs) was withdrawn (see withdraw)."""
    channels = sorted({str(channel) for channel in channels if channel})
    return bool(channels) and bool(db.execute(
        f'SELECT 1 FROM chat_channel_withdrawals WHERE guild_id=? AND channel_id IN ({marks(channels)})',
        (guild, *channels)).fetchone())


def turn_withdrawn(db, guild, conversation, source=None, burst=()):
    """Whether a turn in this conversation, from this message (and burst), comes from a withdrawn channel."""
    return withdrawn(db, guild, [channel_of(conversation), (source or {}).get('channel_id'),
                                 *((row.get('source') or {}).get('channel_id') for row in burst)])


def remember_parent(store, guild, conversation, parent):
    """A thread's channel, as a turn saved there names it (the scope's channel is the one a thread is under)."""
    thread = channel_of(conversation)
    if thread and str(parent).isdigit() and thread != str(parent):
        with store.connection() as db:
            db.execute('INSERT INTO chat_thread_parents VALUES (?,?,?,?) ON CONFLICT(guild_id,thread_id) DO UPDATE SET '
                       'parent_id=excluded.parent_id,at=excluded.at', (guild, thread, str(parent), time.time()))


def parents(store, guild, named):
    """Threads' channels as Fri names them: for threads a turn saved before the controller kept them."""
    now = time.time()
    with store.connection() as db:
        db.executemany('INSERT INTO chat_thread_parents VALUES (?,?,?,?) ON CONFLICT(guild_id,thread_id) DO UPDATE SET '
                       'parent_id=excluded.parent_id,at=excluded.at',
                       [(guild, thread, parent, now) for thread, parent in sorted(named.items()) if thread != parent])
    return {'ok': True}


def rows(db, query, *args):
    """A query over a table this store may never have set up (one another module makes on first use): none then."""
    try:
        return db.execute(query, args).fetchall()
    except sqlite3.OperationalError:
        return []


def held(store, guild):
    """Every channel or thread the controller holds anything saved from (see withdraw), with the channel a thread is
    under where it's known; none withdrawn. For Fri's check of those it may have missed the deletion of."""
    found = set()
    with store.connection() as db:
        for query in ('SELECT DISTINCT channel_id FROM chat_turns WHERE guild_id=?',
                      'SELECT DISTINCT channel_id FROM personal_notes WHERE guild_id=?',
                      'SELECT DISTINCT origin FROM personal_notes WHERE guild_id=?',
                      'SELECT DISTINCT o.value FROM fri_member_summaries s, json_each(s.origins) o WHERE s.guild_id=?',
                      *(f'SELECT DISTINCT {column} FROM {table} WHERE guild_id=?' for table, column in BY_CONVERSATION)):
            found |= {channel for row in rows(db, query, guild) if (channel := channel_of(row[0]))}
        for table in BY_CHANNEL:
            found |= {str(row[0]) for row in rows(db, f'SELECT DISTINCT channel_id FROM {table}') if str(row[0]).isdigit()}
        gone = {row[0] for row in db.execute('SELECT channel_id FROM chat_channel_withdrawals WHERE guild_id=?', (guild,))}
        known = dict(db.execute('SELECT thread_id,parent_id FROM chat_thread_parents WHERE guild_id=?', (guild,)).fetchall())
    return {'channels': [{'id': channel, 'parent': known.get(channel)}
                         for channel in sorted(found-gone, key=lambda channel: (len(channel), channel))]}


def withdraw(store, guild, channels):
    """Channels and threads deleted in Discord: everything held from them, and from the threads under each channel,
    goes as each of their messages' deletion would take it (see Store._withdraw_turn), in one transaction. Then a record
    of each keeps anything still on its way from it out (see withdrawn). A repeat finds nothing more."""
    from .fri_agent_store import AgentStore
    from .fri_member_summary import CLAIM, drop
    from .store import Store
    now = time.time()
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        named = sorted({str(channel) for channel in channels})
        under = [row[0] for row in db.execute(f'SELECT thread_id FROM chat_thread_parents WHERE guild_id=? AND '
                                              f'parent_id IN ({marks(named)})', (guild, *named))]
        ids = sorted({*named, *under}, key=lambda channel: (len(channel), channel))
        conversations = ['public:'+channel for channel in ids]
        db.execute('DELETE FROM chat_channel_withdrawals WHERE at<?', (now-KEPT_SECONDS,))
        db.execute('DELETE FROM chat_thread_parents WHERE at<?', (now-KEPT_SECONDS,))
        db.executemany('INSERT INTO chat_channel_withdrawals VALUES (?,?,?) ON CONFLICT(guild_id,channel_id) '
                       'DO UPDATE SET at=excluded.at', [(guild, channel, now) for channel in ids])
        where = marks(conversations)
        # Its exchanges, message by message, as their deletions take them: the turns holding each, a question it left
        # and research it permitted, notes learned from it, the member's summary if it could have read it, and others'
        # replies that carried it, which keep a breadcrumb (gene #141).
        turns = db.execute(f"SELECT id,channel_id,user_id,json_extract(NULLIF(source,''),'$.id') AS source_id,burst,"
                           f"reply_message_id FROM chat_turns WHERE guild_id=? AND channel_id IN ({where})",
                           (guild, *conversations)).fetchall()
        messages = set()
        for turn in turns:
            said = [turn['source_id'], *((row.get('source') or {}).get('id') for row in json.loads(turn['burst'] or '[]'))]
            messages |= {str(message) for message in [*said, turn['reply_message_id']] if message}
            for message in {str(message) for message in said if message}:
                Store._withdraw_turn(db, guild, turn['channel_id'], turn['user_id'], message, deleted=True)
        db.execute(f'DELETE FROM chat_turns WHERE guild_id=? AND channel_id IN ({where})', (guild, *conversations))
        # Notes learned there, wherever they show: a server-profile note keeps the conversation it came from.
        for note in db.execute(f'SELECT sources FROM personal_notes WHERE guild_id=? AND (channel_id IN ({where}) OR '
                               f'origin IN ({where}))', (guild, *conversations, *conversations)).fetchall():
            messages |= {str(message) for message in json.loads(note['sources'] or 'null') or () if message}
        db.execute(f'DELETE FROM personal_notes WHERE guild_id=? AND (channel_id IN ({where}) OR origin IN ({where}))',
                   (guild, *conversations, *conversations))
        # Member summaries drawing on it, and a build of one in flight.
        for summary in rows(db, 'SELECT user_id,origins FROM fri_member_summaries WHERE guild_id=?', guild):
            if set(json.loads(summary['origins'])) & set(conversations):
                drop(db, guild, summary['user_id'])
        claim = AgentStore.get(db, CLAIM)
        if claim and {*claim.get('sources', {}), *((claim.get('previous') or {}).get('origins') or [])} & set(conversations):
            AgentStore.put(db, CLAIM, None)
        for table, column in BY_CONVERSATION:
            rows(db, f'DELETE FROM {table} WHERE guild_id=? AND {column} IN ({where})', guild, *conversations)
        for table in BY_CHANNEL:
            rows(db, f'DELETE FROM {table} WHERE channel_id IN ({marks(ids)})', *ids)
        # Every message of it the controller knew of is deleted: a reply elsewhere that carried one keeps a breadcrumb,
        # and a save still on its way that carried one keeps nothing.
        Store._retire_drawn(db, guild, messages)
        Store._fence(db, guild, sorted(messages), True, now)
        return {'ok': True, 'channels': ids, 'removed': len(turns)}
