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
    ''')


class Controls:
    def __init__(self, store):
        self.store = store

    def catalog(self):
        with self.store.connection() as db:
            row = db.execute('SELECT at,actions FROM gene_control_catalog WHERE id=1').fetchone()
            return json.loads(row['actions']) if row and row['at'] > time.time()-20 else []

    def enqueue(self, body):
        source = ChatSource.model_validate(body['source']).check(body['guild_id'], body['conversation_id'], body['user_id'])
        if source.kind != 'discord_message' or not -5 <= time.time()-source.timestamp().timestamp() <= 300:
            raise ValueError('Gene needs a fresh Discord message request')
        arguments(body['action'], body['argument'])
        if not requested(body['request_text'], body['action']):
            raise ValueError('Ask explicitly to run that action now; examples do not execute commands')
        identifier = hashlib.sha256((body['guild_id']+':'+source.id).encode()).hexdigest()
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT id FROM gene_controls WHERE id=?', (identifier,)).fetchone()
            if old:
                return {'id': identifier}  # Never enqueue the same message twice, even after a timeout.
            row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (body['user_id'],)).fetchone()
            if body['revision'] != (row[0] if row else 0):
                raise ValueError('Conversation changed before the action could start')
            if db.execute('SELECT 1 FROM gene_controls WHERE user_id=? AND created>?', (body['user_id'], time.time()-3)).fetchone():
                raise ValueError('Give Gene a moment before another action')
            db.execute('INSERT INTO gene_controls(id,user_id,created,state,payload) VALUES (?,?,?,?,?)',
                       (identifier, body['user_id'], time.time(), 'queued', json.dumps(body)))
        return {'id': identifier}

    def poll(self, available):
        with self.store.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT OR REPLACE INTO gene_control_catalog VALUES (1,?,?)',
                       (time.time(), json.dumps([a for a in available if a in COMMANDS])))
            db.execute('DELETE FROM gene_controls WHERE created<?', (time.time()-86400,))
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
            if row['state'] in ('finished', 'expired'):
                return {'done': True, **json.loads(row['result'])}
            if row['created'] < time.time()-115:
                return {'done': True, 'uncertain': 'Gene has not confirmed the outcome. Do not retry automatically or claim success.'}
            return {'done': False}
