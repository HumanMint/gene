"""Small exact preferences with item controls and fences against historical re-learning.

A note's scope is the conversation it may appear in, or SERVER: the member's profile, said where
everyone in the server could read it, which follows them into every conversation in that server.
"""

import hashlib
import json
import time

from .fri_context import ChatSource
from .fri_memory import CATEGORIES, MAX_NOTES, PER_CATEGORY, TTL, personal_note

SERVER = 'server'
PROFILE_NOTES = 15  # Every learned category full, at PER_CATEGORY each. Projects get there only by the member's Share.
HORIZON = 5000*86400  # The deepest archive search (exploration_days); see removed().


def initialize(db):
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='personal_notes'").fetchone()[0]
    if 'UNIQUE(guild_id,channel_id,user_id,category)' in sql:
        db.execute('SAVEPOINT personal_notes_upgrade')
        db.execute('ALTER TABLE personal_notes RENAME TO personal_notes_v1')
        db.execute('''CREATE TABLE personal_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                user_id TEXT NOT NULL, category TEXT NOT NULL, evidence TEXT NOT NULL, at REAL NOT NULL,
                UNIQUE(guild_id,channel_id,user_id,category,evidence))''')
        db.execute('INSERT INTO personal_notes SELECT * FROM personal_notes_v1')
        db.execute('DROP TABLE personal_notes_v1')
        db.execute('RELEASE personal_notes_upgrade')
    if 'origin' not in {row[1] for row in db.execute('PRAGMA table_info(personal_notes)')}:
        # Where a profile note was said, shown only to its member. Conversation notes stay where they are.
        db.execute('ALTER TABLE personal_notes ADD COLUMN origin TEXT')
    if 'provenance' not in {row[1] for row in db.execute('PRAGMA table_info(personal_notes)')}:
        # Other members whose words the saving turn drew on: a member's message can quote someone else.
        # NULL is unknown: a note saved before this, or by a turn that didn't say. It stays where it was said.
        db.execute('ALTER TABLE personal_notes ADD COLUMN provenance TEXT')
    if 'sources' not in {row[1] for row in db.execute('PRAGMA table_info(personal_notes)')}:
        # The Discord messages the note was learned from (gene #141), so it goes when one is withdrawn: a JSON list,
        # [] for words the member typed in a memory control. NULL is unknown: saved before this, or by an older
        # controller, which leaves it alone. Such a note stays when a message is withdrawn (see Store._withdraw_notes).
        db.execute('ALTER TABLE personal_notes ADD COLUMN sources TEXT')
    if 'sources_at' not in {row[1] for row in db.execute('PRAGMA table_info(personal_notes)')}:
        # The note's time when those sources were recorded (gene #145 round 2). An older controller that learns the
        # same note again moves its time and leaves both, so a note whose time moved since has stale sources: it's
        # judged as one of unknown source (see CURRENT), never by messages that may not be all that say it now.
        db.execute('ALTER TABLE personal_notes ADD COLUMN sources_at REAL')
    # A member's notes across their conversations: a withdrawal's settling at start asks for them once per fence (gene
    # #145), and the unique key leads with the channel. An older controller keeps it up to date and never reads it.
    db.execute('CREATE INDEX IF NOT EXISTS personal_notes_by_member ON personal_notes(guild_id,user_id)')
    db.execute('CREATE TABLE IF NOT EXISTS personal_note_removals (guild_id TEXT,channel_id TEXT,'
               'user_id TEXT,digest TEXT,at REAL,PRIMARY KEY(guild_id,channel_id,user_id,digest))')
    # When the member last turned memory off, which erased everything else of theirs, removals included: their ID and
    # a time, kept across turning it back on as a removal cutoff in every conversation (HumanMint/fri#321). An older
    # controller never reads it.
    db.execute('CREATE TABLE IF NOT EXISTS personal_erasures (user_id TEXT PRIMARY KEY,at REAL NOT NULL)')
    # And when the Discord message or interaction that asked for it was sent, on Discord's clock, with Gene's stamp of
    # the erasure it belongs to (HumanMint/fri#337): a message's snowflake is compared with it, not with Gene's clock.
    # Its own table, so an older controller's writes to the one above never meet it.
    db.execute('CREATE TABLE IF NOT EXISTS personal_erasure_times (user_id TEXT PRIMARY KEY,at REAL NOT NULL,'
               'discord_at REAL NOT NULL)')
    # And a member's latest note removal in each conversation (or the profile), on Discord's clock: when the panel press
    # that asked was sent, with Gene's stamp of the removals it belongs to (HumanMint/fri#368), as for an erasure above.
    # Its own table: an older controller's removals, which write Gene's clock alone, never meet it.
    db.execute('CREATE TABLE IF NOT EXISTS personal_removal_times (guild_id TEXT,channel_id TEXT,user_id TEXT,'
               'at REAL NOT NULL,discord_at REAL NOT NULL,PRIMARY KEY(guild_id,channel_id,user_id))')
    # A removal's time an older controller in a rollback window took away (HumanMint/gene#219): deleted while still in
    # force, as its erasure deletes every removal of the member and stamps its own time, which can be earlier; or moved
    # back, as its removal writes its own time over a later one. The database keeps the later time here (see
    # floor_triggers) until this Gene's next start puts it back (see reconcile). Its own table, which an older
    # controller never reads or drops.
    db.execute('CREATE TABLE IF NOT EXISTS personal_cutoff_floors (guild_id TEXT,channel_id TEXT,user_id TEXT,'
               'digest TEXT,at REAL NOT NULL,PRIMARY KEY(guild_id,channel_id,user_id,digest))')
    floor_triggers(db)
    reconcile(db)


# What floor_triggers keep: the removal as it was, at its later time if two meet.
KEPT = ('INSERT INTO personal_cutoff_floors VALUES (OLD.guild_id,OLD.channel_id,OLD.user_id,OLD.digest,OLD.at) '
        'ON CONFLICT(guild_id,channel_id,user_id,digest) DO UPDATE SET at=max(at,excluded.at);')


def floor_triggers(db):
    """Whoever writes the removals, an older controller too, the database keeps each one's later time when it's
    - deleted within TTL of being written, as 4ffd668's erasure deletes every removal of the member before stamping
      the erasure with its own clock. Expiry deletes only what's past TTL (per-phrase removals) or HORIZON (wordless
      ones), so it keeps nothing;
    - moved back, as 4ffd668's removal writes its own time over a later one. This Gene never moves one back (see
      edit). Putting it back at once would refuse the older controller's own replacement after it, which it doesn't
      check, so it's put back at this Gene's next start.
    And an erasure's stamp is never moved back, as 4ffd668's next erasure would with its clock behind (an upsert of
    its own time): the later one stays, at once, which only ever holds more back, and its cutoff on Discord's clock is
    at least the new stamp, so a reader that trusts it (see erased_on_discord) never reads less than the new erasure.
    Names of their own, which no older controller drops or makes, replaced at each start so a changed body takes
    effect; in one transaction, so a start cut off between them never leaves one missing."""
    began = not db.in_transaction
    if began:
        db.execute('BEGIN IMMEDIATE')
    for name, event, body in (
            ('note_removal_kept_as_floor', 'AFTER DELETE ON personal_note_removals '
                                           f"WHEN OLD.at>=(julianday('now')-2440587.5)*86400.0-{TTL}", KEPT),
            ('note_removal_kept_when_moved_back', 'AFTER UPDATE OF at ON personal_note_removals WHEN NEW.at<OLD.at',
             KEPT),
            ('erasure_kept_when_moved_back', 'AFTER UPDATE OF at ON personal_erasures WHEN NEW.at<OLD.at',
             'UPDATE personal_erasure_times SET discord_at=max(discord_at,NEW.at) WHERE user_id=NEW.user_id; '
             'UPDATE personal_erasures SET at=OLD.at WHERE user_id=NEW.user_id;')):
        db.execute(f'DROP TRIGGER IF EXISTS {name}')
        db.execute(f'CREATE TRIGGER {name} {event} BEGIN {body} END')
    if began:
        db.commit()


def reconcile(db):
    """At start, what an older controller took away in a rollback window (see floor_triggers) is put back
    (HumanMint/gene#219), as this Gene's own writes would have kept it:
    - a removal it moved back gets its later time again;
    - removals it deleted are carried into the member's erasure, as personal_control does; deleted with no erasure
      (a rollback's copies of profile removals, say) they change nothing;
    - and its erasure, stamped with its own clock, is kept at or past this Gene's earlier stamp and cutoff, as
      record_erasure keeps its own. Its Discord time isn't known, so its cutoff is its stamp or the one in force,
      whichever is later;
    - and a removal's time on Discord's clock (see record_removal) whose removals went while an older controller ran
      (its erasure, which never knew that time) is carried into the member's erasure too, whatever else that erasure
      left: Gene main before HumanMint/fri#368 carries the removals, on Gene's clock alone, and its own floors with
      them (Sol round 1 of #234, B1). Then that record goes, as this Gene's own erasure deletes it.
    Then no floor is left. A rollback to any older Gene, this one's predecessor included, never leaves a cutoff earlier
    than one this Gene had in force."""
    began = not db.in_transaction
    if began:
        db.execute('BEGIN IMMEDIATE')
    match = ('f.guild_id=personal_note_removals.guild_id AND f.channel_id=personal_note_removals.channel_id AND '
             'f.user_id=personal_note_removals.user_id AND f.digest=personal_note_removals.digest')
    db.execute(f'UPDATE personal_note_removals SET at=(SELECT f.at FROM personal_cutoff_floors f WHERE {match}) '
               f'WHERE EXISTS (SELECT 1 FROM personal_cutoff_floors f WHERE {match} AND f.at>personal_note_removals.at)')
    deleted = {row[0]: row[1] for row in db.execute(
        'SELECT user_id,max(at) FROM personal_cutoff_floors f WHERE NOT EXISTS (SELECT 1 FROM personal_note_removals r '
        'WHERE r.guild_id=f.guild_id AND r.channel_id=f.channel_id AND r.user_id=f.user_id AND r.digest=f.digest) '
        'GROUP BY user_id')}
    older = {row[0] for row in db.execute('SELECT e.user_id FROM personal_erasures e JOIN personal_erasure_times t '
                                          'ON t.user_id=e.user_id WHERE t.at IS NOT e.at')}
    orphaned = (' FROM personal_removal_times WHERE NOT EXISTS (SELECT 1 FROM personal_note_removals r WHERE '
                'r.guild_id=personal_removal_times.guild_id AND r.channel_id=personal_removal_times.channel_id AND '
                'r.user_id=personal_removal_times.user_id)')
    left = {row[0] for row in db.execute('SELECT DISTINCT user_id' + orphaned)}
    for user in sorted(set(deleted) | older | left):
        at = erased(db, user)
        if at is None:
            continue
        stamped = db.execute('SELECT at FROM personal_erasure_times WHERE user_id=?', (user,)).fetchone()
        record_erasure(db, user, max(at, stamped[0] if stamped else at), None, deleted.get(user),
                       removals_on_discord(db, user))  # And the removals' Discord times it left (fri #368).
        db.execute('DELETE' + orphaned + ' AND user_id=?', (user,))  # In the erasure now.
    db.execute('DELETE FROM personal_cutoff_floors')
    if began:
        db.commit()


# A note's sources as recorded with its current time: anything else (none, or a time moved since) is unknown.
CURRENT = 'sources IS NOT NULL AND sources_at IS at'


def digest(category, evidence):
    return hashlib.sha256((category+'\0'+evidence).encode()).hexdigest()


def limit(scope):
    return PROFILE_NOTES if scope == SERVER else MAX_NOTES


def removed(db, guild, scope, user):
    """When the member last removed a note this scope could see; nothing they said before then is learned here.

    A learner could restate removed words shorter or under another category, so a removal covers the member's
    older words, not one phrasing. A profile removal covers every conversation. A fresh disclosure still counts.
    """
    return db.execute('SELECT max(at) FROM personal_note_removals WHERE guild_id=? AND channel_id IN (?,?) '
                      'AND user_id=?', (guild, scope, SERVER, user)).fetchone()[0]


def erased(db, user):
    """When the member last turned memory off, or None (HumanMint/fri#321): what they said before it is learned
    nowhere (see last_removal and AgentStore.finish)."""
    row = db.execute('SELECT at FROM personal_erasures WHERE user_id=?', (user,)).fetchone()
    return row[0] if row else None


# How far before Gene's own stamp a Discord time may put the cutoff: further would be a stale time (a control that
# waited out an outage), and holding what was said after it back is the safe way.
SKEW = 60
# Red's own data deletion, with no message of the member's to date it by, is held a few seconds past Gene's stamp on
# Discord's clock, so a message sent just before it, with Gene's clock behind Discord's, still counts as before it
# (HumanMint/fri#368, see api's /privacy/erase). Holding a few seconds' more back is the safe way.
MARGIN = 5


def record_erasure(db, user, at, discord_at=None, removals=None, removals_discord=None, margin=0):
    """Gene's stamp of the member's erasure, and its cutoff on Discord's clock (see erased_on_discord): the Discord time
    of the message or interaction that asked, within SKEW of Gene's stamp, or Gene's stamp without one. Neither is ever
    earlier than a cutoff already in force: an earlier erasure's (Sol round 1 of #213), or `removals`, the latest of
    the member's note removals, which the erasure deletes (Sol round 2), and `removals_discord`, the same on Discord's
    clock (HumanMint/fri#368). A request that arrives late, after a newer erasure or removal, or after an outage,
    can't move a cutoff back and admit what was said before it; nor can Gene's clock stepping back. `margin`: with no
    Discord time, how far past Gene's stamp the cutoff is (Red's own deletion: MARGIN, HumanMint/fri#368)."""
    in_force = max(erased_on_discord(db, user) or 0, removals or 0, removals_discord or 0)
    at = max(at, erased(db, user) or 0, removals or 0)
    cutoff = at + margin if discord_at is None else min(max(discord_at, at - SKEW), at + SKEW)
    db.execute('INSERT INTO personal_erasures VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET at=excluded.at',
                (user, at))
    db.execute('INSERT INTO personal_erasure_times VALUES (?,?,?) ON CONFLICT(user_id) DO UPDATE SET '
               'at=excluded.at,discord_at=excluded.discord_at', (user, at, max(cutoff, in_force)))


def erased_on_discord(db, user):
    """When the member last turned memory off, on Discord's clock (HumanMint/fri#337): what's compared with a message's
    snowflake. The cutoff recorded with this same erasure (see record_erasure); otherwise Gene's stamp, as before (see
    erased), never earlier than one recorded with an earlier erasure: an older controller's, after a rollback, writes
    its stamp alone. None when never."""
    at = erased(db, user)
    if at is None:
        return None
    row = db.execute('SELECT at,discord_at FROM personal_erasure_times WHERE user_id=?', (user,)).fetchone()
    if not row:
        return at
    return row[1] if row[0] == at else max(at, row[1])


def removal_on_discord(db, guild, channel, user):
    """The member's latest note removal in this conversation (or the profile, SERVER) alone, on Discord's clock
    (HumanMint/fri#368): the time recorded with these same removals (see record_removal); otherwise their Gene stamp,
    as before, never earlier than one recorded with an earlier removal (a summary edit's, or an older controller's,
    writes Gene's clock alone). And a recorded one whose removals went (an older controller's erasure) still holds. None
    when none."""
    fence = db.execute('SELECT max(at) FROM personal_note_removals WHERE guild_id=? AND channel_id=? AND user_id=?',
                       (guild, channel, user)).fetchone()[0]
    row = db.execute('SELECT at,discord_at FROM personal_removal_times WHERE guild_id=? AND channel_id=? AND user_id=?',
                     (guild, channel, user)).fetchone()
    if row and fence == row[0]:
        return row[1]
    return max((at for at in (fence, row[1] if row else None) if at is not None), default=None)


def removed_on_discord(db, guild, scope, user):
    """removed (see there), on Discord's clock: the later of this scope's and the profile's (see removal_on_discord)."""
    return max((at for at in (removal_on_discord(db, guild, scope, user), removal_on_discord(db, guild, SERVER, user))
                if at is not None), default=None)


def removals_on_discord(db, user, guild=None):
    """The member's latest note removal anywhere (in this guild, if given), on Discord's clock. None when none."""
    where, args = ('user_id=?', (user,)) if guild is None else ('guild_id=? AND user_id=?', (guild, user))
    places = db.execute(f'SELECT guild_id,channel_id FROM personal_note_removals WHERE {where} UNION '
                        f'SELECT guild_id,channel_id FROM personal_removal_times WHERE {where}', args + args).fetchall()
    return max((at for place in places if (at := removal_on_discord(db, place[0], place[1], user)) is not None),
               default=None)


def record_removal(db, guild, scope, user, stamp, discord_at, before):
    """After a removal's fences are written at Gene's `stamp`: its time on Discord's clock, the panel press's
    (`discord_at`), within SKEW of the stamp as an erasure's is (see record_erasure), and never earlier than `before`,
    this scope's cutoff on Discord's clock when it began (see removal_on_discord), whatever order presses arrive in or
    however Gene's clock steps (HumanMint/fri#368)."""
    fence = db.execute('SELECT max(at) FROM personal_note_removals WHERE guild_id=? AND channel_id=? AND user_id=?',
                       (guild, scope, user)).fetchone()[0]
    cutoff = max(min(max(discord_at, stamp - SKEW), stamp + SKEW), before or 0)
    db.execute('INSERT INTO personal_removal_times VALUES (?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id) DO '
               'UPDATE SET at=excluded.at,discord_at=excluded.discord_at', (guild, scope, user, fence, cutoff))


def learned_cutoff(db, guild, scope, user):
    """What learning from history holds a member's words to: their latest removal seen here, or when they last turned
    memory off, whichever is later, both on Discord's clock, as a message's time is (HumanMint/fri#368). None when
    neither."""
    return max((at for at in (removed_on_discord(db, guild, scope, user), erased_on_discord(db, user))
                if at is not None), default=None)


def predates(db, guild, scope, user, source):
    """A message sent before the member's latest removal seen here, answered after it: it keeps nothing. On Discord's
    clock, as the message's snowflake is (HumanMint/fri#368)."""
    cutoff = removed_on_discord(db, guild, scope, user)
    return bool(source and cutoff is not None and source.timestamp().timestamp() <= cutoff)


def before_erasure(db, user, source):
    """A message sent before the member last turned memory off, answered after it (a queued one, once it's on again):
    a turn saved from it keeps nothing, as one from before a removal doesn't (HumanMint/fri#331). Only a turn's own
    message is held to it; the other cutoffs (see removed) are unchanged. On Discord's clock, as the message's
    snowflake is (HumanMint/fri#337)."""
    at = erased_on_discord(db, user)
    return bool(source and at is not None and source.timestamp().timestamp() <= at)


def last_removal(db, guild, user, on_discord=False):
    """When the member last edited or removed any note, or turned memory off; Fri's own caches drop what they said
    before it, and so does learning (HumanMint/fri#321). `on_discord`: with the erasure on Discord's clock (see
    erased_on_discord), as a newer Fri compares it (HumanMint/fri#337), and each removal too (see removal_on_discord,
    HumanMint/fri#368)."""
    removals = (removals_on_discord(db, user, guild) if on_discord else
                db.execute('SELECT max(at) FROM personal_note_removals WHERE guild_id=? AND user_id=?',
                           (guild, user)).fetchone()[0])
    cutoffs = [removals, (erased_on_discord if on_discord else erased)(db, user)]
    return max((at for at in cutoffs if at is not None), default=0)


def fences():
    """Every category, with no words in it: also what an older controller checks for whatever it would save."""
    return [digest(category, '') for category in CATEGORIES]


def save(db, guild, scope, user, note, at, origin=None, provenance=None, sources=None, *, fenced=True):
    """provenance: other members whose words the saving turn drew on; their privacy changes remove the note.

    None when the turn didn't say, so it's unknown: only a conversation note can be saved that way.
    sources: the Discord messages it was learned from, which take it when withdrawn (gene #141); None, unknown. The
    same note learned again comes from the newer message: the earlier one, withdrawn, no longer takes it.
    fenced: whether a removal fence refuses it (see removed): what's learned from what the member said before a
    removal is. Their own edit in My memory isn't (Sol round 3 of #213): a fence holds back what was learned from
    before it, never what they type or move now, and one kept at a later time, from Gene's clock running ahead, would.
    """
    if scope == SERVER and provenance is None:
        raise ValueError('A profile note needs to say whose words it drew on')
    category, evidence = note
    cutoff = removed(db, guild, scope, user) if fenced else None
    if at < time.time()-TTL or (cutoff is not None and at <= cutoff):
        return False
    # The same words in other capitals ("I play valorant", "i play valorant"), said where the same note was and
    # drawing on the same people, are one note: the newer wording renews it instead of taking another slot.
    # Only literal repeats; a shorter or longer note is another note, and an older wording keeps nothing.
    rows = db.execute('SELECT id,evidence,at,origin,provenance FROM personal_notes WHERE guild_id=? AND '
                      'channel_id=? AND user_id=? AND category=?', (guild, scope, user, category)).fetchall()
    same = [row for row in rows if row['evidence'] != evidence and folded(row['evidence']) == folded(evidence)
            and row['origin'] == origin and whose(row['provenance']) == whose(provenance)]
    if any(row['at'] >= at for row in same):
        return False
    if not any(row['evidence'] == evidence and row['at'] >= at for row in rows):  # Else the upsert keeps that one.
        for row in same:
            db.execute('DELETE FROM personal_notes WHERE id=?', (row['id'],))
    updated = db.execute('INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at,origin,provenance,'
        'sources,sources_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id,category,evidence) '
        'DO UPDATE SET at=excluded.at,origin=excluded.origin,provenance=excluded.provenance,sources=excluded.sources,'
        'sources_at=excluded.sources_at WHERE excluded.at>personal_notes.at',
        (guild, scope, user, category, evidence, at, origin, None if provenance is None else json.dumps(list(provenance)),
         None if sources is None else json.dumps(sorted({str(item) for item in sources})),
         None if sources is None else at))
    db.execute('DELETE FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? AND category=? '
        'AND id NOT IN (SELECT id FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? '
        'AND category=? ORDER BY at DESC,id DESC LIMIT ?)',
        (guild, scope, user, category, guild, scope, user, category, PER_CATEGORY))
    db.execute('DELETE FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? '
        'AND id NOT IN (SELECT id FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? '
        'ORDER BY at DESC,id DESC LIMIT ?)', (guild, scope, user, guild, scope, user, limit(scope)))
    return bool(updated.rowcount and db.execute('SELECT 1 FROM personal_notes WHERE guild_id=? AND '
        'channel_id=? AND user_id=? AND category=? AND evidence=?',
        (guild, scope, user, category, evidence)).fetchone())


def learned(row):
    """The messages a saved note was learned from, which a move keeps: the same words, from the same message. None if
    unknown, stale ones included (see CURRENT)."""
    if row['sources'] is None or row['sources_at'] is None or row['sources_at'] != row['at']:
        return None
    return json.loads(row['sources'])


def folded(evidence):
    return evidence.replace('’', "'").casefold()


def whose(provenance):
    """The members a note drew on, in any order; None (unknown) stays apart from [] (only the member's own)."""
    if isinstance(provenance, str):
        provenance = json.loads(provenance)
    return None if provenance is None else sorted(json.dumps(item, sort_keys=True) for item in provenance)


def visible(conversation):
    """Scopes a conversation may read: the profile and its own notes."""
    return (SERVER, conversation)


def manageable(conversation, scope, profiles=True):
    """Only the member can see a private conversation, so it lists and edits all of their notes.

    An older Fri (profiles=False) manages only this conversation's notes, as before profiles existed.
    """
    if not profiles:
        return scope == conversation
    return conversation.startswith('private:') or scope in visible(conversation)


def entries(store, guild, conversation, user, profiles=True, summaries=False, sources=False):
    """summaries: a Fri that shows the member's summary, which only a private conversation lists. sources: each note
    also names the messages it was learned from (None if unknown), for a Fri that holds back one whose withdrawal is
    still on its way (fri #215)."""
    with store.connection() as db:
        profile = store._personal_context(db, guild, conversation, user, profiles)
        row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        notes = []
        for scope in [r[0] for r in db.execute('SELECT DISTINCT channel_id FROM personal_notes WHERE guild_id=? '
                                                'AND user_id=? ORDER BY channel_id', (guild, user))]:
            if manageable(conversation, scope, profiles):
                notes += [{**{key: r[key] for key in ('id', 'category', 'evidence', 'at', 'origin')}, 'scope': scope,
                           **({'sources': learned(r)} if sources else {})} for r in db.execute(
                    'SELECT id,category,evidence,at,origin,sources,sources_at FROM personal_notes WHERE guild_id=? AND channel_id=? '
                    'AND user_id=? ORDER BY at DESC,id DESC LIMIT ?', (guild, scope, user, limit(scope)))]
        # The profile first, then this conversation, then anywhere else; newest first within each.
        order = {SERVER: 0, conversation: 1}
        notes.sort(key=lambda r: (order.get(r['scope'], 2), r['scope'], -r['at'], -r['id']))
        listed = {'enabled': profile['enabled'], 'revision': row[0] if row else 0,
                  'entries': [{**row, 'expires_at': row['at']+TTL} for row in notes]}
        if summaries:
            from .fri_member_summary import read, view
            shown = view(read(db, guild, user)) if conversation.startswith('private:') else None
            listed['summary'] = shown and {key: shown[key] for key in ('text', 'refreshed', 'expires_at')}
        return listed


def edit(store, guild, conversation, user, identifier, revision, source, action, evidence='', *, profiles=True):
    source = ChatSource.model_validate(source).check(guild, conversation, user)
    if not -5 <= time.time()-source.timestamp().timestamp() <= 300:
        raise ValueError('Use a fresh memory control')
    if action not in ('remove', 'replace', 'share', 'keep') or (action != 'replace' and evidence):
        raise ValueError('Choose Remove or Replace for one saved preference')
    if action in ('share', 'keep') and not (profiles and conversation.startswith('private:')):
        raise ValueError('Move a preference from My memory in your DMs')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        if revision != (current[0] if current else 0):
            raise ValueError('Memory changed. Open My memory again.')
        # A source Gene has as another member's message isn't this member's press, whoever it says it's from: the
        # author check above reads only what the request claims (HumanMint/gene#223). A fresh press is an interaction
        # Gene has never recorded, and their own message is recorded as theirs, so neither is refused.
        if store.someone_elses_in(db, source.id, user):
            raise ValueError('Discord source does not match the conversation author or channel')
        store._personal_context(db, guild, conversation, user)
        row = db.execute('SELECT * FROM personal_notes WHERE id=? AND guild_id=? AND user_id=?',
                         (identifier, guild, user)).fetchone()
        if not row or not manageable(conversation, row['channel_id'], profiles):
            raise ValueError('That saved preference is no longer available here')
        scope = row['channel_id']
        if action == 'share':
            return share(store, db, guild, conversation, user, row, profiles)
        if action == 'keep':
            if scope != SERVER:
                raise ValueError('That preference already stays where you said it')
            moved(db, guild, conversation, user, row, 'Your list here')
            from .fri_source_versions import record_note, versions_of
            kept = versions_of(db, 'note', row['id'])  # The same message, read as it was (HumanMint/fri#358).
        note = personal_note({'category': row['category'], 'evidence': evidence}, evidence) if action == 'replace' else None
        if action == 'replace' and not note:
            raise ValueError('Say what you’re doing, such as I’m working on a mod.'
                             if row['category'] == 'projects' else
                             'Use a short first-person preference, such as I like chess.')
        before, stamp = removal_on_discord(db, guild, scope, user), time.time()
        for fence in fences():  # See removed(). Never earlier than it was, even with Gene's clock stepped back.
            db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) ON CONFLICT(guild_id,channel_id,'
                'user_id,digest) DO UPDATE SET at=max(at,excluded.at)', (guild, scope, user, fence, stamp))
        # And when the press was sent, on Discord's clock (HumanMint/fri#368).
        record_removal(db, guild, scope, user, stamp, source.timestamp().timestamp(), before)
        db.execute('DELETE FROM personal_notes WHERE id=?', (identifier,))
        # Other excerpts of the same words go with it, under any category: one containing the other, as two
        # excerpts of one message do. For a profile note, in every conversation, which it reached.
        words = row['evidence'].casefold()
        others = db.execute('SELECT id,evidence,channel_id FROM personal_notes WHERE guild_id=? AND user_id=?',
                            (guild, user)).fetchall()
        for other in others:
            if scope not in (SERVER, other['channel_id']):
                continue
            if words in other['evidence'].casefold() or other['evidence'].casefold() in words:
                db.execute('DELETE FROM personal_notes WHERE id=?', (other['id'],))
        # An edit keeps the note where it shows, as the member's list labels it, wherever it is made. In their own
        # words now, so no removal fence refuses it; any other refusal undoes the edit, the note it replaces included,
        # never a success with neither kept (Sol round 3 of #213, B3).
        if note and not save(db, guild, scope, user, note, time.time(), row['origin'], (), (), fenced=False):
            raise ValueError('That preference can’t be replaced now. Open My memory again.')
        if action == 'keep':  # Out of the profile the way a removal goes, then saved only in this private chat.
            if not save(db, guild, conversation, user, (row['category'], row['evidence']), time.time(), None,
                        None if row['provenance'] is None else json.loads(row['provenance']), learned(row),
                        fenced=False):
                raise ValueError('That preference can’t be moved now. Open My memory again.')
            record_note(db, guild, conversation, user, (row['category'], row['evidence']), kept, learned(row) or ())
        cleared(store, db, guild, user, scope, revision)
    return entries(store, guild, conversation, user, profiles)


def cleared(store, db, guild, user, scope, revision):
    """After an edit to what Fri keeps in scope: its stale copies go and every in-flight derivation is invalidated.

    Conversational copies, research receipts and requests included. A profile note reached every
    conversation in the server, so all of them go.
    """
    if scope == SERVER:
        db.execute('DELETE FROM chat_turns WHERE guild_id=? AND user_id=?', (guild, user))
        db.execute('DELETE FROM chat_pending WHERE guild_id=? AND user_id=?', (guild, user))
        db.execute('DELETE FROM web_research WHERE guild_id=? AND user_id=?', (guild, user))
        db.execute('DELETE FROM web_requests WHERE guild_id=? AND user_id=?', (guild, user))
    else:
        db.execute('DELETE FROM chat_turns WHERE guild_id=? AND channel_id=? AND user_id=?', (guild, scope, user))
        db.execute('DELETE FROM chat_pending WHERE guild_id=? AND conversation_id=? AND user_id=?', (guild, scope, user))
        db.execute('DELETE FROM web_research WHERE guild_id=? AND channel_id=? AND user_id=?', (guild, scope, user))
        db.execute('DELETE FROM web_requests WHERE guild_id=? AND channel_id=? AND user_id=?', (guild, scope, user))
    for summary in db.execute('SELECT channel_id,authors FROM agent_summaries').fetchall():
        if user in json.loads(summary['authors']):
            db.execute('DELETE FROM agent_summaries WHERE channel_id=?', (summary['channel_id'],))
    db.execute('DELETE FROM agent_reports')
    from .fri_explore_store import erase
    erase(db, user)
    # A preference edit invalidates derivations, not an already explicit
    # reminder confirmation. Global erasure follows its separate erase path.
    from .fri_social_store import Social
    Social.preserve_confirmed(db, user, revision)
    store.advance_revision(db, user)


def moved(db, guild, scope, user, row, place):
    """A move never pushes another note out: the member makes room first."""
    # Only the same note already there (category and exact words) takes no new slot. A repeat in other capitals
    # may still fold in save(), but only with the same origin and provenance, so it's counted here.
    full = db.execute('SELECT count(*), sum(category=?) FROM personal_notes WHERE guild_id=? AND channel_id=? '
                      'AND user_id=? AND NOT (category=? AND evidence=?)',
                      (row['category'], guild, scope, user, row['category'], row['evidence'])).fetchone()
    if full[0] >= limit(scope) or (full[1] or 0) >= PER_CATEGORY:
        raise ValueError(f"{place} is full for {row['category']}. Remove one there first.")


def share(store, db, guild, conversation, user, row, profiles):
    """The member's own press moves a note they said somewhere narrower into their profile.

    Only their own words: a note that drew on someone else, or saved before notes said whose words they drew on,
    stays where it was said. Nothing narrows, so nothing derived is cleared and the revision stays.
    """
    if row['channel_id'] == SERVER:
        raise ValueError('That preference already shows across the server')
    if row['provenance'] is None or json.loads(row['provenance']):
        raise ValueError('That preference may repeat someone else’s words, so it stays where you said it')
    moved(db, guild, SERVER, user, row, 'Your server-wide list')
    from .fri_source_versions import record_note, versions_of
    kept = versions_of(db, 'note', row['id'])  # The same message, read as it was (HumanMint/fri#358).
    db.execute('DELETE FROM personal_notes WHERE id=?', (row['id'],))
    # Its origin is the private chat where they pressed Share: their consent, so a Fri that drops profile notes
    # said where not everyone can read (fri #98) still shows this one wherever the profile does.
    if not save(db, guild, SERVER, user, (row['category'], row['evidence']), time.time(), conversation, (), learned(row)):
        raise ValueError('That preference can’t be shared now. Open My memory again.')
    record_note(db, guild, SERVER, user, (row['category'], row['evidence']), kept, learned(row) or ())
    db.commit()
    return entries(store, guild, conversation, user, profiles)
