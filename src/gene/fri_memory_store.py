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
    db.execute('CREATE TABLE IF NOT EXISTS personal_note_removals (guild_id TEXT,channel_id TEXT,'
               'user_id TEXT,digest TEXT,at REAL,PRIMARY KEY(guild_id,channel_id,user_id,digest))')


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


def predates(db, guild, scope, user, source):
    """A message sent before the member's latest removal seen here, answered after it: it keeps nothing."""
    cutoff = removed(db, guild, scope, user)
    return bool(source and cutoff is not None and source.timestamp().timestamp() <= cutoff)


def last_removal(db, guild, user):
    """When the member last edited or removed any note; Fri's own caches drop what they said before it."""
    return db.execute('SELECT max(at) FROM personal_note_removals WHERE guild_id=? AND user_id=?',
                      (guild, user)).fetchone()[0] or 0


def fences():
    """Every category, with no words in it: also what an older controller checks for whatever it would save."""
    return [digest(category, '') for category in CATEGORIES]


def save(db, guild, scope, user, note, at, origin=None, provenance=None):
    """provenance: other members whose words the saving turn drew on; their privacy changes remove the note.

    None when the turn didn't say, so it's unknown: only a conversation note can be saved that way.
    """
    if scope == SERVER and provenance is None:
        raise ValueError('A profile note needs to say whose words it drew on')
    category, evidence = note
    cutoff = removed(db, guild, scope, user)
    if at < time.time()-TTL or (cutoff is not None and at <= cutoff):
        return False
    updated = db.execute('INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at,origin,provenance) '
        'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id,category,evidence) '
        'DO UPDATE SET at=excluded.at,origin=excluded.origin,provenance=excluded.provenance '
        'WHERE excluded.at>personal_notes.at',
        (guild, scope, user, category, evidence, at, origin, None if provenance is None else json.dumps(list(provenance))))
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


def entries(store, guild, conversation, user, profiles=True, summaries=False):
    """summaries: a Fri that shows the member's summary, which only a private conversation lists."""
    with store.connection() as db:
        profile = store._personal_context(db, guild, conversation, user, profiles)
        row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        notes = []
        for scope in [r[0] for r in db.execute('SELECT DISTINCT channel_id FROM personal_notes WHERE guild_id=? '
                                                'AND user_id=? ORDER BY channel_id', (guild, user))]:
            if manageable(conversation, scope, profiles):
                notes += [{**dict(r), 'scope': scope} for r in db.execute(
                    'SELECT id,category,evidence,at,origin FROM personal_notes WHERE guild_id=? AND channel_id=? '
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
        note = personal_note({'category': row['category'], 'evidence': evidence}, evidence) if action == 'replace' else None
        if action == 'replace' and not note:
            raise ValueError('Say what you’re doing, such as I’m working on a mod.'
                             if row['category'] == 'projects' else
                             'Use a short first-person preference, such as I like chess.')
        for fence in fences():  # See removed().
            db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) ON CONFLICT(guild_id,channel_id,'
                'user_id,digest) DO UPDATE SET at=excluded.at', (guild, scope, user, fence, time.time()))
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
        if note:  # An edit keeps the note where it shows, as the member's list labels it, wherever it is made.
            save(db, guild, scope, user, note, time.time(), row['origin'], ())  # In their own words now.
        if action == 'keep':  # Out of the profile the way a removal goes, then saved only in this private chat.
            if not save(db, guild, conversation, user, (row['category'], row['evidence']), time.time(), None,
                        None if row['provenance'] is None else json.loads(row['provenance'])):
                raise ValueError('That preference can’t be moved now. Open My memory again.')
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
    full = db.execute('SELECT count(*), sum(category=?) FROM personal_notes WHERE guild_id=? AND channel_id=? '
                      'AND user_id=? AND evidence<>?', (row['category'], guild, scope, user, row['evidence'])).fetchone()
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
    db.execute('DELETE FROM personal_notes WHERE id=?', (row['id'],))
    if not save(db, guild, SERVER, user, (row['category'], row['evidence']), time.time(), row['channel_id'], ()):
        raise ValueError('That preference can’t be shared now. Open My memory again.')
    db.commit()
    return entries(store, guild, conversation, user, profiles)
