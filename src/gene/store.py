from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .fri_agent_store import initialize
from .fri_memory import MAX_NOTES, TTL, personal_note
from .privacy import clean
from .reviews import pending_notice, pending_review, request_reference

SHA = re.compile(r'^[a-f0-9]{40}$')


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            initialize(db)
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
            ''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(jobs)')}
            for name in ('model', 'effort', 'intake_approved_by', 'intake_approved_ref', 'progress'):
                if name not in columns:
                    db.execute(f'ALTER TABLE jobs ADD COLUMN {name} TEXT')
            # Existing queued requests have never received intake approval. Fail closed
            # during migration; completed/running jobs and release approvals retain their history.
            db.execute('UPDATE jobs SET state="awaiting_owner" WHERE state="queued" '
                       'AND intake_approved_ref IS NULL')
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
            db.execute('DELETE FROM chat_turns WHERE at<?', (time.time() - 7 * 86400,))
            db.execute('DELETE FROM personal_notes WHERE at<?', (time.time() - TTL,))
            return {
                'paused': self._setting(db, 'paused') == 'true',
                'active_sha': self._setting(db, 'active_sha'),
                'previous_sha': self._setting(db, 'previous_sha'),
                'model': self._setting(db, 'code_model'),
                'effort': self._setting(db, 'code_effort') or 'model default',
                'chat_model': self._setting(db, 'chat_model'),
                'chat_effort': self._setting(db, 'chat_effort'),
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

    def request(self, guild_id: str, user_id: str, source_id: str, source_url: str, body: str):
        body = clean(body)
        if len(body) < 5:
            raise ValueError('Describe the request in at least five characters')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT * FROM jobs WHERE source_id=?', (source_id,)).fetchone()
            if existing:
                return self._job(existing)
            pending = db.execute('SELECT count(*) FROM jobs WHERE user_id=? AND state IN '
                                 '("awaiting_owner","queued","building","review","approved","deploying")',
                                 (user_id,)).fetchone()[0]
            if pending >= 3:
                raise Conflict('You already have three open requests; finish one first')
            job_id = uuid.uuid4().hex[:12]
            now = time.time()
            db.execute('INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,created,updated,state) '
                       'VALUES (?,?,?,?,?,?,?,?,"awaiting_owner")',
                       (job_id, guild_id, user_id, source_id, source_url, body, now, now))
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
            rows = db.execute('SELECT * FROM jobs WHERE state IN ("awaiting_owner","review") OR (updated>? '
                              'AND (intake_approved_ref IS NOT NULL OR kind="rollback")) '
                              'ORDER BY updated', (now - 7 * 86400,)).fetchall()
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

    def recent(self):
        with self.connection() as db:
            return [self._job(r) for r in db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT 20')]

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
            if not row['intake_approved_by'] or row['intake_approved_ref'] != request_reference(dict(row)):
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
            self._leased(db, job_id, lease)
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
            db.execute('UPDATE jobs SET state=?,proposal=?,result=?,lease=NULL,updated=? WHERE id=?',
                       (state, json.dumps(proposal) if proposal else None, clean(error), time.time(), job_id))
            self._audit(db, 'worker', state, job_id)

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
            for row in rows:
                job = self._job(row)
                proposal = job['proposal']
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

    def finish_deploy(self, job_id, lease, success, detail):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            row = self._leased(db, job_id, lease, 'deploying')
            proposal = json.loads(row['proposal'])
            if success:
                db.execute('UPDATE settings SET value=? WHERE key="previous_sha"', (proposal['base_sha'],))
                db.execute('UPDATE settings SET value=? WHERE key="active_sha"', (proposal['sha'],))
            state = 'deployed' if success else 'failed'
            db.execute('UPDATE jobs SET state=?,result=?,lease=NULL,updated=? WHERE id=?',
                       (state, clean(detail), time.time(), job_id))
            db.execute('UPDATE deployments SET state=? WHERE job_id=?', (state, job_id))
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
            rows = db.execute('SELECT id,proposal FROM jobs WHERE user_id=?', (user_id,)).fetchall()
            for row in rows:
                proposal = json.loads(row['proposal']) if row['proposal'] else None
                if proposal:
                    proposal['summary'] = '[User request erased]'
                db.execute('UPDATE jobs SET user_id="deleted",body="[User request erased]",'
                           'source_url="",source_id=?,result="",proposal=?,'
                           'state=CASE WHEN state IN ("awaiting_owner","queued","review","approved") THEN "rejected" '
                           'ELSE state END WHERE id=?',
                           ('erased:' + row['id'], json.dumps(proposal) if proposal else None, row['id']))
            db.execute('DELETE FROM memories WHERE approved_by=?', (user_id,))
            db.execute('UPDATE audit SET actor="deleted" WHERE actor=?', (user_id,))
            db.execute('UPDATE jobs SET approved_by="deleted" WHERE approved_by=?', (user_id,))
            db.execute('UPDATE jobs SET intake_approved_by="deleted" WHERE intake_approved_by=?', (user_id,))

    def chat_context(self, guild_id, channel_id, user_id):
        with self.connection() as db:
            db.execute('DELETE FROM chat_turns WHERE at<?', (time.time() - 7 * 86400,))
            profile = self._personal_context(db, guild_id, channel_id, user_id)
            revision = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user_id,)).fetchone()
            turns = [dict(row) for row in db.execute(
                'SELECT input,reply FROM chat_turns WHERE guild_id=? AND channel_id=? AND user_id=? '
                'ORDER BY id DESC LIMIT 6', (guild_id, channel_id, user_id))][::-1]
            facts = [row[0] for row in db.execute('SELECT body FROM memories WHERE guild_id=? '
                                                  'ORDER BY created DESC LIMIT 20', (guild_id,))]
            return {'turns': turns, 'facts': facts, 'revision': revision[0] if revision else 0,
                    'personal_memory': profile,
                    'model': self._setting(db, 'chat_model'), 'effort': self._setting(db, 'chat_effort')}

    def save_chat(self, guild_id, channel_id, user_id, revision, text, reply, model, effort,
                  input_tokens=None, output_tokens=None, notes=()):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT revision FROM chat_revisions WHERE user_id=?', (user_id,)).fetchone()
            if revision != (current[0] if current else 0):
                raise Conflict('This conversation was cleared while Fri was replying. Start again.')
            profile = self._personal_context(db, guild_id, channel_id, user_id)
            if not profile['enabled']:
                return {'ok': True, 'first_memory': False}
            saved = 0
            for candidate in list(notes)[:3]:
                note = personal_note(candidate, text)
                if note:
                    db.execute('INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at) '
                               'VALUES (?,?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id,category) '
                               'DO UPDATE SET evidence=excluded.evidence,at=excluded.at',
                               (guild_id, channel_id, user_id, *note, time.time()))
                    saved += 1
            db.execute('DELETE FROM personal_notes WHERE guild_id=? AND channel_id=? AND user_id=? '
                       'AND id NOT IN (SELECT id FROM personal_notes WHERE guild_id=? AND channel_id=? '
                       'AND user_id=? ORDER BY at DESC LIMIT ?)',
                       (guild_id, channel_id, user_id, guild_id, channel_id, user_id, MAX_NOTES))
            db.execute('INSERT INTO chat_turns(guild_id,channel_id,user_id,input,reply,model,effort,at,'
                       'input_tokens,output_tokens) VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (guild_id, channel_id, user_id, clean(text, 4000), clean(reply, 3500), model, effort,
                        time.time(), input_tokens, output_tokens))
            db.execute('DELETE FROM chat_turns WHERE guild_id=? AND channel_id=? AND user_id=? '
                       'AND id NOT IN (SELECT id FROM chat_turns WHERE guild_id=? AND channel_id=? '
                       'AND user_id=? ORDER BY id DESC LIMIT 6)',
                       (guild_id, channel_id, user_id, guild_id, channel_id, user_id))
            return {'ok': True, 'first_memory': bool(saved and not profile['notes'])}

    @staticmethod
    def _personal_context(db, guild_id, channel_id, user_id):
        db.execute('DELETE FROM personal_notes WHERE at<?', (time.time() - TTL,))
        row = db.execute('SELECT enabled FROM personal_preferences WHERE user_id=?', (user_id,)).fetchone()
        return {'enabled': bool(row[0]) if row else True,
                'notes': [dict(r) for r in db.execute('SELECT category,evidence FROM personal_notes '
                    'WHERE guild_id=? AND channel_id=? AND user_id=? ORDER BY at DESC LIMIT ?',
                    (guild_id, channel_id, user_id, MAX_NOTES))]}

    def personal_control(self, user_id, action):
        if action not in ('disable', 'enable'):
            raise ValueError('Invalid memory control')
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('INSERT INTO personal_preferences VALUES (?,?) ON CONFLICT(user_id) '
                       'DO UPDATE SET enabled=excluded.enabled', (user_id, action == 'enable'))
            if action == 'disable':
                db.execute('DELETE FROM personal_notes WHERE user_id=?', (user_id,))
                db.execute('DELETE FROM chat_turns WHERE user_id=?', (user_id,))
                for row in db.execute('SELECT channel_id,authors FROM agent_summaries').fetchall():
                    if user_id in json.loads(row['authors']):
                        db.execute('DELETE FROM agent_summaries WHERE channel_id=?', (row['channel_id'],))
                # Reports can derive from several people. Remove pending/retained copies
                # conservatively; already delivered Discord messages are separate.
                db.execute('DELETE FROM agent_reports')
            # Invalidate even an in-flight reply that began before memory was toggled.
            db.execute('INSERT INTO chat_revisions VALUES (?,1) ON CONFLICT(user_id) '
                       'DO UPDATE SET revision=revision+1', (user_id,))

    def clear_chats(self, user_id):
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM chat_turns WHERE user_id=?', (user_id,))
            db.execute('INSERT INTO chat_revisions VALUES (?,1) ON CONFLICT(user_id) '
                       'DO UPDATE SET revision=revision+1', (user_id,))

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
