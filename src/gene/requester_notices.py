"""The owner's one reply to a member whose reported Gene fault shipped a fix.

The controller keeps the mark, so a second tap, a restart or a redelivered DM never replies twice.
Only a deployed repair a member reported, even one the owner finished after its automatic checks
handed it over, qualifies while its report is still on record (an erased request has no source left)
and its commit is still the live release. Fri claims only after its Discord reads, right before it
sends, so this claim is the send-time check.
"""

import json
import time
import uuid

from .store import Conflict

FINISHED = ('sent', 'failed', 'uncertain')  # failed: Discord refused it before anything posted, so it may be tried again.


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS requester_notices (job_id TEXT PRIMARY KEY REFERENCES jobs(id), "
               "lease TEXT, state TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '', at REAL NOT NULL)")


def claim(store, job_id, actor):
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        job = store._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        # Started by repair intake, not merely labelled like it; shipped by the deployer, which dates it.
        shipped = db.execute('SELECT d.at FROM deployments d JOIN repair_jobs r ON r.job_id=d.job_id '
                             'WHERE d.job_id=? AND d.state="deployed"', (job_id,)).fetchone()
        if (job['state'] != 'deployed' or not shipped or not job['source_id'].startswith('repair:') or
                not job['source_url'] or not job['approved_sha']):
            raise Conflict('Only a shipped repair whose report is still on record can reply to its reporter')
        # Deployment times, not jobs.updated: an erasure touches a job's updated time long after it ran.
        if db.execute('SELECT 1 FROM deployments d JOIN jobs j ON j.id=d.job_id WHERE j.kind="rollback" '
                      'AND d.state="deployed" AND d.at>?', (shipped['at'],)).fetchone():
            raise Conflict('A rollback ran after this repair shipped, so the fix may not be live')
        if store._setting(db, 'active_sha') != job['approved_sha']:
            raise Conflict('A newer release is live now, so this repair is not what the reporter is running')
        # active_sha moves only once a deployment is recorded; the runtime may switch well before that.
        if db.execute('SELECT 1 FROM jobs WHERE state="deploying"').fetchone():
            raise Conflict('Another release is going out right now, so tap again once it lands')
        # An approved rollback of this release goes out on the deployer's next pass: the reply would race it.
        # The deployer fails one approved against an older release instead, so that one doesn't count.
        for row in db.execute('SELECT proposal FROM jobs WHERE kind="rollback" AND state="approved"'):
            if json.loads(row['proposal'])['base_sha'] == job['approved_sha']:
                raise Conflict('A rollback is waiting to go out, so the fix may not stay live')
        row = db.execute('SELECT state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()
        if row and row['state'] == 'sent':
            raise Conflict('The reporter was already told')
        if row and row['state'] != 'failed':
            raise Conflict('A reply to the reporter may already have posted, so it is not tried again')
        lease = uuid.uuid4().hex
        db.execute("INSERT INTO requester_notices(job_id,lease,state,at) VALUES (?,?,'sending',?) "
                   "ON CONFLICT(job_id) DO UPDATE SET lease=excluded.lease,state='sending',message_id='',at=excluded.at",
                   (job_id, lease, time.time()))
        store._audit(db, actor, 'requester_notice', job_id)
        return {'lease': lease, 'job': {key: job[key] for key in ('id', 'guild_id', 'user_id', 'source_url', 'approved_sha')}}


def finish(store, job_id, lease, state, message_id=''):
    if state not in FINISHED:
        raise ValueError('Unknown reply outcome')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT lease,state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()
        if not row or row['state'] != 'sending' or row['lease'] != lease:
            raise Conflict('This reply has already been recorded')
        db.execute('UPDATE requester_notices SET state=?,message_id=?,lease=NULL,at=? WHERE job_id=?',
                   (state, message_id if state == 'sent' else '', time.time(), job_id))
