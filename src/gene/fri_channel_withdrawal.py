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
# NEW, so each is one primary-key lookup (and runs alike on SQLite 3.40, production's, and later ones). Each is BEFORE
# the write, and refuses it (RAISE(IGNORE)): nothing about the row changes, so an older controller's save of the same
# note from a withdrawn channel, an upsert onto that note saved from another, leaves it as it was (Sol round 1 of #243,
# G1). None of them deletes a row.
WITHDRAWN_HERE = ("{column} LIKE 'public:%' AND EXISTS (SELECT 1 FROM chat_channel_withdrawals w "
                  "WHERE w.guild_id=NEW.guild_id AND w.channel_id=substr(NEW.{column}, 8))")
TRIGGERS = f'''
CREATE TRIGGER IF NOT EXISTS chat_turn_in_withdrawn_channel BEFORE INSERT ON chat_turns
WHEN NEW.{WITHDRAWN_HERE.format(column='channel_id')}
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS personal_note_from_withdrawn_channel BEFORE INSERT ON personal_notes
WHEN (NEW.{WITHDRAWN_HERE.format(column='channel_id')}) OR (NEW.{WITHDRAWN_HERE.format(column='origin')})
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS personal_note_moved_to_withdrawn_channel BEFORE UPDATE OF channel_id, origin
ON personal_notes
WHEN (NEW.{WITHDRAWN_HERE.format(column='channel_id')}) OR (NEW.{WITHDRAWN_HERE.format(column='origin')})
BEGIN SELECT RAISE(IGNORE); END;
CREATE TRIGGER IF NOT EXISTS chat_pending_in_withdrawn_channel BEFORE INSERT ON chat_pending
WHEN NEW.{WITHDRAWN_HERE.format(column='conversation_id')}
BEGIN SELECT RAISE(IGNORE); END;
'''
TRIGGER_NAMES = ('chat_turn_in_withdrawn_channel', 'personal_note_from_withdrawn_channel',
                 'personal_note_moved_to_withdrawn_channel', 'chat_pending_in_withdrawn_channel')
# Tables holding what a public conversation saved, by conversation ("public:<id>"), and those keyed by the bare ID,
# with no server named (what exploring read, which reads only Fri's own server). Learning reports, their cards,
# channel summaries and archive observations go through gene #247's withdraw (see take).
BY_CONVERSATION = (('chat_pending', 'conversation_id'), ('web_requests', 'channel_id'), ('web_research', 'channel_id'),
                   ('mention_grants', 'conversation_id'))
BY_CHANNEL = ('fri_explore_members', 'fri_explore_queries', 'fri_explore_seen')
# The only tables a store may not have yet: each is made by its own module on first use, not when the store opens.
OPTIONAL = frozenset(BY_CHANNEL)


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


def turn_withdrawn(db, guild, conversation, source=None, burst=(), parent=None):
    """Whether a turn in this conversation, from this message (and burst), comes from a withdrawn channel: the thread
    itself, or the channel the turn's scope names it under, whether or not that was known before (Sol round 1 of #243,
    G2). Only a public conversation's scope names a parent: a private one's names the workshop, to authorize it, so it
    never counts (Sol round 2 of #243, B1)."""
    here = channel_of(conversation)
    return withdrawn(db, guild, [here, (source or {}).get('channel_id'), parent if here else None,
                                 *((row.get('source') or {}).get('channel_id') for row in burst)])


def remember_parent(store, guild, conversation, parent):
    """A thread's channel, as a turn saved there names it (the scope's channel is the one a thread is under). Under a
    channel already withdrawn, the thread is withdrawn with it, there and then (see parents)."""
    thread = channel_of(conversation)
    if thread and str(parent).isdigit() and thread != str(parent):
        parents(store, guild, {thread: str(parent)})


def parents(store, guild, named):
    """Threads' channels as Fri names them, or a turn's scope does: for threads saved before the controller kept them.
    A thread named under a channel already withdrawn is withdrawn too, with what was saved there, in the same
    transaction (Sol round 1 of #243, G2)."""
    now = time.time()
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        named = {str(thread): str(parent) for thread, parent in named.items() if str(thread) != str(parent)}
        db.executemany('INSERT INTO chat_thread_parents VALUES (?,?,?,?) ON CONFLICT(guild_id,thread_id) DO UPDATE SET '
                       'parent_id=excluded.parent_id,at=excluded.at',
                       [(guild, thread, parent, now) for thread, parent in sorted(named.items())])
        if gone := sorted(thread for thread, parent in named.items() if withdrawn(db, guild, [parent])):
            take(db, guild, gone, now)
    return {'ok': True}


def rows(db, query, *args, table=None):
    """A query, over `table` where that's one a store may not have set up yet (see OPTIONAL): none then. Any other
    failure (a full disk, say) fails the call, and its transaction (Sol round 1 of #243, G4)."""
    try:
        return db.execute(query, args).fetchall()
    except sqlite3.OperationalError as exc:
        if table in OPTIONAL and str(exc) == f'no such table: {table}':
            return []
        raise


def held(store, guild):
    """Every channel or thread the controller holds anything saved from (see withdraw), with the channel a thread is
    under where it's known. For Fri's check of those it may have missed the deletion of: one withdrawn before is listed
    again only if something was written there since (an older controller's history summary, say), and goes again."""
    found = set()
    with store.connection() as db:
        for query in ('SELECT DISTINCT channel_id FROM chat_turns WHERE guild_id=?',
                      'SELECT DISTINCT channel_id FROM personal_notes WHERE guild_id=?',
                      'SELECT DISTINCT origin FROM personal_notes WHERE guild_id=?',
                      'SELECT DISTINCT o.value FROM fri_member_summaries s, json_each(s.origins) o WHERE s.guild_id=?',
                      *(f'SELECT DISTINCT {column} FROM {table} WHERE guild_id=?' for table, column in BY_CONVERSATION)):
            found |= {channel for row in rows(db, query, guild) if (channel := channel_of(row[0]))}
        for table in BY_CHANNEL:
            found |= {str(row[0]) for row in rows(db, f'SELECT DISTINCT channel_id FROM {table}', table=table)
                      if str(row[0]).isdigit()}
        known = dict(db.execute('SELECT thread_id,parent_id FROM chat_thread_parents WHERE guild_id=?', (guild,)).fetchall())
    return {'channels': [{'id': channel, 'parent': known.get(channel)}
                         for channel in sorted(found, key=lambda channel: (len(channel), channel))]}


def withdraw(store, guild, channels):
    """Channels and threads deleted in Discord: everything held from them, and from the threads under each channel,
    goes, in one transaction (see take). Names the messages it took, for Fri's own fences (HumanMint/fri#372, Sol round
    1, F1)."""
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        return take(db, guild, channels, time.time())


def take(db, guild, channels, now):
    """What withdraw does, in a transaction already open: everything held from these channels, and from the threads
    under each, goes as each of their messages' deletion would take it: exchanges, notes learned there, the summaries
    drawing on them, questions, research, and replies elsewhere that carried one keep a breadcrumb. A record of each
    then keeps anything still on its way from it out (see withdrawn). A repeat finds nothing more."""
    from .fri_agent_store import AgentStore
    from .fri_member_summary import CLAIM, drop
    from .store import Store
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
    # Its exchanges, and the messages they hold: the members', and Fri's replies.
    turns = db.execute(f"SELECT json_extract(NULLIF(source,''),'$.id') AS source_id,burst,reply_message_id "
                       f"FROM chat_turns WHERE guild_id=? AND channel_id IN ({where})", (guild, *conversations))
    turns = turns.fetchall()
    messages = {str(message) for turn in turns for message in (
        turn['source_id'], turn['reply_message_id'],
        *((row.get('source') or {}).get('id') for row in json.loads(turn['burst'] or '[]'))) if message}
    db.execute(f'DELETE FROM chat_turns WHERE guild_id=? AND channel_id IN ({where})', (guild, *conversations))
    # Notes learned there, wherever they show: a server-profile note keeps the conversation it came from.
    for note in db.execute(f'SELECT sources FROM personal_notes WHERE guild_id=? AND (channel_id IN ({where}) OR '
                           f'origin IN ({where}))', (guild, *conversations, *conversations)).fetchall():
        messages |= {str(message) for message in json.loads(note['sources'] or 'null') or () if message}
    db.execute(f'DELETE FROM personal_notes WHERE guild_id=? AND (channel_id IN ({where}) OR origin IN ({where}))',
               (guild, *conversations, *conversations))
    # Member summaries drawing on it, and a build of one in flight; the messages of it each names, which may be all
    # that's left naming them once their exchanges were trimmed (Sol round 1 of #243, G3).
    def said_here(items):
        return {str(item['id']) for item in items or () if isinstance(item, dict) and item.get('id')
                and item.get('conversation_id') in conversations}
    for source in db.execute('SELECT messages FROM fri_member_summary_sources WHERE guild_id=?', (guild,)).fetchall():
        messages |= said_here(json.loads(source['messages']))
    for summary in db.execute('SELECT user_id,origins FROM fri_member_summaries WHERE guild_id=?', (guild,)).fetchall():
        if set(json.loads(summary['origins'])) & set(conversations):
            drop(db, guild, summary['user_id'])
    claim = AgentStore.get(db, CLAIM)
    if claim:
        messages |= said_here(claim.get('messages')) | said_here((claim.get('previous') or {}).get('messages'))
    if claim and {*claim.get('sources', {}), *((claim.get('previous') or {}).get('origins') or [])} & set(conversations):
        AgentStore.put(db, CLAIM, None)
    for table, column in BY_CONVERSATION:
        db.execute(f'DELETE FROM {table} WHERE guild_id=? AND {column} IN ({where})', (guild, *conversations))
    # Learning reports and their cards keep a breadcrumb, channel summaries and archive observations go (gene #247,
    # round 1's G5).
    from .fri_learning_sources import withdraw as withdraw_learning
    withdraw_learning(db, channels=ids)
    for table in BY_CHANNEL:
        rows(db, f'DELETE FROM {table} WHERE channel_id IN ({marks(ids)})', *ids, table=table)
    # Every message of it the controller knew of is deleted, as by its own deletion: a reply elsewhere that carried
    # one keeps a breadcrumb (gene #141), and a save still on its way that carried one keeps nothing (gene #149).
    Store._retire_drawn(db, guild, messages)
    Store._fence(db, guild, sorted(messages), True, now)
    return {'ok': True, 'channels': ids, 'removed': len(turns), 'messages': sorted(messages, key=lambda message: (len(message), message))}
