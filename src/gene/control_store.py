"""Authenticated, expiring, at-most-once handoff from Fri to the Gene runtime."""

import hashlib
import json
import time
import uuid

from .fri_context import ChatSource
from .gene_controls import COMMANDS, arguments, requested


def initialize(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS gene_controls (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created REAL NOT NULL,
            state TEXT NOT NULL, payload TEXT NOT NULL, lease TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS gene_control_catalog (id INTEGER PRIMARY KEY, at REAL, actions TEXT);
        CREATE TABLE IF NOT EXISTS gene_runtime_health (id INTEGER PRIMARY KEY, at REAL, body TEXT);
        CREATE TABLE IF NOT EXISTS gene_control_origins (id TEXT PRIMARY KEY, created REAL NOT NULL);
    ''')


def source_key(guild, source_id):
    return hashlib.sha256((guild+':'+source_id).encode()).hexdigest()


def source_used(db, guild, source_id):
    """Keep execution dedupe independent of retained user text and privacy settings."""
    key = source_key(guild, source_id)
    return bool(db.execute('SELECT 1 FROM gene_controls WHERE id=? UNION ALL '
                           'SELECT 1 FROM gene_control_origins WHERE id=? LIMIT 1', (key, key)).fetchone())


class Controls:
    def __init__(self, store):
        self.store = store

    def catalog(self):
        with self.store.connection() as db:
            row = db.execute('SELECT at,actions FROM gene_control_catalog WHERE id=1').fetchone()
            return json.loads(row['actions']) if row and row['at'] > time.time()-20 else []

    def heartbeat(self, body):
        if body['started'] > time.time()+5:
            raise ValueError('Runtime start cannot be in the future')
        with self.store.connection() as db:
            db.execute('INSERT OR REPLACE INTO gene_runtime_health VALUES (1,?,?)',
                       (time.time(), json.dumps(body)))

    def health(self):
        """Controller-side evidence, available even when Gene cannot execute commands."""
        now = time.time()
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM gene_runtime_health WHERE id=1').fetchone()
            poll = db.execute('SELECT at FROM gene_control_catalog WHERE id=1').fetchone()
        age = max(0, int(now-row['at'])) if row else None
        poll_age = max(0, int(now-poll['at'])) if poll else None
        report = json.loads(row['body']) if row else {}
        responsive = (age is not None and age < 30 and report.get('guild_connected') is True
                      and report.get('controls_running') is True and poll_age is not None and poll_age < 20)
        return {'checked_at': now, 'heartbeat_age_seconds': age, 'command_poll_age_seconds': poll_age,
                'responsive': responsive, 'runtime': report,
                'limits': 'A fresh heartbeat and command poll do not prove a command completed or audio is audible. '
                          'A stale poll may mean a command is busy. No shell, process logs, or restart access.'}

    def recent(self, user, guild, conversation):
        """Small dated receipts, scoped like the original command and erased with chats."""
        with self.store.connection() as db:
            rows = db.execute('SELECT * FROM gene_controls WHERE user_id=? AND created>? '
                              'ORDER BY created DESC LIMIT 30', (user, time.time()-86400)).fetchall()
        receipts = []
        for row in rows:
            payload = json.loads(row['payload'])
            if payload.get('guild_id') != guild or payload.get('conversation_id') != conversation:
                continue
            receipts.append({'at': row['created'], 'action': payload['action'], 'argument': payload['argument'],
                'state': row['state'], 'channel_id': payload['target_channel_id'],
                'result': self._result(row)})
            if receipts[-1]['result'].get('output'):
                receipts[-1]['result']['output'] = receipts[-1]['result']['output'][:1000]
            if len(receipts) == 3:
                break
        return receipts

    def enqueue(self, body):
        source = ChatSource.model_validate(body['source']).check(body['guild_id'], body['conversation_id'], body['user_id'])
        if source.kind != 'discord_message' or not -5 <= time.time()-source.timestamp().timestamp() <= 300:
            raise ValueError('Gene needs a fresh Discord message request')
        arguments(body['action'], body['argument'])
        identifier = source_key(body['guild_id'], source.id)
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id FROM gene_controls WHERE id=?', (identifier,)).fetchone()
            if old:
                return {'id': identifier}  # Never enqueue the same message twice, even after a timeout.
            if source_used(db, body['guild_id'], source.id):
                raise ValueError('This request already ran')
            row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (body['user_id'],)).fetchone()
            if body['revision'] != (row[0] if row else 0):
                raise ValueError('Conversation changed before the action could start')
            from .fri_pending import clear, completion, read
            pending = read(db, body['guild_id'], body['conversation_id'], body['user_id'], body['revision'])
            body = {k: v for k, v in body.items() if k != 'continuation'}
            if not requested(body['request_text'], body['action']):
                if (not pending or body.get('continuation_id') != pending['source']['id'] or
                        not completion(pending, body['request_text'], body['action'], body['argument'],
                                       body['source'], body['guild_id'], body['conversation_id'],
                                       body['user_id'], body['revision'], body.get('addressed_bot_id', ''))):
                    raise ValueError('Ask to play a song, or answer the current song question in this conversation')
                if source_used(db, body['guild_id'], pending['source']['id']):
                    raise ValueError('This request already ran')
                body['continuation'] = pending
            if db.execute('SELECT 1 FROM gene_controls WHERE user_id=? AND created>?', (body['user_id'], time.time()-3)).fetchone():
                raise ValueError('Give Gene a moment before another action')
            db.execute('INSERT INTO gene_controls(id,user_id,created,state,payload) VALUES (?,?,?,?,?)',
                       (identifier, body['user_id'], time.time(), 'queued', json.dumps(body)))
            if body.get('continuation'):
                db.execute('INSERT INTO gene_control_origins VALUES (?,?)',
                           (source_key(body['guild_id'], pending['source']['id']), time.time()))
            clear(db, body['guild_id'], body['conversation_id'], body['user_id'])
        return {'id': identifier}

    def poll(self, available):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO gene_control_catalog VALUES (1,?,?)',
                       (time.time(), json.dumps([a for a in available if a in COMMANDS])))
            db.execute('DELETE FROM gene_controls WHERE created<?', (time.time()-86400,))
            db.execute('DELETE FROM gene_control_origins WHERE created<?', (time.time()-86400,))
            db.execute("UPDATE gene_controls SET state='expired',result=? WHERE state='queued' AND created<?",
                       (json.dumps({'blocked': 'Gene did not start the request in time; nothing was run.'}), time.time()-30))
            row = db.execute("SELECT * FROM gene_controls WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                return None
            lease = uuid.uuid4().hex
            db.execute("UPDATE gene_controls SET state='claimed',lease=? WHERE id=?", (lease, row['id']))
            return {'id': row['id'], 'lease': lease, **json.loads(row['payload'])}

    def start(self, identifier, lease):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM gene_controls WHERE id=? AND lease=?', (identifier, lease)).fetchone()
            if not row or row['state'] != 'claimed' or row['created'] < time.time()-45:
                raise ValueError('That action expired or was already started')
            body = json.loads(row['payload'])
            current = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (row['user_id'],)).fetchone()
            if body['revision'] != (current[0] if current else 0):
                raise ValueError('Conversation was cleared before Gene started')
            db.execute("UPDATE gene_controls SET state='running' WHERE id=?", (identifier,))

    def finish(self, identifier, lease, result):
        with self.store.connection() as db:
            db.execute("UPDATE gene_controls SET state='finished',result=? WHERE id=? AND lease=? AND state IN ('claimed','running')",
                       (json.dumps(result), identifier, lease))

    def result(self, identifier, user, guild, conversation):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM gene_controls WHERE id=? AND user_id=?', (identifier, user)).fetchone()
            if not row:
                raise KeyError('Action not found')
            payload = json.loads(row['payload'])
            if payload['guild_id'] != guild or payload['conversation_id'] != conversation:
                raise ValueError('Action belongs to another conversation')
            return self._result(row)

    def _result(self, row):
        if row['state'] in ('finished', 'expired'):
            return {'done': True, **json.loads(row['result'])}
        if row['state'] in ('claimed', 'running'):
            with self.store.connection() as db:
                report = db.execute('SELECT body FROM gene_runtime_health WHERE id=1').fetchone()
            if report and json.loads(report['body'])['started'] > row['created']:
                return {'done': True, 'uncertain': 'Gene restarted after accepting this request; its outcome was not '
                        'confirmed. Do not retry automatically or claim success.'}
        if row['created'] < time.time()-115:
            return {'done': True, 'uncertain': 'Gene has not confirmed the outcome. Do not retry automatically or claim success.'}
        return {'done': False}
