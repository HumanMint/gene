"""The owner's one reply to a member whose reported Gene fault shipped a fix.

The controller keeps the mark, so a second tap, a restart or a redelivered DM never replies twice.
Only a deployed repair a member reported, even one the owner finished after its automatic checks
handed it over, qualifies while its report is still on record (an erased request has no source left)
and its commit is still the live release. Fri claims only after its Discord reads, right before it
sends, so this claim is the send-time check. Recording the reply runs the same checks again: one that
stopped holding while the claim was on its way to Fri, or while Fri sent, is never recorded as sent
(gene #47), and Fri hears why.

A Fri that offers it (requester-notice-authorize) claims only to prepare. Once its last Discord read is done
it asks for the send itself, which runs the checks again right before it sends (fri #214). Rows stay in the
states an older controller knows, so a rollback settles them as it does its own. A preparation is failed with
its lease still held: nothing has posted, and an older controller, which can't clear a send, claims it as
refused. A cleared send is sending, which an older one records with the tap's lease. Preparing never posts, so
a claim left by a Fri that stopped before it was cleared is taken over once it's old (gene #139). A cleared
send whose outcome never came back may have posted, so like an unconfirmed one it's never tried again: Discord
can't tell the notice from another reply of Fri's to the report. Recorded outcomes that may have posted keep
their lease, so a finish whose answer was lost hears it again.
"""

import json
import time
import uuid

from .store import Conflict

FINISHED = ('sent', 'failed', 'uncertain')  # failed: nothing posted, so it may be tried again.
# A reply Fri sent after its claim stopped holding. It may still be up, so like uncertain it is never tried again.
INVALIDATED = 'invalidated'
PREPARE_SECONDS = 120  # A tap still reading Discord. Older, its Fri stopped before the send, and the next tap takes over.


def preparing(row):
    """A tap that claimed to prepare and wasn't cleared to send: failed, with its lease still held."""
    return row is not None and row['state'] == 'failed' and row['lease'] is not None


def initialize(db):
    db.execute("CREATE TABLE IF NOT EXISTS requester_notices (job_id TEXT PRIMARY KEY REFERENCES jobs(id), "
               "lease TEXT, state TEXT NOT NULL, message_id TEXT NOT NULL DEFAULT '', at REAL NOT NULL)")


def unqualified(store, db, job, claiming=True):
    """Why a reply to this repair's reporter wouldn't hold now, or None when it would.

    claiming=False words it for a reply already on its way, which is never tried again."""
    from .runtime_state import current as runtime_unknown
    if runtime_unknown(db):  # A restore that never proved healthy: active_sha may not be what's running.
        return "Gene's runtime is unknown after a failed restore, so the fix may not be live"
    # Started by repair intake, not merely labelled like it; shipped by the deployer, which dates it.
    shipped = db.execute('SELECT d.at FROM deployments d JOIN repair_jobs r ON r.job_id=d.job_id '
                         'WHERE d.job_id=? AND d.state="deployed"', (job['id'],)).fetchone()
    if (job['state'] != 'deployed' or not shipped or not job['source_id'].startswith('repair:') or
            not job['source_url'] or not job['approved_sha']):
        return ('Only a shipped repair whose report is still on record can reply to its reporter' if claiming
                else 'The report is no longer on record')  # It was on record at the claim: an erasure since.
    # Deployment times, not jobs.updated: an erasure touches a job's updated time long after it ran.
    if db.execute('SELECT 1 FROM deployments d JOIN jobs j ON j.id=d.job_id WHERE j.kind="rollback" '
                  'AND d.state="deployed" AND d.at>?', (shipped['at'],)).fetchone():
        return 'A rollback ran after this repair shipped, so the fix may not be live'
    if store._setting(db, 'active_sha') != job['approved_sha']:
        return 'A newer release is live now, so this repair is not what the reporter is running'
    # active_sha moves only once a deployment is recorded; the runtime may switch well before that.
    if db.execute('SELECT 1 FROM jobs WHERE state="deploying"').fetchone():
        return ('Another release is going out right now, so tap again once it lands' if claiming
                else 'Another release started going out, so the fix may not stay live')
    # An approved rollback of this release goes out on the deployer's next pass: the reply would race it.
    # The deployer fails one approved against an older release instead, so that one doesn't count.
    for row in db.execute('SELECT proposal FROM jobs WHERE kind="rollback" AND state="approved"'):
        if json.loads(row['proposal'])['base_sha'] == job['approved_sha']:
            return 'A rollback is waiting to go out, so the fix may not stay live'
    return None


def claim(store, job_id, actor, prepare=False):
    """prepare: a Fri that asks for the send right before it (see authorize). Without it the claim clears the send."""
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        job = store._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
        if reason := unqualified(store, db, job):
            raise Conflict(reason)
        row = db.execute('SELECT state,lease,at FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()
        if row and row['state'] == 'sent':
            raise Conflict('The reporter was already told')
        if preparing(row) and time.time()-row['at'] < PREPARE_SECONDS:
            raise Conflict('Another tap is replying to the reporter right now')
        if row and row['state'] != 'failed':  # A send cleared, or one that may have posted.
            raise Conflict('A reply to the reporter may already have posted, so it is not tried again')
        lease = uuid.uuid4().hex
        db.execute("INSERT INTO requester_notices(job_id,lease,state,at) VALUES (?,?,?,?) ON CONFLICT(job_id) "
                   "DO UPDATE SET lease=excluded.lease,state=excluded.state,message_id='',at=excluded.at",
                   (job_id, lease, 'failed' if prepare else 'sending', time.time()))
        store._audit(db, actor, 'requester_notice', job_id)
        return {'lease': lease, 'job': {key: job[key] for key in ('id', 'guild_id', 'user_id', 'source_url', 'approved_sha')}}


def authorize(store, job_id, lease):
    """The send itself, asked for once Fri's last Discord read is done: still this tap's, and its checks still hold.

    Refused, nothing has posted, and Fri records it as failed, so a later tap may try again."""
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT lease,state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()
        if not preparing(row) or row['lease'] != lease:
            raise Conflict('This tap no longer holds the reply, so it is not sent')
        if reason := unqualified(store, db, store._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())):
            raise Conflict(reason)
        db.execute("UPDATE requester_notices SET state='sending',at=? WHERE job_id=?", (time.time(), job_id))


def finish(store, job_id, lease, state, message_id=''):
    """Record what Fri saw, and return what was recorded with why, if a sent reply's claim stopped holding.

    The same lease again, its answer lost, hears what was recorded and changes nothing."""
    if state not in FINISHED:
        raise ValueError('Unknown reply outcome')
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT lease,state FROM requester_notices WHERE job_id=?', (job_id,)).fetchone()
        if row and row['lease'] == lease and row['state'] in ('sent', 'uncertain', INVALIDATED):
            why = db.execute('SELECT detail FROM audit WHERE action="requester_notice_invalidated" AND target=? '
                             'ORDER BY id DESC LIMIT 1', (job_id,)).fetchone()
            return {'state': row['state'], 'reason': why[0] if why and row['state'] == INVALIDATED else None}
        # A claim sending, or a tap preparing (failed with its lease held).
        if not row or row['state'] not in ('sending', 'failed') or row['lease'] != lease:
            raise Conflict('This reply has already been recorded')
        reason = None
        if state == 'sent':  # A rollback, a newer release or the reporter's erasure that landed since the claim.
            job = store._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            reason = unqualified(store, db, job, claiming=False)
        if reason:
            state = INVALIDATED
            store._audit(db, 'controller', 'requester_notice_invalidated', job_id, reason)
        # Only a reply that holds keeps its link: an erased reporter's job keeps nothing that points at them.
        # A refusal lets its lease go, so failed with a lease only ever means a tap preparing.
        db.execute('UPDATE requester_notices SET state=?,message_id=?,lease=?,at=? WHERE job_id=?',
                   (state, message_id if state == 'sent' else '', None if state == 'failed' else lease, time.time(),
                    job_id))
    return {'state': state, 'reason': reason}
