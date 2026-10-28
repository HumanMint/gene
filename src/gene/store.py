from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import maintenance
from .fri_agent_store import initialize
from .fri_context import ChatSource
from .fri_memory import CONVERSATION_ONLY, TTL, personal_note
from .privacy import clean
from .reviews import pending_notice, pending_review, request_reference

SHA = re.compile(r'^[a-f0-9]{40}$')
ELSEWHERE_SECONDS = 12 * 3600  # A member's exchanges elsewhere in the server, for picking a topic back up.
ELSEWHERE_TURNS = 2
LOOP_ITEMS = 5  # Of each kind of open loop in a conversation.
LOOP_SETTLED_SECONDS = 86400  # A reminder that went out (or didn't) stays visible for a day.
LOOP_UNDERWAY_SECONDS = 3 * 3600  # A game night stays visible while it's probably still on.
# A withdrawn message's fence: as long as a saved exchange is kept, far longer than any turn holding it still runs.
WITHDRAWN_SECONDS = 7 * 86400
WITHDRAWN = 'That message was edited or deleted while Fri was replying.'
SUMMARY_CHANGED = 'What Fri remembered about you changed while it was replying. Ask again for a fresh reply.'


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            initialize(db)
            from .control_store import initialize as initialize_controls
            initialize_controls(db)
            from .fri_research import initialize as initialize_research
            initialize_research(db)
            from .fri_workspace_store import initialize as initialize_workspace
            initialize_workspace(db)
            from .fri_social_store import initialize as initialize_social
            initialize_social(db)
            from .fri_pending import initialize as initialize_pending
            initialize_pending(db)
            from .fri_mentions import initialize as initialize_mentions
            initialize_mentions(db)
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO settings VALUES ('paused', 'true');
                INSERT OR IGNORE INTO settings VALUES ('active_sha', '');
                INSERT OR IGNORE INTO settings VALUES ('previous_sha', '');
                INSERT OR IGNORE INTO settings VALUES ('code_model', 'gpt-6.1-sol');
                INSERT OR IGNORE INTO settings VALUES ('code_effort', 'high');
                INSERT OR IGNORE INTO settings VALUES ('chat_model', 'gpt-6.1-sol');
                INSERT OR IGNORE INTO settings VALUES ('chat_effort', 'high');
                INSERT OR IGNORE INTO settings VALUES ('models_requested_at', '1');
                INSERT OR IGNORE INTO settings VALUES ('models_refreshed_at', '0');
                INSERT OR IGNORE INTO settings VALUES ('models_error', '');
                CREATE TABLE IF NOT EXISTS models (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, efforts TEXT NOT NULL,
                    default_effort TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, user_id TEXT NOT NULL,
                    source_id TEXT NOT NULL UNIQUE, source_url TEXT NOT NULL, body TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'queued', created REAL NOT NULL, updated REAL NOT NULL,
                    lease TEXT, heartbeat REAL, proposal TEXT, result TEXT, approved_by TEXT,
                    approved_sha TEXT, kind TEXT NOT NULL DEFAULT 'feature', model TEXT, effort TEXT
                );
                CREATE TABLE IF NOT EXISTS usage (
                    attempt_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                    model TEXT NOT NULL, input_tokens INTEGER, cached_input_tokens INTEGER,
                    output_tokens INTEGER, reasoning_output_tokens INTEGER, turns INTEGER NOT NULL,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, body TEXT NOT NULL,
                    source_url TEXT NOT NULL, approved_by TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, actor TEXT NOT NULL,
                    action TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployments (
                    job_id TEXT PRIMARY KEY, sha TEXT NOT NULL, previous_sha TEXT NOT NULL,
                    state TEXT NOT NULL, at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deployment_receipts (
                    job_id TEXT PRIMARY KEY, lease TEXT NOT NULL, result TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_notices (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id), phase TEXT NOT NULL,
                    reference TEXT NOT NULL, channel_id TEXT, message_id TEXT,
                    delivered INTEGER NOT NULL DEFAULT 0, lease TEXT, lease_until REAL NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chat_usage (
                    attempt_id TEXT PRIMARY KEY, model TEXT NOT NULL, effort TEXT NOT NULL,
                    status TEXT NOT NULL, at REAL NOT NULL, input_tokens INTEGER,
                    cached_input_tokens INTEGER, output_tokens INTEGER, reasoning_output_tokens INTEGER
                );
                CREATE TABLE IF NOT EXISTS chat_models (id TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS chat_revisions (user_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS personal_preferences (
                    user_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS personal_notes (
                    id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, category TEXT NOT NULL, evidence TEXT NOT NULL, at REAL NOT NULL,
                    UNIQUE(guild_id,channel_id,user_id,category)
                );
                CREATE TABLE IF NOT EXISTS chat_turns (
                    id INTEGER PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
                    user_id TEXT NOT NULL, input TEXT NOT NULL, reply TEXT NOT NULL,
                    model TEXT NOT NULL, effort TEXT NOT NULL, at REAL NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER
                );
                -- A member's message withdrawn after Fri saved an exchange with it (gene #90): its id, never its words.
                CREATE TABLE IF NOT EXISTS chat_turn_withdrawals (
                    guild_id TEXT NOT NULL, user_id TEXT NOT NULL, message_id TEXT NOT NULL, at REAL NOT NULL,
                    PRIMARY KEY(guild_id, user_id, message_id)
                );
            ''')
            from .repair_store import initialize as initialize_repairs
            initialize_repairs(db)
            from .requester_notices import initialize as initialize_requester_notices
            initialize_requester_notices(db)
            maintenance.initialize(db)
            from .runtime_state import initialize as initialize_runtime_state
            initialize_runtime_state(db)
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            for name in ('model', 'effort', 'intake_approved_by', 'intake_approved_ref', 'progress'):
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')
            if 'provenance' not in columns:
                # Other members whose messages shaped a submitted draft, with the revision seen.
                db.execute("ALTER TABLE jobs ADD COLUMN provenance TEXT NOT NULL DEFAULT '[]'")
            if 'attributed' not in columns:
                # Submitted with a provenance list. Older requests may quote anyone with no record of whom.
                db.execute('ALTER TABLE jobs ADD COLUMN attributed INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE INDEX IF NOT EXISTS jobs_requester ON jobs(guild_id, user_id, updated)')
            if 'source' not in {r['name'] for r in db.execute('PRAGMA table_info(chat_turns)')}:
                db.execute("ALTER TABLE chat_turns ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            from .fri_memory_store import initialize as initialize_personal_notes
            initialize_personal_notes(db)
            from .fri_member_summary import initialize as initialize_summaries
            initialize_summaries(db)
            if 'burst' not in {r['name'] for r in db.execute('PRAGMA table_info(chat_turns)')}:
                db.execute("ALTER TABLE chat_turns ADD COLUMN burst TEXT NOT NULL DEFAULT '[]'")
            columns = {r['name'] for r in db.execute('PRAGMA table_info(chat_turns)')}
            if 'response_kind' not in columns:
                db.execute("ALTER TABLE chat_turns ADD COLUMN response_kind TEXT NOT NULL DEFAULT 'reply'")
            if 'reaction' not in columns:
                db.execute('ALTER TABLE chat_turns ADD COLUMN reaction TEXT')
            if 'allow_closing' not in columns:
                db.execute('ALTER TABLE chat_turns ADD COLUMN allow_closing INTEGER NOT NULL DEFAULT 0')
            if 'provenance' not in columns:
                # Other members whose messages shaped a saved reply, with the privacy revision seen.
                db.execute("ALTER TABLE chat_turns ADD COLUMN provenance TEXT NOT NULL DEFAULT '[]'")
            if 'reply_message_id' not in columns:
                # Fri's delivered Discord message, so later references resolve to these sources.
                db.execute('ALTER TABLE chat_turns ADD COLUMN reply_message_id TEXT')
                db.execute('ALTER TABLE chat_turns ADD COLUMN verbatim INTEGER NOT NULL DEFAULT 0')
            if 'speaker_revision' not in {r['name'] for r in db.execute('PRAGMA table_info(chat_turns)')}:
                # The speaker's revision when saved; references fail closed once it moves.
                db.execute('ALTER TABLE chat_turns ADD COLUMN speaker_revision INTEGER')
            if 'attributed' not in {r['name'] for r in db.execute('PRAGMA table_info(chat_turns)')}:
                # Saved with a provenance list. Older turns, and any a legacy companion writes, may quote
                # other members with no record of whom, so capable companions withhold their replies.
                db.execute('ALTER TABLE chat_turns ADD COLUMN attributed INTEGER NOT NULL DEFAULT 0')
            if 'audience' not in {r['name'] for r in db.execute('PRAGMA table_info(chat_turns)')}:
                # server: said where everyone in the server could read it, so the member's other
                # conversations there may see it. Everything saved before stays where it was said.
                db.execute("ALTER TABLE chat_turns ADD COLUMN audience TEXT NOT NULL DEFAULT 'conversation'")
            if 'summary_generation' not in {r['name'] for r in db.execute('PRAGMA table_info(chat_turns)')}:
                # The member summary's generation the saved reply drew on, if it drew on one (fri #172).
                db.execute('ALTER TABLE chat_turns ADD COLUMN summary_generation INTEGER')
            from .fri_member_summary import settle as settle_summaries
            settle_summaries(db)
            # Existing queued requests have never received intake approval. Fail closed
            # during migration; completed/running jobs and release approvals retain their history.
            db.execute('UPDATE jobs SET state="awaiting_owner" WHERE state="queued" '
                       'AND intake_approved_ref IS NULL AND kind!="repair"')
        path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _audit(db, actor, action, target='', detail=''):
        db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
                   (time.time(), actor, action, target, clean(detail)))

    @staticmethod
    def _job(row):
        if row is None:
            raise KeyError('Request not found')
        result = dict(row)
        result['proposal'] = json.loads(result['proposal']) if result['proposal'] else None
        return result

    @staticmethod
    def _setting(db, key):
        return db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()[0]

    def status(self):
        with self.connection() as db:
            # The controller health check runs even when nobody chats with Fri.
            from .fri_pending import TTL as pending_ttl
            db.execute('DELETE FROM chat_pending WHERE created<?', (time.time()-pending_ttl,))
            db.execute('DELETE FROM chat_turns WHERE at<?', (time.time() - 7 * 86400,))
            db.execute('DELETE FROM personal_notes WHERE at<?', (time.time() - TTL,))
            db.execute('DELETE FROM web_research WHERE created<?', (time.time()-86400,))
            db.execute('DELETE FROM web_requests WHERE created<?', (time.time()-600,))
            db.execute('DELETE FROM mention_grants WHERE expires<?', (time.time()-600,))
            from .fri_reply_checks import prune as prune_reply_checks
            prune_reply_checks(db)
            from .fri_member_summary import prune as prune_summaries
            prune_summaries(db)
            from .runtime_state import current as runtime_unknown
            return {
                'paused': self._setting(db, 'paused') == 'true',
                'active_sha': self._setting(db, 'active_sha'),
                'previous_sha': self._setting(db, 'previous_sha'),
                # Set when a failed deployment's restore never proved healthy: active_sha may not be running.
                'runtime_unknown': runtime_unknown(db),
                'model': self._setting(db, 'code_model'),
                'effort': self._setting(db, 'code_effort') or 'model default',
                'chat_model': self._setting(db, 'chat_model'),
                'chat_effort': self._setting(db, 'chat_effort'),
                'auto_repairs_enabled': self._setting(db, 'repair_policy') == 'existing-features-v1',
                'jobs': {r['state']: r['n'] for r in db.execute(
                    'SELECT state, count(*) AS n FROM jobs GROUP BY state')},
            }

    def set_paused(self, paused: bool, actor: str):
        with self.connection() as db:
            db.execute('UPDATE settings SET value=? WHERE key="paused"',
                       ('true' if paused else 'false',))
            self._audit(db, actor, 'pause' if paused else 'resume')

    def bootstrap(self, sha: str):
        if not SHA.fullmatch(sha):
            raise ValueError('A full commit SHA is required')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._setting(db, 'active_sha'):
                raise Conflict('An active release already exists')
            db.execute('UPDATE settings SET value=? WHERE key="active_sha"', (sha,))
            self._audit(db, 'installer', 'bootstrap', sha)

    def request(self, guild_id: str, user_id: str, source_id: str, source_url: str, body: str, provenance=None,
                revision=None):
        # None: a companion that doesn't track sources. A list, even empty, is complete attribution.
        attributed = provenance is not None
        body = clean(body)
        if len(body) < 5:
            raise ValueError('Describe the request in at least five characters')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE source_id=?', (source_id,)).fetchone()
            if existing:
                return self._job(existing)
            # Checked in the creating transaction: an erasure can't slip in after the companion's fence.
            if revision is not None and self._privacy_state(db, user_id)[0] != revision:
                raise Conflict('Your privacy settings changed while this draft was open. Draft it again.')
            provenance = self._checked_provenance(db, user_id, provenance or ())
            pending = db.execute('SELECT count(*) FROM jobs WHERE user_id=? AND state IN '
                                 '("awaiting_owner","queued","building","review","approved","deploying")',
                                 (user_id,)).fetchone()[0]
            if pending >= 3:
                raise Conflict('You already have three open requests; finish one first')
            job_id = uuid.uuid4().hex[:12]
            now = time.time()
            db.execute('INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,created,updated,state,provenance,'
                       'attributed) VALUES (?,?,?,?,?,?,?,?,"awaiting_owner",?,?)',
                       (job_id, guild_id, user_id, source_id, source_url, body, now, now, json.dumps(provenance), attributed))
            self._audit(db, user_id, 'request', job_id)
            return self._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def review_decision(self, job_id, phase, reference, decision, actor):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            if pending_review(job, self._setting(db, 'active_sha')) != (phase, reference):
                raise Conflict('This review is out of date. Open the current request before deciding')
            if decision == 'reject':
                db.execute('UPDATE jobs SET state="rejected",updated=? WHERE id=?', (time.time(), job_id))
                self._audit(db, actor, 'reject', job_id, phase)
            elif decision == 'approve' and phase == 'intake':
                db.execute('UPDATE jobs SET state="queued",intake_approved_by=?,intake_approved_ref=?,'
                           'updated=? WHERE id=?', (actor, reference, time.time(), job_id))
                self._audit(db, actor, 'allow_coding', job_id, reference)
            elif decision == 'approve' and phase == 'release':
                db.execute('UPDATE jobs SET state="approved",approved_by=?,approved_sha=?,updated=? WHERE id=?',
                           (actor, reference, time.time(), job_id))
                self._audit(db, actor, 'approve', job_id, reference)
            else:
                raise ValueError('Unknown review decision')

    def claim_review_notice(self):
        """Lease one current review notification, retaining message IDs across phases/restarts."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            active = self._setting(db, 'active_sha')
            # A repair its checks handed to the owner is a feature job by then, with no intake approval;
            # repair intake recorded it, so the owner still hears how the release they approved went.
            rows = db.execute('SELECT * FROM jobs WHERE state IN ("awaiting_owner","review") OR (updated>? '
                              'AND (intake_approved_ref IS NOT NULL OR kind IN ("rollback","repair") '
                              'OR id IN (SELECT job_id FROM repair_jobs))) ORDER BY updated',
                              (now - 7 * 86400,)).fetchall()
            for row in rows:
                job = self._job(row)
                current = pending_notice(job, active)
                if not current:
                    continue
                phase, reference = current
                notice = db.execute('SELECT * FROM review_notices WHERE job_id=?', (job['id'],)).fetchone()
                if notice and notice['lease_until'] > now:
                    continue
                same = notice and (notice['phase'], notice['reference']) == current
                if same and (notice['delivered'] or notice['retry_at'] > now):
                    continue
                lease = uuid.uuid4().hex
                db.execute('INSERT INTO review_notices(job_id,phase,reference,lease,lease_until) VALUES (?,?,?,?,?) '
                           'ON CONFLICT(job_id) DO UPDATE SET phase=excluded.phase,reference=excluded.reference,'
                           'lease=excluded.lease,lease_until=excluded.lease_until,delivered=0,retry_at=0',
                           (job['id'], phase, reference, lease, now + 120))
                return {'job': job, 'phase': phase, 'reference': reference, 'lease': lease,
                        'channel_id': notice['channel_id'] if notice else None,
                        'message_id': notice['message_id'] if notice else None}
            return None

    def finish_review_notice(self, job_id, lease, channel_id='', message_id=''):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            notice = db.execute('SELECT * FROM review_notices WHERE job_id=?', (job_id,)).fetchone()
            if not notice or notice['lease'] != lease:
                raise Conflict('The notification lease has changed')
            if channel_id and message_id:
                db.execute('UPDATE review_notices SET channel_id=?,message_id=?,delivered=1,lease=NULL,'
                           'lease_until=0 WHERE job_id=?', (channel_id, message_id, job_id))
            else:
                db.execute('UPDATE review_notices SET lease=NULL,lease_until=0,retry_at=? WHERE job_id=?',
                           (time.time() + 600, job_id))
                self._audit(db, 'runtime', 'owner_dm_unavailable', job_id,
                            'Request remains gated; owner can use the workshop review panel.')

    def get(self, job_id):
        with self.connection() as db:
            return self._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def recent(self, guild_id=None):
        with self.connection() as db:
            return [self._job(r) for r in db.execute(
                'SELECT * FROM jobs WHERE (? IS NULL OR guild_id=?) ORDER BY created DESC LIMIT 20',
                (guild_id, guild_id))]

    def claim(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # A crashed worker needs an explicit new request; do not run duplicated jobs silently.
            expired = list(db.execute('SELECT id FROM jobs WHERE state="building" AND heartbeat<?',
                                      (time.time() - 7200,)))
            for row in expired:
                db.execute('UPDATE jobs SET state="failed",lease=NULL,result=?,updated=? WHERE id=?',
                           ('Worker lease expired. Inspect the job before retrying.', time.time(), row[0]))
                self._audit(db, 'controller', 'lease_expired', row[0])
            if self._setting(db, 'paused') == 'true':
                return None
            if db.execute('SELECT 1 FROM jobs WHERE state IN ("building","deploying")').fetchone():
                return None
            if not self._setting(db, 'active_sha'):
                return None
            model = self._setting(db, 'code_model')
            catalog = db.execute('SELECT * FROM models WHERE id=?', (model,)).fetchone()
            if catalog is None:
                return None
            effort = self._setting(db, 'code_effort') or catalog['default_effort']
            row = db.execute('SELECT * FROM jobs WHERE state="queued" ORDER BY created LIMIT 1').fetchone()
            if row is None:
                return None
            from .repair_store import eligible, escalate, record
            if row['kind'] == 'repair' and not eligible(self, db, dict(row)):
                escalate(self, db, row['id'], 'Automatic repair authorization expired or the live version changed.')
                return None
            if row['kind'] != 'repair' and (not row['intake_approved_by'] or row['intake_approved_ref'] != request_reference(dict(row))):
                db.execute('UPDATE jobs SET state="awaiting_owner",intake_approved_by=NULL,'
                           'intake_approved_ref=NULL WHERE id=?', (row['id'],))
                return None
            lease = uuid.uuid4().hex
            db.execute('UPDATE jobs SET state="building",lease=?,heartbeat=?,updated=?,model=?,effort=? WHERE id=?',
                       (lease, time.time(), time.time(), model, effort, row['id']))
            self._audit(db, 'worker', 'claim', row['id'])
            job = self._job(db.execute('SELECT * FROM jobs WHERE id=?', (row['id'],)).fetchone())
            job['base_sha'] = self._setting(db, 'active_sha')
            job['memories'] = [dict(r) for r in db.execute('SELECT * FROM memories ORDER BY created')]
            if job['kind'] == 'repair':
                job['repair'] = record(db, job['id'])
                job['memories'] = []
            return job

    @staticmethod
    def _leased(db, job_id, lease, state='building'):
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not row or row['state'] != state or row['lease'] != lease:
            raise Conflict('The job lease or state has changed')
        return row

    def heartbeat(self, job_id, lease, progress=''):
        with self.connection() as db:
            self._leased(db, job_id, lease)
            db.execute('UPDATE jobs SET heartbeat=? WHERE id=?', (time.time(), job_id))
            if progress:
                db.execute('UPDATE jobs SET progress=?,updated=? WHERE id=?',
                           (progress, time.time(), job_id))

    def finish_build(self, job_id, lease, proposal: dict | None, error: str = '', outcome='failed'):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._leased(db, job_id, lease)
            if proposal:
                for key in ('sha', 'base_sha'):
                    if not SHA.fullmatch(proposal.get(key, '')):
                        raise ValueError('Proposal must identify complete commit hashes')
                if proposal['sha'] == proposal['base_sha']:
                    raise ValueError('The proposal contains no change')
                if not proposal.get('tests_passed') or not proposal.get('branch', '').startswith('codex/'):
                    raise ValueError('A tested, published feature branch is required')
            if outcome not in ('failed', 'needs_attention'):
                raise ValueError('Invalid build outcome')
            state = 'review' if proposal else outcome
            db.execute('UPDATE jobs SET state=?,proposal=?,result=?,lease=NULL,progress=NULL,updated=? WHERE id=?',
                       (state, json.dumps(proposal) if proposal else None, clean(error), time.time(), job_id))
            self._audit(db, 'worker', state, job_id)
            if row['kind'] == 'repair':
                from .repair_policy import ACTOR
                from .repair_store import eligible, escalate
                if proposal and eligible(self, db, dict(row), proposal):
                    db.execute('UPDATE jobs SET state="approved",approved_by=?,approved_sha=? WHERE id=?',
                               (ACTOR, proposal['sha'], job_id))
                    self._audit(db, ACTOR, 'verified_repair_authorized', job_id, proposal['sha'])
                else:
                    escalate(self, db, job_id, error or 'Automatic verification or independent review did not authorize this repair.',
                             'review' if proposal else 'awaiting_owner')

    def approve(self, job_id, sha, actor):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            if job['state'] != 'review' or not job['proposal']:
                raise Conflict('This request is not awaiting approval')
            if sha != job['proposal']['sha']:
                raise Conflict('Approval must name the exact full commit SHA shown in the proposal')
            if job['proposal']['base_sha'] != self._setting(db, 'active_sha'):
                raise Conflict('The live version changed. This proposal must be rebuilt and retested')
            db.execute('UPDATE jobs SET state="approved",approved_by=?,approved_sha=?,updated=? WHERE id=?',
                       (actor, sha, time.time(), job_id))
            self._audit(db, actor, 'approve', job_id, sha)

    def reject(self, job_id, actor):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            job = self._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())
            if job['state'] not in ('awaiting_owner', 'queued', 'review', 'approved'):
                raise Conflict('Only queued or pending proposals can be rejected')
            db.execute('UPDATE jobs SET state="rejected",updated=? WHERE id=?', (time.time(), job_id))
            self._audit(db, actor, 'reject', job_id)

    def rollback(self, guild_id, actor, expected_sha=None):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = self._setting(db, 'previous_sha')
            current = self._setting(db, 'active_sha')
            if not previous:
                raise Conflict('No previous healthy release is available')
            if expected_sha is not None and expected_sha != previous:
                raise Conflict('The previous release changed. Reopen rollback before approving it')
            job_id = uuid.uuid4().hex[:12]
            proposal = {'sha': previous, 'base_sha': current, 'summary': 'Owner-requested rollback'}
            now = time.time()
            db.execute('INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated,'
                       'proposal,approved_by,approved_sha,kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, guild_id, actor, 'rollback:' + job_id, '', 'Roll back Gene', 'approved',
                        now, now, json.dumps(proposal), actor, previous, 'rollback'))
            self._audit(db, actor, 'rollback', job_id, previous)
            return self._job(db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())

    def claim_deploy(self):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            # Never auto-expire a deployment lease: a crash requires reconciliation with the live runtime.
            if self._setting(db, 'paused') == 'true':
                return None
            if db.execute('SELECT 1 FROM jobs WHERE state IN ("deploying","building")').fetchone():
                return None
            rows = db.execute('SELECT * FROM jobs WHERE state="approved" ORDER BY updated').fetchall()
            # Outside the maintenance window approved work waits for the next one, untouched; an owner's
            # rollback restores service at any hour. Claims stop early enough for the switch to start in time.
            window = maintenance.is_open(margin=maintenance.CLAIM_MARGIN)
            from .runtime_state import current as runtime_unknown
            unknown = runtime_unknown(db)
            for row in rows:
                job = self._job(row)
                if not window and job['kind'] != 'rollback':
                    continue
                # Nothing proves which release is running, so approved work waits for a maintainer, untouched.
                # A rollback still restores service: it proves its release healthy or puts the last one back.
                if unknown and job['kind'] != 'rollback':
                    continue
                if job['kind'] != 'rollback' and maintenance.batch_used(db, job):
                    continue  # Today's one production batch is used: the job waits for a later day, untouched.
                proposal = job['proposal']
                if job['kind'] == 'repair':
                    from .repair_policy import ACTOR
                    from .repair_store import eligible, escalate
                    if job['approved_by'] != ACTOR or not eligible(self, db, job, proposal):
                        escalate(self, db, job['id'], 'Automatic repair authorization is no longer current.', 'review')
                        continue
                if (proposal['base_sha'] != self._setting(db, 'active_sha')
                        or proposal['sha'] != job['approved_sha']):
                    db.execute('UPDATE jobs SET state="failed",result=? WHERE id=?',
                               ('Stale approval; live version changed. Rebuild required.', job['id']))
                    continue
                lease = uuid.uuid4().hex
                db.execute('UPDATE jobs SET state="deploying",lease=?,updated=? WHERE id=?',
                           (lease, time.time(), job['id']))
                db.execute('INSERT INTO deployments VALUES (?,?,?,?,?)',
                           (job['id'], proposal['sha'], proposal['base_sha'], 'deploying', time.time()))
                self._audit(db, 'deployer', 'deploy_start', job['id'], proposal['sha'])
                return self._job(db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone())
            return None

    def finish_deploy(self, job_id, lease, success, detail, runtime_unknown=False):
        """runtime_unknown: the deployer stopped Red and no release proved healthy after, restore included."""
        if success and runtime_unknown:
            raise ValueError('A release that proved healthy is a known runtime')
        detail = clean(detail)
        # The receipt every controller records and compares a replay with; the runtime evidence stays outside it,
        # so a receipt one side of an upgrade or rollback committed still matches its replay on the other.
        result = json.dumps({'success': success, 'detail': detail}, sort_keys=True)
        from . import runtime_state
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            receipt = db.execute('SELECT lease,result FROM deployment_receipts WHERE job_id=?',
                                 (job_id,)).fetchone()
            if receipt:
                if receipt['lease'] != lease or receipt['result'] != result:
                    raise Conflict('Deployment completion differs from the recorded receipt')
                if runtime_unknown:  # Perhaps committed by a controller that ignored it.
                    runtime_state.late(db, job_id)
                return  # A lost acknowledgement must not reapply or rewind a release.
            row = self._leased(db, job_id, lease, 'deploying')
            proposal = json.loads(row['proposal'])
            if success:
                db.execute('UPDATE settings SET value=? WHERE key="previous_sha"', (proposal['base_sha'],))
                db.execute('UPDATE settings SET value=? WHERE key="active_sha"', (proposal['sha'],))
                if runtime_state.current(db):  # The deployer just saw this release healthy: what's running is known.
                    runtime_state.known(db)
                    self._audit(db, 'deployer', 'runtime_known', job_id, proposal['sha'])
            elif runtime_unknown:
                runtime_state.mark(db, dict(row), proposal)
            state = 'deployed' if success else 'failed'
            db.execute('UPDATE jobs SET state=?,result=?,lease=NULL,updated=? WHERE id=?',
                       (state, clean(detail), time.time(), job_id))
            db.execute('UPDATE deployments SET state=? WHERE job_id=?', (state, job_id))
            db.execute('INSERT INTO deployment_receipts VALUES (?,?,?)', (job_id, lease, result))
            self._audit(db, 'deployer', state, job_id, detail)

    def add_usage(self, job_id, lease, attempt_id, model, counters):
        keys = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
        for key in keys:
            value = counters.get(key)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
                raise ValueError('Usage counters must be nonnegative integers or unknown')
        with self.connection() as db:
            self._leased(db, job_id, lease)
            # Each execution gets an attempt id. Retrying the POST cannot double count it.
            db.execute('INSERT OR IGNORE INTO usage VALUES (?,?,?,?,?,?,?,?,?)',
                       (attempt_id, job_id, model, *(counters.get(k) for k in keys),
                        counters.get('turns', 0), time.time()))

    def usage(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT model, count(*) AS attempts, '
                'sum(input_tokens) AS input_tokens, sum(cached_input_tokens) AS cached_input_tokens, '
                'sum(output_tokens) AS output_tokens, '
                'sum(input_tokens IS NULL OR output_tokens IS NULL) AS unknown_attempts '
                'FROM usage GROUP BY model')]

    def add_chat_usage(self, attempt_id, model, effort, status, counters, role='conversation'):
        keys = ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
        for key in keys:
            value = counters.get(key)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError('Usage counters must be nonnegative integers or unknown')
        with self.connection() as db:
            # Contains no user IDs or conversation text; clearing history preserves totals.
            db.execute('INSERT OR IGNORE INTO chat_usage VALUES (?,?,?,?,?,?,?,?,?)',
                       (attempt_id, model, effort, status, time.time(), *(counters.get(k) for k in keys)))
            db.execute('INSERT OR IGNORE INTO agent_usage_roles VALUES (?,?)', (attempt_id, role))

    def chat_usage(self):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT model, effort, count(*) AS attempts, '
                'sum(input_tokens) AS input_tokens, sum(cached_input_tokens) AS cached_input_tokens, '
                'sum(output_tokens) AS output_tokens, sum(reasoning_output_tokens) AS reasoning_output_tokens, '
                'sum(status != "completed") AS failed_attempts, '
                'sum(input_tokens IS NULL OR output_tokens IS NULL) AS unknown_attempts '
                'FROM chat_usage GROUP BY model, effort')]

    def memories(self, guild_id):
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM memories WHERE guild_id=? ORDER BY created',
                                               (guild_id,))]

    def remember(self, guild_id, actor, body, source_url):
        with self.connection() as db:
            memory_id = uuid.uuid4().hex[:12]
            db.execute('INSERT INTO memories VALUES (?,?,?,?,?,?)',
                       (memory_id, guild_id, clean(body, 1000), source_url, actor, time.time()))
            self._audit(db, actor, 'remember', memory_id)
            return memory_id

    def forget(self, memory_id, guild_id, actor):
        with self.connection() as db:
            db.execute('DELETE FROM memories WHERE id=? AND guild_id=?', (memory_id, guild_id))
            self._audit(db, actor, 'forget', memory_id)

    def erase_user(self, user_id):
        self.personal_control(user_id, 'disable')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self._erase_jobs(db, db.execute('SELECT id,proposal FROM jobs WHERE user_id=?', (user_id,)).fetchall(),
                             '[User request erased]', 'deleted')
            db.execute('DELETE FROM memories WHERE approved_by=?', (user_id,))
            db.execute('UPDATE audit SET actor="deleted" WHERE actor=?', (user_id,))
            db.execute('UPDATE jobs SET approved_by="deleted" WHERE approved_by=?', (user_id,))
            db.execute('UPDATE jobs SET intake_approved_by="deleted" WHERE intake_approved_by=?', (user_id,))

    def chat_context(self, guild_id, channel_id, user_id, profiles=True, summaries=False):
        """profiles=False is an older Fri: only this conversation's notes, as before profiles existed.

        summaries: a Fri that knows the member summary, which it alone may pass to its model and recheck.
        An older one never gets it, so it can't reach a model that wasn't told what it is.
        """
        with self.connection() as db:
            db.execute('DELETE FROM web_research WHERE created<?', (time.time()-86400,))
            db.execute('DELETE FROM web_requests WHERE created<?', (time.time()-600,))
            db.execute('DELETE FROM chat_turns WHERE at<?', (time.time() - 7 * 86400,))
            db.execute('DELETE FROM chat_turn_withdrawals WHERE at<?', (time.time() - WITHDRAWN_SECONDS,))
            profile = self._personal_context(db, guild_id, channel_id, user_id, profiles, origins=profiles)
            # Where each profile note was said, beside the notes rather than in them: an older Fri passes the
            # notes to its model whole. A newer one drops a note said where not everyone can read now (fri #98).
            origins = [note.pop('origin') for note in profile['notes']] if profiles else None
            # Before the turns: a summary that lapses as it's read retires the replies that drew on it (fri #172).
            summary = self._member_summary(db, guild_id, user_id) if summaries and profile['enabled'] else None
            revision = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user_id,)).fetchone()
            turns = [dict(row) for row in db.execute(
                'SELECT id,input,reply,source,at,response_kind,reaction,allow_closing,burst,provenance,attributed '
                'FROM chat_turns '
                'WHERE guild_id=? AND channel_id=? AND user_id=? '
                'ORDER BY id DESC LIMIT 6', (guild_id, channel_id, user_id))][::-1]
            turns = self._current_turns(db, turns)
            for turn in turns:
                turn['burst'] = json.loads(turn['burst'])
                turn['attributed'] = bool(turn['attributed'])
                source = turn.pop('source')
                recorded = turn.pop('at')
                turn['source'] = ChatSource.model_validate_json(source).evidence() if source else {
                    'kind': 'legacy_unverified', 'recorded_at': recorded}
            facts = [row[0] for row in db.execute('SELECT body FROM memories WHERE guild_id=? '
                                                  'ORDER BY created DESC LIMIT 20', (guild_id,))]
            from .fri_pending import read as read_pending
            pending = read_pending(db, guild_id, channel_id, user_id, revision[0] if revision else 0)
            return {'pending': pending, 'turns': turns, 'facts': facts, 'revision': revision[0] if revision else 0,
                    'elsewhere': self._elsewhere(db, guild_id, channel_id, user_id)
                    if profiles and profile['enabled'] else [],
                    'requests': self._own_requests(db, guild_id, channel_id, user_id) if profile['enabled'] else [],
                    'loops': self._open_loops(db, guild_id, channel_id, user_id, revision[0] if revision else 0,
                                              profile['enabled']),
                    'web_research': [json.loads(row[0]) for row in db.execute(
                        'SELECT receipt FROM web_research WHERE guild_id=? AND channel_id=? AND user_id=? ORDER BY id DESC LIMIT 3',
                        (guild_id, channel_id, user_id))],
                    'personal_memory': profile,
                    **({'profile_origins': origins} if profiles else {}),
                    **({'member_summary': summary} if summaries else {}),
                    'model': self._setting(db, 'chat_model'), 'effort': self._setting(db, 'chat_effort')}

    @staticmethod
    def _member_summary(db, guild_id, user_id):
        """With its generation, which a turn that drew on it sends back (gene #91). Read first: a withdrawal between
        the two reads leaves the turn the older one, which it then fails on."""
        from .fri_member_summary import generation, read, view
        current = generation(db, guild_id, user_id)
        summary = view(read(db, guild_id, user_id))
        return summary and {**summary, 'generation': current}

    def _elsewhere(self, db, guild_id, conversation, user_id):
        """What the member said to Fri lately in the server's other everyone-readable conversations.

        Only their own words, said where everyone could read them. Fri's replies stay out: one can
        draw on a note or exchange that belongs to its own conversation. Private ones never appear.
        Provenance stays with each one: the member's words can repeat someone else's, so a reply built
        on them inherits it and goes when that person erases.
        """
        since = time.time()-ELSEWHERE_SECONDS
        # A member keeps a few exchanges per conversation, so every candidate is read before the latest are picked.
        turns = [dict(row) for row in db.execute(
            "SELECT id,channel_id AS conversation_id,input,at,provenance,source FROM chat_turns "
            "WHERE guild_id=? AND user_id=? AND audience='server' AND channel_id LIKE 'public:%' AND channel_id<>? "
            "AND response_kind='reply' AND attributed=1 AND at>? ORDER BY id",
            (guild_id, user_id, conversation, since))]
        for turn in turns:  # When the member said it, not when the reply was saved.
            if turn['source']:
                turn['at'] = min(turn['at'], ChatSource.model_validate_json(turn['source']).timestamp().timestamp())
        turns = self._current_turns(db, [turn for turn in turns if turn['at'] > since])
        return [{key: turn[key] for key in ('conversation_id', 'input', 'at', 'provenance')}
                for turn in sorted(turns, key=lambda turn: turn['at'])[-ELSEWHERE_TURNS:]]

    @staticmethod
    def _own_requests(db, guild_id, conversation, user_id):
        """The member's open or recently settled requests, so Fri knows what they're waiting on.

        Only the requester and the owner see request text in the panel, so titles appear only in
        the member's private conversation, and only for requests known to hold just their own words.
        Open requests come first: settled ones never crowd out what the member is still waiting on.
        """
        rows = db.execute("SELECT id,kind,state,created,updated,body,provenance,attributed,source_id FROM jobs "
                          "WHERE guild_id=? AND user_id=? AND (state IN ('awaiting_owner','queued','building','review',"
                          "'approved','deploying') OR updated>?) ORDER BY state IN ('awaiting_owner','queued','building',"
                          "'review','approved','deploying') DESC, updated DESC LIMIT 5",
                          (guild_id, user_id, time.time()-7*86400))
        requests = []
        for row in rows:
            item = {key: row[key] for key in ('id', 'kind', 'state', 'created', 'updated')}
            if row['source_id'].startswith('erased:'):
                # Its text was removed after a privacy change: not the owner's decision, and no title.
                item['withdrawn'] = True
            elif conversation.startswith('private:') and row['attributed'] and row['provenance'] == '[]':
                item['title'] = clean((row['body'] or '').strip().split('\n')[0], 100)
            requests.append(item)
        return requests

    @staticmethod
    def _open_loops(db, guild_id, conversation, user_id, revision, enabled=True):
        """The member's own unfinished things that belong to this conversation, so Fri knows them without a tool.

        Each shows only where the member already sees it: reminders in the DM they were made in, saved work in
        its own conversation, and game-night cards they organized in the channel the card is in. Nothing here
        crosses into another conversation, and nothing holds another member's words or choices. A kind that
        can't show in this conversation is None, so an empty list never reads as "none anywhere". A private /fri
        panel looks like a DM here, so its reminder list is empty, and Fri, which knows a DM from a panel, nulls it.
        """
        kind, _, place = conversation.partition(':')
        reminders = [] if kind == 'private' else None
        game_nights = [] if kind == 'public' else None
        if not enabled:  # Memory off: Fri keeps none of these, and forget-me erased them.
            return {'reminders': reminders, 'saved_work': [], 'game_nights': game_nights}
        from .fri_social_store import ACTIVE, Social
        from .fri_workspace_store import Workspace
        Social.prune(db)
        Workspace.prune(db)
        now = time.time()
        if kind == 'private':
            # A settled one stays a day from when it settled: delivery stamps lease_until (two minutes on) when it
            # claims and sends, so a late send after an outage doesn't vanish the moment it goes out.
            for row in db.execute(
                    f"SELECT body,due,local_time,timezone,state,revision FROM fri_reminders WHERE guild_id=? AND user_id=? "
                    f"AND dm_id=? AND (state IN ({','.join('?'*len(ACTIVE))}) OR (state IN ('sent','failed','uncertain') "
                    f"AND max(due,lease_until)>?)) ORDER BY state IN ({','.join('?'*len(ACTIVE))}) DESC, due LIMIT ?",
                    (guild_id, user_id, place, *ACTIVE, now-LOOP_SETTLED_SECONDS, *ACTIVE, LOOP_ITEMS)):
                state = row['state']
                # Confirming and delivering both check the privacy revision: a change since then means it never goes out.
                if state in ('pending', 'scheduled', 'claimed') and row['revision'] != revision:
                    state = 'will_not_send'
                reminders.append({'text': row['body'], 'due': row['due'], 'local_time': row['local_time'],
                                  'timezone': row['timezone'], 'state': state})
        saved_work = [dict(row) for row in db.execute(
            "SELECT title,updated FROM fri_work WHERE guild_id=? AND user_id=? AND scope=? AND state='open' "
            'ORDER BY updated DESC LIMIT ?', (guild_id, user_id, conversation, LOOP_ITEMS))]
        if kind == 'public':
            for row in db.execute("SELECT id FROM fri_plans WHERE guild_id=? AND user_id=? AND channel_id=? AND state IN "
                                  "('pending','open','finalized') ORDER BY created DESC", (guild_id, user_id, place)).fetchall():
                plan = Social.plan_row(db, row['id'])
                chosen = plan['slots'][plan['selected']] if plan['state'] == 'finalized' else None
                if (chosen if chosen is not None else max(plan['slots'])) < now-LOOP_UNDERWAY_SECONDS:
                    continue  # It already happened.
                # The card's own counts, which everyone in the channel sees; never who chose what.
                game_nights.append({'title': plan['title'], 'state': plan['state'], 'timezone': plan['timezone'],
                                    'options': plan['slots'], 'available': plan['available'], 'chosen': chosen,
                                    'rsvps': plan['rsvps'] if chosen is not None else None})
        return {'reminders': reminders, 'saved_work': saved_work,
                'game_nights': None if game_nights is None else game_nights[:LOOP_ITEMS]}

    @staticmethod
    def _privacy_state(db, user_id):
        row = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user_id,)).fetchone()
        enabled = db.execute('SELECT enabled FROM personal_preferences WHERE user_id=?', (user_id,)).fetchone()
        return (row[0] if row else 0), (bool(enabled[0]) if enabled else True)

    def _checked_provenance(self, db, speaker, provenance):
        """Fail closed if any referenced member's privacy changed after the reply's context was read."""
        seen = {}
        for item in provenance:
            user, revision = str(item['user_id']), int(item['revision'])
            if user == speaker:
                continue  # The speaker's own revision is already checked for the whole turn.
            if seen.setdefault(user, revision) != revision:
                raise ValueError('Conflicting provenance revisions')
        for user, revision in seen.items():
            if self._privacy_state(db, user) != (revision, True):
                raise Conflict('Someone in that context changed their privacy settings. Ask again for a fresh reply.')
        return [{'user_id': user, 'revision': revision} for user, revision in sorted(seen.items())]

    def _current_turns(self, db, turns):
        """Drop turns derived from members whose privacy revision changed or who turned memory off."""
        kept = []
        for turn in turns:
            turn['provenance'] = json.loads(turn['provenance'] or '[]')
            if all(self._privacy_state(db, s['user_id']) == (s['revision'], True) for s in turn['provenance']):
                kept.append(turn)
            else:
                db.execute('DELETE FROM chat_turns WHERE id=?', (turn['id'],))
        for turn in kept:
            turn.pop('id')  # Provenance stays: replies built on this history inherit it.
        return kept

    def message_sources(self, guild_id, message_ids):
        """Resolve Fri's delivered messages to the members they drew on, for later references."""
        found = {}
        with self.connection() as db:
            for message_id in message_ids:
                row = db.execute('SELECT user_id,provenance,verbatim,speaker_revision FROM chat_turns WHERE guild_id=? '
                                 'AND reply_message_id=?', (guild_id, message_id)).fetchone()
                if not row or row['speaker_revision'] is None:
                    continue
                sources = json.loads(row['provenance'] or '[]') + [
                    {'user_id': row['user_id'], 'revision': row['speaker_revision']}]
                if not all(self._privacy_state(db, s['user_id']) == (s['revision'], True) for s in sources):
                    continue  # Includes the original speaker: never relabel an old reply.
                found[message_id] = {'verbatim': bool(row['verbatim']), 'provenance': sources}
        return found

    @staticmethod
    def _erase_jobs(db, rows, text, user=None):
        """Withdraw requests' content: unfinished ones are rejected, history keeps only the shell.

        A running build loses its lease, so the worker can't write its summary back afterwards.
        """
        for row in rows:
            proposal = json.loads(row['proposal']) if row['proposal'] else None
            if proposal:
                proposal['summary'] = text
                proposal.pop('repair_verification', None)
            db.execute('UPDATE jobs SET user_id=COALESCE(?,user_id),body=?,provenance="[]",updated=?,'
                       'source_url="",source_id=?,result="",proposal=?,'
                       'lease=CASE WHEN state="building" THEN NULL ELSE lease END,'
                       'progress=CASE WHEN state="building" THEN NULL ELSE progress END,'
                       'state=CASE WHEN state IN ("awaiting_owner","queued","building","review","approved") '
                       'THEN "rejected" ELSE state END WHERE id=?',
                       (user, text, time.time(), 'erased:' + row['id'], json.dumps(proposal) if proposal else None, row['id']))

    @staticmethod
    def advance_revision(db, user_id):
        """Every privacy change (erasure, memory on/off, cleared chats, edited notes) moves the revision.

        Other members' turns, notes, pending questions and requests built from this member's earlier
        words go with it, so nothing derived outlives the change.
        """
        db.execute("DELETE FROM chat_turns WHERE id IN (SELECT t.id FROM chat_turns t, json_each(t.provenance) p "
                   "WHERE json_extract(p.value, '$.user_id')=?)", (user_id,))
        db.execute("DELETE FROM chat_pending WHERE EXISTS (SELECT 1 FROM json_each(chat_pending.body, '$.provenance') p "
                   "WHERE json_extract(p.value, '$.user_id')=?)", (user_id,))
        db.execute("DELETE FROM personal_notes WHERE EXISTS (SELECT 1 FROM json_each(personal_notes.provenance) p "
                   "WHERE json_extract(p.value, '$.user_id')=?)", (user_id,))
        from .fri_reply_checks import erase as erase_reply_findings
        erase_reply_findings(db, user_id)
        from .fri_member_summary import erase as erase_summaries
        erase_summaries(db, user_id)
        Store._erase_jobs(db, db.execute("SELECT j.id,j.proposal FROM jobs j, json_each(j.provenance) p "
                                         "WHERE json_extract(p.value, '$.user_id')=?", (user_id,)).fetchall(),
                          '[Request withdrawn: someone it drew on changed their privacy settings]')
        db.execute('INSERT INTO chat_revisions VALUES (?,1) ON CONFLICT(user_id) '
                   'DO UPDATE SET revision=revision+1', (user_id,))

    def downgrade_turn_provenance(self):
        """Before starting a controller without turn-provenance, which can't erase derived state."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("DELETE FROM chat_turns WHERE provenance <> '[]'")
            db.execute("DELETE FROM chat_pending WHERE json_array_length(json_extract(body, '$.provenance')) > 0")
            self._erase_jobs(db, db.execute("SELECT id,proposal FROM jobs WHERE provenance <> '[]'").fetchall(),
                             '[Request withdrawn before a controller downgrade]')
            # The older controller writes summaries positionally.
            db.execute('ALTER TABLE agent_summaries DROP COLUMN revisions')

    @staticmethod
    def downgrade_refusal(db, channels):
        """Why a member-profile downgrade must not run, or None. Reads only, so it can run before a migration."""
        if malformed := [str(c) for c in channels if not re.fullmatch(r'[0-9]{1,25}', str(c))]:
            return 'Channels are Discord channel ids (digits only), not '+', '.join(malformed[:5])+'.'
        try:
            fenced = db.execute("SELECT 1 FROM personal_note_removals WHERE channel_id='server' LIMIT 1").fetchone()
        except sqlite3.OperationalError:  # Older than removals: nothing to keep.
            fenced = None
        if fenced and not channels:
            return ('Profile removals need the configured channels (FRI_CHANNEL_IDS and '
                    'FRI_HISTORY_CHANNEL_IDS, or --channels) to be kept after the downgrade.')
        return None

    def downgrade_member_profiles(self, channels=()):
        with self.connection() as db:
            return self.downgrade_profiles(db, channels)

    @staticmethod
    def downgrade_profiles(db, channels):
        """Before starting a controller without member profiles, which could neither show nor edit them.

        An older controller checks removal fences only in the conversation it writes to, so each profile
        removal is copied to every public conversation it could learn into: the channels given (the
        configured chat and learning channels) and every one this database knows. A removed preference
        stays removed. Without the channels a channel never learned in yet would be missed, so it refuses.
        It changes the database as it is and never migrates it, so it composes with other downgrades.
        False when the database never had member profiles.
        """
        db.execute('BEGIN IMMEDIATE')
        if refusal := Store.downgrade_refusal(db, channels):
            raise ValueError(refusal)
        if 'audience' not in {row[1] for row in db.execute('PRAGMA table_info(chat_turns)')}:
            return False
        known = {row[0] for row in db.execute(
            "SELECT channel_id FROM personal_notes UNION SELECT channel_id FROM personal_note_removals "
            "UNION SELECT channel_id FROM chat_turns UNION SELECT 'public:'||channel_id FROM agent_summaries")}
        from .fri_agent_store import AgentStore
        known |= {'public:'+str(c) for c in [*channels, *AgentStore.get(db, 'cursors', {})]}
        known = sorted(c for c in known if re.fullmatch(r'public:[0-9]{1,25}', c))
        fences = db.execute("SELECT guild_id,user_id,digest,at FROM personal_note_removals "
                            "WHERE channel_id='server'").fetchall()
        for guild, user, digest, at in fences:
            for conversation in known:
                db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) '
                           'ON CONFLICT(guild_id,channel_id,user_id,digest) '
                           'DO UPDATE SET at=max(at,excluded.at)', (guild, conversation, user, digest, at))
        db.execute("DELETE FROM personal_notes WHERE channel_id='server'")
        db.execute("DELETE FROM personal_note_removals WHERE channel_id='server'")
        # An older controller ignores the audience, so these stay in the conversation they were said in.
        db.execute("UPDATE chat_turns SET audience='conversation'")
        return True

    def check_chat(self, user_id, revision, provenance=(), *, guild_id=None, messages=(), summary_generation=None):
        """The save's privacy fence without writing: run before a reply is delivered.

        messages: the Discord messages the turn answers (its own and its burst's). One its author withdrew since stops
        the reply (gene #90). summary_generation: the member summary's the turn drew on, if it did (gene #91).
        """
        with self.connection() as db:
            if summary_generation is not None:  # A lapsed summary goes here (see _summary_unchanged), in one go.
                db.execute('BEGIN IMMEDIATE')
            if self._privacy_state(db, user_id)[0] != revision:
                raise Conflict('This conversation was cleared while Fri was replying. Start again.')
            self._checked_provenance(db, user_id, provenance)
            if self.taken_back(db, guild_id, user_id, messages):
                raise Conflict(WITHDRAWN)
            self._summary_unchanged(db, guild_id, user_id, summary_generation)
            return {'ok': True}

    @staticmethod
    def taken_back(db, guild_id, user_id, messages):
        """Whether Fri withdrew any of these messages of the member's, edited or deleted, within the week (gene #90)."""
        messages = sorted({str(message) for message in messages if message})
        return bool(messages) and bool(db.execute(
            f'SELECT 1 FROM chat_turn_withdrawals WHERE guild_id=? AND user_id=? '
            f'AND message_id IN ({",".join("?"*len(messages))})', (guild_id, user_id, *messages)).fetchone())

    @staticmethod
    def _summary_unchanged(db, guild_id, user_id, summary_generation):
        """A turn that drew on the member's summary fails closed once a withdrawal dropped it (gene #91), or once it
        lapsed: one that expired with nothing pruning it yet goes here, as a read that finds it lapsed drops it, so
        its generation moves before it's compared (gene #113). A turn that says nothing, from an older Fri or one that
        drew on none, is checked as before."""
        from .fri_member_summary import generation, lapsed
        if summary_generation is None:
            return
        if lapsed(db, guild_id, user_id):
            db.commit()  # It stays gone, and its replies retired, though the refusal undoes the rest.
            raise Conflict(SUMMARY_CHANGED)
        if generation(db, guild_id, user_id) != summary_generation:
            raise Conflict(SUMMARY_CHANGED)

    def check_authors(self, authors):
        """Attention's last privacy check before its model call (fri #84): every author it is about to send still
        allows it, at the revision their words were read under. Read under the write lock, so no erasure lands
        between two authors' reads; the refusal names nobody."""
        if not authors:
            return {'ok': True}
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for item in authors:
                if self._privacy_state(db, str(item['user_id'])) != (int(item['revision']), True):
                    raise Conflict('Someone in that conversation changed their privacy settings.')
            return {'ok': True}

    def save_chat(self, guild_id, channel_id, user_id, revision, text, reply, model, effort,
                  input_tokens=None, output_tokens=None, notes=(), source=None, web_receipt=None, clarification=None,
                  *, response_kind='reply', reaction=None, allow_closing=False, burst=(), provenance=None,
                  reply_message_id=None, verbatim=False, audience='conversation', summary_generation=None):
        # None: a legacy companion that doesn't track sources. A list, even empty, is complete attribution.
        attributed = provenance is not None
        if audience not in ('server', 'conversation') or (audience == 'server' and not channel_id.startswith('public:')):
            raise ValueError('Only an everyone-readable server conversation can reach the member profile')
        if audience == 'server' and not attributed:
            raise ValueError('A turn that reaches the member profile needs to say whose words it drew on')
        from .fri_responses import validate_response
        validate_response(response_kind, reply, reaction, notes=notes)
        if allow_closing and (response_kind != 'reply' or clarification):
            raise ValueError('Only completed text can allow a closing shortcut')
        source = ChatSource.model_validate(source).check(guild_id, channel_id, user_id) if source else None
        from .fri_conversation import checked_burst
        burst = checked_burst(burst, source, text) if burst else []
        if response_kind != 'reply' and (not source or source.kind != 'discord_message' or
                web_receipt or clarification or not -5 <= time.time()-source.timestamp().timestamp() <= 300):
            raise ValueError('Acknowledgments need a fresh message and cannot complete tools')
        if web_receipt:
            from .fri_research import ResearchReceipt
            web_receipt = ResearchReceipt.model_validate(web_receipt).model_dump()
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user_id,)).fetchone()
            if revision != (current[0] if current else 0):
                raise Conflict('This conversation was cleared while Fri was replying. Start again.')
            provenance = self._checked_provenance(db, user_id, provenance or ())
            self._summary_unchanged(db, guild_id, user_id, summary_generation)
            from .fri_pending import read as read_pending
            from .fri_pending import save as save_pending
            if response_kind != 'reply' and read_pending(db, guild_id, channel_id, user_id, revision):
                raise Conflict('An outstanding request needs a response, not an acknowledgment')
            if web_receipt:
                from .fri_research import authorized_receipt
                from .web_policy import public_query
                if not (public_query(web_receipt['query'], text) or authorized_receipt(db, guild_id,
                        channel_id, user_id, revision, source, web_receipt['query'])):
                    raise ValueError('Research has no matching public request for this turn')
            if source:
                db.execute('DELETE FROM web_requests WHERE guild_id=? AND channel_id=? AND user_id=? AND source_id=?',
                           (guild_id, channel_id, user_id, source.id))
            # Words sent before the member removed a note seen here, answered after it (a queued message),
            # can't bring it back: like memory off, the turn keeps nothing, and says so. Nor can a message of
            # theirs withdrawn before this save landed (gene #90): its exchange went, and this would be it again.
            from .fri_memory_store import predates
            first = ChatSource.model_validate(burst[0]['source']) if burst else source  # A burst's earliest.
            fenced = self.taken_back(
                db, guild_id, user_id, [*([source.id] if source else []), *(row['source']['id'] for row in burst)])
            stale = fenced or predates(db, guild_id, channel_id, user_id, first)
            # A withdrawn message's late save leaves what's pending as it is: a newer turn may have left a question.
            if response_kind == 'reply' and not fenced:
                save_pending(db, guild_id, channel_id, user_id, revision, text, source,
                             None if stale else clarification, provenance if attributed else None)
            profile = self._personal_context(db, guild_id, channel_id, user_id)
            if not profile['enabled'] or stale:
                return {'ok': True, 'first_memory': False, 'kept': False}
            if web_receipt:
                db.execute('INSERT INTO web_research(guild_id,user_id,channel_id,created,receipt) VALUES (?,?,?,?,?)',
                           (guild_id, user_id, channel_id, time.time(), json.dumps(web_receipt)))
                db.execute('DELETE FROM web_research WHERE guild_id=? AND user_id=? AND channel_id=? '
                           'AND id NOT IN (SELECT id FROM web_research WHERE guild_id=? AND user_id=? AND channel_id=? ORDER BY id DESC LIMIT 3)',
                           (guild_id, user_id, channel_id, guild_id, user_id, channel_id))
            from .fri_memory_store import SERVER, limit
            from .fri_memory_store import save as save_note
            scope = SERVER if audience == 'server' else channel_id
            first, fresh = {}, set()  # Whether each scope had no notes before this turn; those it gave a first.
            for candidate in list(notes)[:3]:
                note = personal_note(candidate, text)
                if not note:
                    continue
                # What a member is working on stays where they said it, even in a turn that reaches the profile.
                where = channel_id if note[0] in CONVERSATION_ONLY else scope
                if where not in first:
                    first[where] = not db.execute('SELECT 1 FROM personal_notes WHERE guild_id=? AND channel_id=? '
                                                  'AND user_id=?', (guild_id, where, user_id)).fetchone()
                if save_note(db, guild_id, where, user_id, note, time.time(),
                             channel_id if where == SERVER else None, provenance if attributed else None) and first[where]:
                    fresh.add(where)
            db.execute('DELETE FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? '
                       'AND id NOT IN (SELECT id FROM personal_notes WHERE guild_id=? AND channel_id=? '
                       'AND user_id=? ORDER BY at DESC LIMIT ?)',
                       (guild_id, scope, user_id, guild_id, scope, user_id, limit(scope)))
            # With the summary generation it drew on: once that moves, the reply keeps only a breadcrumb (fri #172).
            if summary_generation is not None:
                from .fri_member_summary import keep as keep_generation
                keep_generation(db, guild_id, user_id)
            db.execute('INSERT INTO chat_turns(guild_id,channel_id,user_id,input,reply,model,effort,at,'
                       'input_tokens,output_tokens,source,response_kind,reaction,allow_closing,burst,provenance,'
                       'reply_message_id,verbatim,speaker_revision,attributed,audience,summary_generation) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (guild_id, channel_id, user_id, clean(text, 4000), clean(reply, 3500), model, effort,
                        time.time(), input_tokens, output_tokens, source.model_dump_json() if source else '', response_kind,
                        reaction, allow_closing, json.dumps(burst), json.dumps(provenance), reply_message_id,
                        bool(verbatim), revision, attributed, audience, summary_generation))
            db.execute('DELETE FROM chat_turns WHERE guild_id=? AND channel_id=? AND user_id=? '
                       'AND id NOT IN (SELECT id FROM chat_turns WHERE guild_id=? AND channel_id=? '
                       'AND user_id=? ORDER BY id DESC LIMIT 6)',
                       (guild_id, channel_id, user_id, guild_id, channel_id, user_id))
            shown = SERVER if SERVER in fresh else channel_id if fresh else scope  # The notice names the wider.
            return {'ok': True, 'first_memory': bool(fresh),
                    'memory_scope': 'server' if shown == SERVER else 'conversation'}

    @staticmethod
    def _personal_context(db, guild_id, channel_id, user_id, profiles=True, origins=False):
        """The member's profile and this conversation's own notes: never another conversation's.

        An older Fri (profiles=False) gets only this conversation's notes, within the bounds it was built for.
        origins: each note also says where a profile note was said (see chat_context), for Fri to recheck.
        """
        from .fri_memory_store import HORIZON, SERVER, fences, limit
        db.execute('DELETE FROM personal_notes WHERE at<?', (time.time() - TTL,))
        # A removal that holds no words lasts as long as archive search reaches back, so nothing said
        # before it is learned again; older per-phrase fences go with the notes they fenced.
        wide = fences()
        db.execute(f'DELETE FROM personal_note_removals WHERE at<? AND (at<? OR digest NOT IN ({",".join("?"*len(wide))}))',
                   (time.time() - TTL, time.time() - HORIZON, *wide))
        # A note learned while other members' words were in view goes when any of them changes their privacy.
        for note_id, provenance in db.execute("SELECT id,provenance FROM personal_notes WHERE guild_id=? AND "
                                              "user_id=? AND provenance<>'[]'", (guild_id, user_id)).fetchall():
            if any(Store._privacy_state(db, s['user_id']) != (s['revision'], True) for s in json.loads(provenance)):
                db.execute('DELETE FROM personal_notes WHERE id=?', (note_id,))
        row = db.execute('SELECT enabled FROM personal_preferences WHERE user_id=?', (user_id,)).fetchone()
        # With profiles, each note says whose words were in view when it was learned: a reply built on it
        # inherits them, as a reply built on an exchange does. An older Fri can't carry that, so it gets
        # only the notes that drew on no one else, or that it can't tell, as it always has. A note of unknown
        # provenance (saved before profiles, or by a turn that didn't say) is a conversation note: it shows
        # only where it was said, and so does a reply built on it, as before.
        return {'enabled': bool(row[0]) if row else True,
                'notes': [{'category': r['category'], 'evidence': r['evidence'],
                           'scope': 'server' if scope == SERVER else 'here',
                           **({'provenance': json.loads(r['provenance'] or '[]')} if profiles else {}),
                           **({'origin': r['origin'] if scope == SERVER else None} if origins else {})}
                          for scope in ((SERVER, channel_id) if profiles else (channel_id,)) for r in db.execute(
                              'SELECT category,evidence,provenance,origin FROM personal_notes WHERE guild_id=? AND '
                              "channel_id=? AND user_id=? AND (? OR provenance IS NULL OR provenance='[]') "
                              'ORDER BY at DESC LIMIT ?', (guild_id, scope, user_id, profiles, limit(scope)))]}

    def personal_control(self, user_id, action):
        if action not in ('disable', 'enable'):
            raise ValueError('Invalid memory control')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO personal_preferences VALUES (?,?) ON CONFLICT(user_id) '
                       'DO UPDATE SET enabled=excluded.enabled', (user_id, action == 'enable'))
            if action == 'disable':
                from .fri_explore_store import erase
                erase(db, user_id)
                from .fri_workspace_store import erase as erase_workspace
                erase_workspace(db, user_id)
                from .fri_social_store import erase as erase_social
                erase_social(db, user_id)
                db.execute('DELETE FROM web_research WHERE user_id=?', (user_id,))
                db.execute('DELETE FROM personal_notes WHERE user_id=?', (user_id,))
                db.execute('DELETE FROM personal_note_removals WHERE user_id=?', (user_id,))
                db.execute('DELETE FROM chat_turn_withdrawals WHERE user_id=?', (user_id,))
                db.execute('DELETE FROM chat_turns WHERE user_id=?', (user_id,))
                db.execute('DELETE FROM chat_pending WHERE user_id=?', (user_id,))
                db.execute('DELETE FROM web_requests WHERE user_id=?', (user_id,))
                db.execute("UPDATE gene_controls SET user_id='deleted',payload='{}',state='cancelled',result='{}' WHERE user_id=?", (user_id,))
                for row in db.execute('SELECT channel_id,authors FROM agent_summaries').fetchall():
                    if user_id in json.loads(row['authors']):
                        db.execute('DELETE FROM agent_summaries WHERE channel_id=?', (row['channel_id'],))
                # Reports can derive from several people. Remove pending/retained copies
                # conservatively; already delivered Discord messages are separate.
                db.execute('DELETE FROM agent_reports')
            # Invalidate even an in-flight reply that began before memory was toggled.
            self.advance_revision(db, user_id)

    def clear_chats(self, user_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM web_research WHERE user_id=?', (user_id,))
            db.execute('DELETE FROM chat_turns WHERE user_id=?', (user_id,))
            for row in db.execute('SELECT channel_id,authors FROM agent_summaries').fetchall():
                if user_id in json.loads(row['authors']):
                    db.execute('DELETE FROM agent_summaries WHERE channel_id=?', (row['channel_id'],))
            # Learning reports can quote several members; remove retained and queued copies, as disable does.
            db.execute('DELETE FROM agent_reports')
            db.execute('DELETE FROM chat_pending WHERE user_id=?', (user_id,))
            db.execute('DELETE FROM web_requests WHERE user_id=?', (user_id,))
            db.execute("UPDATE gene_controls SET user_id='deleted',payload='{}',state='cancelled',result='{}' WHERE user_id=?", (user_id,))
            self.advance_revision(db, user_id)

    def withdraw_turn(self, guild_id, conversation_id, user_id, message_id):
        """A member's message edited or deleted after Fri saved their exchange with it (fri #129): the exchanges
        holding it go, as erasure removes them, so its old words don't come back as history. Fri's reply goes too,
        since it may repeat them. Only that member's, in that conversation, and a repeat finds nothing. Not a
        privacy change: their revision stays, so replies to their other messages still save. Their summary goes if
        it may have read the message (gene #83). Other members' replies and summaries that drew on it stay (fri #101).
        A fence holding only the message's id keeps a save still on its way from bringing it back (gene #90)."""
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            now = time.time()
            db.execute('DELETE FROM chat_turn_withdrawals WHERE at<?', (now-WITHDRAWN_SECONDS,))
            db.execute('INSERT INTO chat_turn_withdrawals VALUES (?,?,?,?) ON CONFLICT(guild_id,user_id,message_id) '
                       'DO UPDATE SET at=excluded.at', (guild_id, user_id, message_id, now))
            turns = db.execute(
                "SELECT id,json_extract(NULLIF(source,''),'$.id') AS source_id,at FROM chat_turns WHERE guild_id=? "
                "AND channel_id=? AND user_id=? AND (json_extract(NULLIF(source,''),'$.id')=? OR EXISTS (SELECT 1 "
                "FROM json_each(chat_turns.burst) b WHERE json_extract(b.value,'$.source.id')=?))",
                (guild_id, conversation_id, user_id, message_id, message_id)).fetchall()
            db.executemany('DELETE FROM chat_turns WHERE id=?', [(turn['id'],) for turn in turns])
            # A question it left open, and the research it permitted, go with it. A burst's are its final message's
            # (gene #84).
            sources = sorted({message_id, *(turn['source_id'] for turn in turns if turn['source_id'])})
            marks = ','.join('?'*len(sources))
            db.execute(f'DELETE FROM chat_pending WHERE guild_id=? AND conversation_id=? AND user_id=? '
                       f'AND source_id IN ({marks})', (guild_id, conversation_id, user_id, *sources))
            db.execute(f'DELETE FROM web_requests WHERE guild_id=? AND channel_id=? AND user_id=? '
                       f'AND source_id IN ({marks})', (guild_id, conversation_id, user_id, *sources))
            from .fri_member_summary import withdrawn
            withdrawn(db, guild_id, user_id, conversation_id, message_id, [turn['at'] for turn in turns])
            return {'ok': True, 'removed': len(turns)}

    def chat_models(self):
        with self.connection() as db:
            return {'selected': self._setting(db, 'chat_model'),
                    'effort': self._setting(db, 'chat_effort'),
                    'models': [row[0] for row in db.execute('SELECT id FROM chat_models ORDER BY id')]}

    def update_chat_models(self, models):
        if not models or len(models) > 500 or any(not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', m) for m in models):
            raise ValueError('Invalid subscription model catalog; keeping the previous list')
        with self.connection() as db:
            db.execute('DELETE FROM chat_models')
            db.executemany('INSERT INTO chat_models VALUES (?)', [(m,) for m in sorted(set(models))])

    def select_chat_model(self, model, effort, actor):
        if effort not in ('low', 'medium', 'high', 'xhigh', 'max'):
            raise ValueError('Choose a supported reasoning effort')
        with self.connection() as db:
            if not db.execute('SELECT 1 FROM chat_models WHERE id=?', (model,)).fetchone():
                raise Conflict('Refresh the subscription model list and choose an available model')
            db.execute('UPDATE settings SET value=? WHERE key="chat_model"', (model,))
            db.execute('UPDATE settings SET value=? WHERE key="chat_effort"', (effort,))
            self._audit(db, actor, 'chat_model_selected', model, effort)

    def backup(self, destination: Path):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db, sqlite3.connect(destination) as backup:
            db.backup(backup)
        destination.chmod(0o600)

    def model_state(self):
        with self.connection() as db:
            models = [{**dict(r), 'efforts': json.loads(r['efforts'])} for r in db.execute('SELECT * FROM models')]
            selected = self._setting(db, 'code_model')
            return {'selected': selected, 'effort': self._setting(db, 'code_effort') or 'model default',
                    'models': models, 'selected_available': any(m['id'] == selected for m in models),
                    'refreshed_at': float(self._setting(db, 'models_refreshed_at')),
                    'refresh_pending': float(self._setting(db, 'models_requested_at')) >
                                       float(self._setting(db, 'models_refreshed_at')),
                    'error': self._setting(db, 'models_error')}

    def request_model_refresh(self, actor):
        with self.connection() as db:
            db.execute('UPDATE settings SET value=? WHERE key="models_requested_at"', (str(time.time()),))
            self._audit(db, actor, 'models_refresh_requested')

    def update_models(self, models, error=''):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            if error:
                db.execute('UPDATE settings SET value=? WHERE key="models_error"', (clean(error, 500),))
                return
            if not models:
                raise ValueError('The model catalog is empty; keeping the previous list')
            for model in models:
                if not re.fullmatch(r'[A-Za-z0-9_.:/-]{1,100}', model.get('id', '')):
                    raise ValueError('Invalid model ID in catalog')
            db.execute('DELETE FROM models')
            for model in models:
                db.execute('INSERT INTO models VALUES (?,?,?,?)',
                           (model['id'], clean(model['name'], 100), json.dumps(model['efforts']),
                            model['default_effort']))
            db.execute('UPDATE settings SET value=? WHERE key="models_refreshed_at"', (str(time.time()),))
            db.execute('UPDATE settings SET value="" WHERE key="models_error"')
            self._audit(db, 'worker', 'models_refreshed', detail=f'{len(models)} available models')

    def select_model(self, model_id, effort, actor):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            model = db.execute('SELECT * FROM models WHERE id=?', (model_id,)).fetchone()
            if not model:
                raise Conflict('Model is not in the available catalog. Refresh the list first')
            if effort and effort not in json.loads(model['efforts']):
                raise ValueError('That reasoning effort is not supported by this model')
            db.execute('UPDATE settings SET value=? WHERE key="code_model"', (model_id,))
            db.execute('UPDATE settings SET value=? WHERE key="code_effort"', (effort,))
            self._audit(db, actor, 'model_selected', model_id, effort or 'model default')
