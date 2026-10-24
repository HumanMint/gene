"""Small exact preferences with item controls and fences against historical re-learning."""

import hashlib
import json
import time

from .fri_context import ChatSource
from .fri_memory import MAX_NOTES, PER_CATEGORY, TTL, personal_note


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
    db.execute('CREATE TABLE IF NOT EXISTS personal_note_removals (guild_id TEXT,channel_id TEXT,'
               'user_id TEXT,digest TEXT,at REAL,PRIMARY KEY(guild_id,channel_id,user_id,digest))')


def digest(category, evidence):
    return hashlib.sha256((category+'\0'+evidence).encode()).hexdigest()


def save(db, guild, conversation, user, note, at):
    category, evidence = note
    removed = db.execute('SELECT max(at) FROM personal_note_removals WHERE guild_id=? AND channel_id=? '
        'AND user_id=? AND digest IN (?,?)',
        (guild, conversation, user, digest(*note), digest(category, ''))).fetchone()[0]
    if at < time.time()-TTL or (removed is not None and at <= removed):
        return False
    updated = db.execute('INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at) '
        'VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id,category,evidence) '
        'DO UPDATE SET at=excluded.at WHERE excluded.at>personal_notes.at',
        (guild, conversation, user, category, evidence, at))
    db.execute('DELETE FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? AND category=? '
        'AND id NOT IN (SELECT id FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? '
        'AND category=? ORDER BY at DESC,id DESC LIMIT ?)',
        (guild, conversation, user, category, guild, conversation, user, category, PER_CATEGORY))
    db.execute('DELETE FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? '
        'AND id NOT IN (SELECT id FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? '
        'ORDER BY at DESC,id DESC LIMIT ?)', (guild, conversation, user, guild, conversation, user, MAX_NOTES))
    return bool(updated.rowcount and db.execute('SELECT 1 FROM personal_notes WHERE guild_id=? AND '
        'channel_id=? AND user_id=? AND category=? AND evidence=?',
        (guild, conversation, user, category, evidence)).fetchone())


def entries(store, guild, conversation, user):
    with store.connection() as db:
        profile = store._personal_context(db, guild, conversation, user)
        row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        notes = [dict(row) for row in db.execute('SELECT id,category,evidence,at FROM personal_notes '
            'WHERE guild_id=? AND channel_id=? AND user_id=? ORDER BY at DESC,id DESC LIMIT ?',
            (guild, conversation, user, MAX_NOTES))]
        return {'enabled': profile['enabled'], 'revision': row[0] if row else 0,
                'entries': [{**row, 'expires_at': row['at']+TTL} for row in notes]}


def edit(store, guild, conversation, user, identifier, revision, source, action, evidence=''):
    source = ChatSource.model_validate(source).check(guild, conversation, user)
    if not -5 <= time.time()-source.timestamp().timestamp() <= 300:
        raise ValueError('Use a fresh memory control')
    if action not in ('remove', 'replace') or (action == 'remove' and evidence):
        raise ValueError('Choose Remove or Replace for one saved preference')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        if revision != (current[0] if current else 0):
            raise ValueError('Memory changed. Open My memory again.')
        store._personal_context(db, guild, conversation, user)
        row = db.execute('SELECT * FROM personal_notes WHERE id=? AND guild_id=? AND channel_id=? AND user_id=?',
                         (identifier, guild, conversation, user)).fetchone()
        if not row:
            raise ValueError('That saved preference is no longer available here')
        note = personal_note({'category': row['category'], 'evidence': evidence}, evidence) if action == 'replace' else None
        if action == 'replace' and not note:
            raise ValueError('Use a short first-person preference, such as I like chess.')
        # Fence older category disclosures too: a learner could choose a shorter
        # exact substring of the removed evidence rather than the same full text.
        db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id,digest) '
            'DO UPDATE SET at=excluded.at', (guild, conversation, user, digest(row['category'], ''), time.time()))
        db.execute('DELETE FROM personal_notes WHERE id=?', (identifier,))
        if note:
            save(db, guild, conversation, user, note, time.time())
        # Clear stale conversational copies and invalidate every in-flight derivation.
        db.execute('DELETE FROM chat_turns WHERE guild_id=? AND channel_id=? AND user_id=?', (guild, conversation, user))
        db.execute('DELETE FROM chat_pending WHERE guild_id=? AND conversation_id=? AND user_id=?', (guild, conversation, user))
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
    return entries(store, guild, conversation, user)
