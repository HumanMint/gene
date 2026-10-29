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
    db.execute('CREATE TABLE IF NOT EXISTS personal_note_removals (guild_id TEXT,channel_id TEXT,'
               'user_id TEXT,digest TEXT,at REAL,PRIMARY KEY(guild_id,channel_id,user_id,digest))')


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


def save(db, guild, scope, user, note, at, origin=None, provenance=None, sources=None):
    """provenance: other members whose words the saving turn drew on; their privacy changes remove the note.

    None when the turn didn't say, so it's unknown: only a conversation note can be saved that way.
    sources: the Discord messages it was learned from, which take it when withdrawn (gene #141); None, unknown. The
    same note learned again comes from the newer message: the earlier one, withdrawn, no longer takes it.
    """
    if scope == SERVER and provenance is None:
        raise ValueError('A profile note needs to say whose words it drew on')
    category, evidence = note
    cutoff = removed(db, guild, scope, user)
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
            save(db, guild, scope, user, note, time.time(), row['origin'], (), ())  # In their own words now.
        if action == 'keep':  # Out of the profile the way a removal goes, then saved only in this private chat.
            if not save(db, guild, conversation, user, (row['category'], row['evidence']), time.time(), None,
                        None if row['provenance'] is None else json.loads(row['provenance']), learned(row)):
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
    db.execute('DELETE FROM personal_notes WHERE id=?', (row['id'],))
    # Its origin is the private chat where they pressed Share: their consent, so a Fri that drops profile notes
    # said where not everyone can read (fri #98) still shows this one wherever the profile does.
    if not save(db, guild, SERVER, user, (row['category'], row['evidence']), time.time(), conversation, (), learned(row)):
        raise ValueError('That preference can’t be shared now. Open My memory again.')
    db.commit()
    return entries(store, guild, conversation, user, profiles)
