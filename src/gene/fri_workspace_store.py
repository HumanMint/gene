"""Durable scoped work and an owner-authorized, at-most-once Discord outbox.

The model may prepare work. Only a bound Discord interaction authorizes delivery.
Uncertain sends are never retried: Discord's nonce deduplication is only short-lived.
"""

import json
import time
import uuid

from .privacy import redact


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS fri_work (
            id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
            scope TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            version INTEGER NOT NULL, state TEXT NOT NULL, revision INTEGER NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL, expires REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fri_work_events (
            source_id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES fri_work(id) ON DELETE CASCADE,
            payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fri_posts (
            id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES fri_work(id) ON DELETE CASCADE,
            version INTEGER NOT NULL, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
            dm_id TEXT NOT NULL, target_id TEXT NOT NULL, body TEXT NOT NULL,
            revision INTEGER NOT NULL, state TEXT NOT NULL, preview_id TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
            lease TEXT NOT NULL DEFAULT '', message_id TEXT NOT NULL DEFAULT '',
            detail TEXT NOT NULL DEFAULT ''
        );
    ''')


def erase(db, user):
    # Cascades remove drafts, event idempotency keys, grants and retained message text.
    db.execute('DELETE FROM fri_work WHERE user_id=?', (user,))


class Workspace:
    def __init__(self, store):
        self.store = store

    @staticmethod
    def revision(db, user, revision):
        row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user,)).fetchone()
        enabled = db.execute('SELECT enabled FROM personal_preferences WHERE user_id=?', (user,)).fetchone()
        if (row[0] if row else 0) != revision or (enabled and not enabled[0]):
            raise ValueError('Memory changed or is disabled. Request fresh work after enabling it.')

    @staticmethod
    def prune(db):
        now = time.time()
        db.execute('DELETE FROM fri_work WHERE expires<?', (now,))
        db.execute("UPDATE fri_posts SET state=CASE WHEN state='sending' THEN 'uncertain' ELSE 'expired' END "
                   "WHERE expires<? AND state IN ('pending','authorized','claimed','sending')", (now,))

    @staticmethod
    def get(db, work_id, scope):
        row = db.execute('SELECT * FROM fri_work WHERE id=? AND guild_id=? AND user_id=? AND scope=?',
                         (work_id, scope['guild_id'], scope['user_id'], scope['conversation_id'])).fetchone()
        if not row:
            raise KeyError('Work unavailable in this conversation')
        return dict(row)

    def read(self, scope, work_id=''):
        with self.store.connection() as db:
            self.prune(db)
            if work_id:
                work = self.get(db, work_id, scope)
                work['deliveries'] = [dict(r) for r in db.execute(
                    'SELECT id,target_id,version,state,message_id FROM fri_posts WHERE work_id=? ORDER BY created DESC LIMIT 5',
                    (work_id,))]
                return work
            return {'items': [dict(row) for row in db.execute(
                'SELECT id,title,version,state,updated,expires FROM fri_work WHERE guild_id=? AND user_id=? '
                'AND scope=? ORDER BY updated DESC LIMIT 30',
                (scope['guild_id'], scope['user_id'], scope['conversation_id']))]}

    def save(self, scope, source, revision, work_id, version, title, body, state, request_text=''):
        from .store import Conflict
        source.check(scope['guild_id'], scope['conversation_id'], scope['user_id'])
        if source.kind != 'discord_message' or abs(time.time()-source.timestamp().timestamp()) > 900:
            raise ValueError('Saving work needs a fresh Discord message')
        if not title.strip() or not body.strip() or redact(body) != body or redact(title) != title:
            raise ValueError('Work must contain text without credentials')
        payload = json.dumps([scope, revision, work_id, version, title, body, state, request_text], sort_keys=True)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            self.revision(db, scope['user_id'], revision)
            if scope['conversation_id'].startswith('public:'):
                old = self.get(db, work_id, scope) if work_id else None
                if body not in request_text and (not old or body != old['body']):
                    raise ValueError('In a public channel, provide the exact text to save in your own message. Other members’ context cannot be copied.')
                # Titles also must not launder someone else's ambient context into durable work.
                title = body.strip()[:100]
            event = db.execute('SELECT * FROM fri_work_events WHERE source_id=?', (source.id,)).fetchone()
            if event:
                if event['payload'] != payload:
                    raise Conflict('This message already changed saved work. Send a new message to edit it.')
                return self.get(db, event['work_id'], scope)
            now = time.time()
            if work_id:
                old = self.get(db, work_id, scope)
                if old['version'] != version:
                    raise Conflict('Saved work changed. Read the latest version first.')
                if db.execute("SELECT 1 FROM fri_posts WHERE work_id=? AND state='sending'", (work_id,)).fetchone():
                    raise Conflict('Delivery is in progress. Wait for its receipt before editing.')
                db.execute('UPDATE fri_work SET title=?,body=?,version=version+1,state=?,updated=?,expires=? WHERE id=?',
                           (title, body, state, now, now+30*86400, work_id))
                db.execute("UPDATE fri_posts SET state='cancelled',body='' WHERE work_id=? "
                           "AND state IN ('pending','authorized','claimed')", (work_id,))
            else:
                if version or db.execute('SELECT COUNT(*) FROM fri_work WHERE user_id=?', (scope['user_id'],)).fetchone()[0] >= 30:
                    raise ValueError('Saved work limit reached or invalid initial version')
                work_id = uuid.uuid4().hex
                db.execute('INSERT INTO fri_work VALUES (?,?,?,?,?,?,1,?,?,?,?,?)',
                    (work_id, scope['guild_id'], scope['user_id'], scope['conversation_id'], title, body,
                     state, revision, now, now, now+30*86400))
            db.execute('INSERT INTO fri_work_events VALUES (?,?,?)', (source.id, work_id, payload))
            return self.get(db, work_id, scope)

    def prepare(self, scope, source, revision, work_id, version, target_id):
        source.check(scope['guild_id'], scope['conversation_id'], scope['user_id'])
        if source.guild_id is not None or source.kind != 'discord_message' or abs(time.time()-source.timestamp().timestamp()) > 900:
            raise ValueError('Prepare a one-off post from a fresh owner DM')
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            self.revision(db, scope['user_id'], revision)
            work = self.get(db, work_id, scope)
            if work['version'] != version or work['state'] != 'open':
                raise ValueError('Choose the current open draft')
            if len(work['body'].encode('utf-16-le'))//2 > 1900:
                raise ValueError('Shorten the draft to 1900 Discord characters before posting')
            existing = db.execute('SELECT * FROM fri_posts WHERE source=?', (source.model_dump_json(),)).fetchone()
            if existing:
                if existing['work_id'] != work_id or existing['version'] != version or existing['target_id'] != target_id:
                    raise ValueError('This message already prepared a different post')
                return dict(existing)
            post_id, now = uuid.uuid4().hex, time.time()
            db.execute('INSERT INTO fri_posts(id,work_id,version,guild_id,user_id,dm_id,target_id,body,revision,'
                'state,source,created,expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (post_id, work_id, version, scope['guild_id'], scope['user_id'], source.channel_id, target_id,
                 work['body'], revision, 'pending', source.model_dump_json(), now, now+900))
            return dict(db.execute('SELECT * FROM fri_posts WHERE id=?', (post_id,)).fetchone())

    def post(self, post_id):
        with self.store.connection() as db:
            self.prune(db)
            row = db.execute('SELECT * FROM fri_posts WHERE id=?', (post_id,)).fetchone()
            if not row:
                raise KeyError('Post expired or was erased')
            return dict(row)

    def bind(self, post_id, message_id):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM fri_posts WHERE id=?', (post_id,)).fetchone()
            if not row or row['state'] != 'pending' or (row['preview_id'] and row['preview_id'] != message_id):
                raise ValueError('Preview already bound or inactive')
            db.execute('UPDATE fri_posts SET preview_id=? WHERE id=?', (message_id, post_id))

    def decide(self, post_id, source, preview_id, approve):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            row = db.execute('SELECT * FROM fri_posts WHERE id=?', (post_id,)).fetchone()
            if not row:
                raise KeyError('Post expired or was erased')
            if (source.kind != 'discord_interaction' or source.guild_id is not None or
                    source.user_id != row['user_id'] or source.channel_id != row['dm_id'] or
                    not row['preview_id'] or preview_id != row['preview_id'] or
                    abs(time.time()-source.timestamp().timestamp()) > 120):
                raise ValueError('Approval requires the original owner DM preview and a fresh button click')
            self.revision(db, row['user_id'], row['revision'])
            if row['state'] != 'pending':
                return {'state': row['state']}
            db.execute('UPDATE fri_posts SET state=?,expires=? WHERE id=?',
                       ('authorized' if approve else 'cancelled', time.time()+300, post_id))
            self.store._audit(db, source.user_id, 'one_off_post_authorized' if approve else 'one_off_post_cancelled', post_id)
            return {'state': 'authorized' if approve else 'cancelled'}

    def claim(self):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            row = db.execute("SELECT * FROM fri_posts WHERE state='authorized' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            try:
                self.revision(db, row['user_id'], row['revision'])
            except ValueError:
                db.execute("UPDATE fri_posts SET state='cancelled' WHERE id=?", (row['id'],))
                return None
            lease = uuid.uuid4().hex
            db.execute("UPDATE fri_posts SET state='claimed',lease=? WHERE id=?", (lease, row['id']))
            return {**dict(row), 'state': 'claimed', 'lease': lease}

    def sending(self, post_id, lease):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.prune(db)
            row = db.execute('SELECT * FROM fri_posts WHERE id=?', (post_id,)).fetchone()
            if not row or row['state'] != 'claimed' or row['lease'] != lease:
                raise ValueError('Delivery grant unavailable; do not send')
            self.revision(db, row['user_id'], row['revision'])
            db.execute("UPDATE fri_posts SET state='sending' WHERE id=?", (post_id,))
            return dict(row)

    def finish(self, post_id, lease, state, message_id=''):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM fri_posts WHERE id=?', (post_id,)).fetchone()
            if not row or row['lease'] != lease:
                raise ValueError('Delivery receipt unavailable')
            if row['state'] not in ('claimed', 'sending', 'uncertain', state):
                raise ValueError('Delivery already closed')
            if state == 'sent' and (not message_id or (row['message_id'] and row['message_id'] != message_id)):
                raise ValueError('A Discord message receipt is required')
            db.execute('UPDATE fri_posts SET state=?,message_id=? WHERE id=?', (state, message_id, post_id))
            self.store._audit(db, 'fri', 'one_off_post_'+state, post_id, message_id)
            return {'state': state, 'message_id': message_id}
