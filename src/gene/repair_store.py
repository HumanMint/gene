"""Authenticated repair intake; member/model claims never become owner approvals."""

import time
import uuid

from .fri_context import ChatSource
from .privacy import clean
from .repair_policy import ACTOR, POLICY, authorized
from .reviews import request_reference


def initialize(db):
    db.executescript('''
        INSERT OR IGNORE INTO settings VALUES ('repair_policy', 'disabled');
        CREATE TABLE IF NOT EXISTS repair_jobs (
            job_id TEXT PRIMARY KEY REFERENCES jobs(id), base_sha TEXT NOT NULL,
            policy TEXT NOT NULL, request_ref TEXT NOT NULL
        );
    ''')


def record(db, job_id):
    row = db.execute('SELECT * FROM repair_jobs WHERE job_id=?', (job_id,)).fetchone()
    return dict(row) if row else None


def eligible(store, db, job, proposal=None):
    saved = record(db, job['id'])
    return bool(saved and store._setting(db, 'repair_policy') == POLICY and
                saved['policy'] == POLICY and saved['request_ref'] == request_reference(job) and
                saved['base_sha'] == store._setting(db, 'active_sha') and
                job['created'] > time.time()-6*3600 and
                (proposal is None or authorized(job, saved, proposal)))


def escalate(store, db, job_id, reason, state='awaiting_owner'):
    db.execute('UPDATE jobs SET kind="feature",state=?,approved_by=NULL,approved_sha=NULL,'
               'intake_approved_by=NULL,intake_approved_ref=NULL,result=?,updated=? WHERE id=?',
               (state, clean(reason, 2000), time.time(), job_id))
    store._audit(db, ACTOR, 'repair_needs_owner', job_id, reason)


def request(store, body):
    source = ChatSource.model_validate(body['source']).check(
        body['guild_id'], body['conversation_id'], body['user_id'])
    if source.kind != 'discord_message' or not -5 <= time.time()-source.timestamp().timestamp() <= 300:
        raise ValueError('Repair checks need a fresh Discord message')
    text = clean(body['body'], 5500)
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        revision = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (body['user_id'],)).fetchone()
        if body['revision'] != (revision[0] if revision else 0):
            raise ValueError('Conversation was cleared before repair verification')
        if store._setting(db, 'repair_policy') != POLICY:
            return {'accepted': False, 'reason': 'Automatic repairs are not enabled; use owner review.'}
        if store._setting(db, 'paused') == 'true':
            return {'accepted': False, 'reason': 'Development is paused; no automatic repair started.'}
        base = store._setting(db, 'active_sha')
        if not base:
            return {'accepted': False, 'reason': 'No live release is available to verify.'}
        existing = db.execute('SELECT j.id,j.state FROM jobs j JOIN repair_jobs r ON j.id=r.job_id '
            'WHERE j.source_id=? OR (r.base_sha=? AND j.kind="repair" AND '
            'j.state IN ("queued","building","approved","deploying")) ORDER BY j.created LIMIT 1',
            ('repair:'+source.id, base)).fetchone()
        if existing:
            return {'accepted': True, 'id': existing['id'], 'state': existing['state'], 'duplicate': True}
        count = db.execute('SELECT count(*) FROM repair_jobs r JOIN jobs j ON r.job_id=j.id WHERE j.created>?',
                           (time.time()-86400,)).fetchone()[0]
        if count >= 3:
            return {'accepted': False, 'reason': 'Three automatic checks have already started in the past day; owner review is available.'}
        job_id, now = uuid.uuid4().hex[:12], time.time()
        db.execute('INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,created,updated,state,kind) '
            'VALUES (?,?,?,?,?,?,?,?,"queued","repair")', (job_id, body['guild_id'], body['user_id'],
            'repair:'+source.id, source.evidence()['url'], text, now, now))
        job = dict(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        db.execute('INSERT INTO repair_jobs VALUES (?,?,?,?)', (job_id, base, POLICY, request_reference(job)))
        store._audit(db, ACTOR, 'repair_verification_queued', job_id, 'Verify an existing fault before editing feature code.')
        return {'accepted': True, 'id': job_id, 'state': 'queued', 'duplicate': False}
