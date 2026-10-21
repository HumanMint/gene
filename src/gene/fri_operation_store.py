"""Persistent operational allowance, separate from conversational inference budgets."""

import time

from .control_store import Controls, source_key
from .fri_context import ChatSource
from .fri_pending import cancelled, read
from .gene_controls import current_music_context
from .gene_controls import quick_control as command

WINDOW = 1800
LIMIT = 12



def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS fri_operation_admissions ('
               'id TEXT PRIMARY KEY, user_id TEXT NOT NULL, guild_id TEXT NOT NULL, '
               'created REAL NOT NULL, continuation TEXT NOT NULL DEFAULT "")')


def admit(store, scope, source, revision, text, continuation):
    source = ChatSource.model_validate(source).check(scope['guild_id'], scope['conversation_id'], scope['user_id'])
    now = time.time()
    if source.kind != 'discord_message' or not -5 <= now-source.timestamp().timestamp() <= 300:
        raise ValueError('Operational access needs a fresh Discord message')
    identifier = source_key(scope['guild_id'], source.id)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM fri_operation_admissions WHERE created<?', (now-WINDOW,))
        row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (scope['user_id'],)).fetchone()
        if revision != (row[0] if row else 0):
            raise ValueError('Conversation changed before this operation')
        origin = ''
        if continuation:
            pending = read(db, scope['guild_id'], scope['conversation_id'], scope['user_id'], revision)
            if not pending or cancelled(text) or int(source.id) <= int(pending['source']['id']):
                raise ValueError('There is no current accepted request to continue')
            origin = source_key(scope['guild_id'], pending['source']['id'])
        elif not command(text):
            recent = Controls(store).recent(scope['user_id'], scope['guild_id'], scope['conversation_id'])
            if not command(text, music_context=current_music_context(recent)):
                raise ValueError('Name music or playback explicitly, or answer a current clarification')
        old = db.execute('SELECT id FROM fri_operation_admissions WHERE id=?', (identifier,)).fetchone()
        if old:
            return {'allowed': False, 'reason': 'This operational request was already accepted. Check its result before retrying.'}
        if origin and db.execute('SELECT id FROM fri_operation_admissions WHERE continuation=?', (origin,)).fetchone():
            return {'allowed': False, 'reason': 'That clarification already received a continuation. Check its status or send a fresh complete request.'}
        rows = db.execute('SELECT created FROM fri_operation_admissions WHERE user_id=? AND guild_id=? ORDER BY created',
                          (scope['user_id'], scope['guild_id'])).fetchall()
        if len(rows) >= LIMIT:
            return {'allowed': False, 'reason': 'The small Fri control allowance is used up for now. Use Gene’s music panel or try again shortly.'}
        db.execute('INSERT INTO fri_operation_admissions VALUES (?,?,?,?,?)',
                   (identifier, scope['user_id'], scope['guild_id'], now, origin))
        return {'allowed': True}
