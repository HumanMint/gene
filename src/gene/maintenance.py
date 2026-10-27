"""The production maintenance window and the production-action log (owner policy, 2026-10-27).

Production changes start only in [04:00, 05:00) America/Los_Angeles, following daylight saving.
Rollback and restoring healthy service run at any hour. An unknown clock or zone counts as closed.
One production batch runs per local day across both routes (owner policy, 2026-10-27): the maintainer
batch, which counts once it drains, or one approved automatic feature or repair deployment, which counts
once claimed, whichever comes first. A critical fix (P0: a crash, data loss, a privacy leak, a broken
rollback) is exempt from that cap, never from the window. An unknown clock, zone or history counts as used.
ops/maintainer/batch-release.sh carries its own copy of the window and the cap, since it runs without this package.
"""

import argparse
import datetime as dt
import fcntl
import json
import math
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

ZONE = 'America/Los_Angeles'
OPENS, CLOSES = dt.time(4), dt.time(5)
# Automatic deployments are claimed only this long before the close, so a switch never starts after it.
CLAIM_MARGIN = dt.timedelta(minutes=10)
EARLIEST = 1793059200  # 2026-10-27T00:00Z: a clock before the policy existed is not trusted
LOG_MAX = 1 << 20  # bytes; past this the log keeps its newest half
DRAINED = 'batch_drained'  # The audit action batch-release.sh's drain records in the controller DB.
clock = time.time


def local(now=None):
    """The wall-clock time in the window's zone, or None when the clock or the zone is unknown."""
    try:
        from zoneinfo import ZoneInfo
        stamp = float(clock() if now is None else now)
        if not math.isfinite(stamp) or stamp < EARLIEST:
            return None
        return dt.datetime.fromtimestamp(stamp, ZoneInfo(ZONE))
    except Exception:  # A missing zone database, an unreadable clock: closed, never open.
        return None


def is_open(now=None, margin=dt.timedelta(0)):
    """True only inside the window, ending margin before the close."""
    moment = local(now)
    if moment is None:
        return False
    closes = (dt.datetime.combine(dt.date(2000, 1, 1), CLOSES) - margin).time()
    return OPENS <= moment.time() < closes


def day_start(now=None):
    """Today's local midnight in the window's zone, as epoch seconds, or None when the clock or zone is unknown."""
    moment = local(now)
    if moment is None:
        return None
    return dt.datetime.combine(moment.date(), dt.time(0), tzinfo=moment.tzinfo).timestamp()


def initialize(db):
    """Critical marks, apart from the jobs table, so a controller without the cap ignores them."""
    db.execute('CREATE TABLE IF NOT EXISTS critical_fixes (job_id TEXT PRIMARY KEY, sha TEXT NOT NULL, '
               'reason TEXT NOT NULL, actor TEXT NOT NULL, at REAL NOT NULL)')


def batch_used(db, job, now=None):
    """True when an approved feature or repair job has to wait for tomorrow: today's batch is used, or unknown.

    A job marked critical for the commit it was approved at never waits for the cap. Rollbacks never ask.
    """
    if db.execute('SELECT 1 FROM critical_fixes WHERE job_id=? AND sha=?',
                  (job['id'], job['approved_sha'])).fetchone():
        return False
    since = day_start(now)
    if since is None:
        return True
    # A deployment counts once claimed, whatever its outcome. Anything dated later than now counts too,
    # since the clock went back, and so does one whose job is gone.
    if db.execute("SELECT 1 FROM deployments d LEFT JOIN jobs j ON j.id=d.job_id "
                  "WHERE d.at>=? AND j.kind IS NOT 'rollback'", (since,)).fetchone():
        return True
    return db.execute('SELECT 1 FROM audit WHERE action=? AND at>=?', (DRAINED, since)).fetchone() is not None


def critical_reason(text):
    """The reason as stored and logged: one line, bounded, with anything secret-shaped redacted."""
    from .privacy import clean
    return clean(' '.join(str(text).split()), 500)


def mark_critical(path, job_id, reason, actor):
    """Exempt an approved feature or repair job from the daily cap, for the commit it was approved at.

    Its deployment still waits for the window. Returns that commit.
    """
    reason = critical_reason(reason)
    if not reason:
        raise ValueError('A critical fix needs its P0 reason: a crash, data loss, a privacy leak or a broken rollback')
    # mode=rw: a wrong path is an error, never a new empty database.
    with closing(sqlite3.connect(Path(path).absolute().as_uri()+'?mode=rw', uri=True, timeout=30,
                                 isolation_level=None)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            row = db.execute('SELECT kind,state,approved_sha FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not row or row[0] not in ('feature', 'repair') or row[1] != 'approved' or not row[2]:
                raise ValueError('Only an approved feature or repair job can be marked critical')
            initialize(db)
            db.execute('INSERT INTO critical_fixes VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET '
                       'sha=excluded.sha,reason=excluded.reason,actor=excluded.actor,at=excluded.at',
                       (job_id, row[2], reason, actor, clock()))
            db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
                       (clock(), actor, 'critical_fix', job_id, f'{row[2]}: {reason}'))
            db.execute('COMMIT')
        except BaseException:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
    return row[2]


def critical_marks(path):
    """Every critical mark with its job's kind and state; never the job's request text."""
    with closing(sqlite3.connect(Path(path).absolute().as_uri()+'?mode=ro', uri=True, timeout=30)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='critical_fixes'").fetchone():
            return []
        rows = db.execute('SELECT c.job_id,j.kind,j.state,c.sha,j.approved_sha,c.reason,c.actor,c.at '
                          'FROM critical_fixes c LEFT JOIN jobs j ON j.id=c.job_id ORDER BY c.at').fetchall()
    marks = []
    for job_id, kind, state, sha, approved_sha, reason, actor, stamp in rows:
        moment = local(stamp)
        marks.append({'job_id': job_id, 'kind': kind, 'state': state, 'sha': sha,
                      'current': state == 'approved' and sha == approved_sha, 'reason': reason, 'actor': actor,
                      'marked_at': moment.isoformat() if moment else stamp})
    return marks


def record(path, action, *, actor, trigger, reason, outcome, before=None, after=None,
           started=None, restart_s=None, exception=None, **extra):
    """Append one production action. Never put secrets or member content in any field."""
    now = clock()
    moment = local(now)
    row = {'at': moment.isoformat() if moment else dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
           'tz': ZONE if moment else 'UTC (zone unavailable)', 'actor': actor, 'trigger': trigger,
           'action': action, 'reason': reason, 'before': before, 'after': after,
           'duration_s': None if started is None else round(now-started, 1),
           'restart_s': None if restart_s is None else round(restart_s, 1),
           'outcome': outcome, 'exception': exception or None} | extra
    append(Path(path), json.dumps(row, sort_keys=True))


def append(path, line):
    """One line, under the log's lock; past LOG_MAX the oldest lines go, so the log stays bounded."""
    while True:
        with open(path, 'a') as log:
            os.fchmod(log.fileno(), 0o600)
            fcntl.flock(log, fcntl.LOCK_EX)
            try:
                if os.fstat(log.fileno()).st_ino != os.stat(path).st_ino:
                    continue  # Trimmed meanwhile: write to the file that is there now.
            except FileNotFoundError:
                continue
            log.write(line+'\n')
            log.flush()
            if log.tell() > LOG_MAX:
                trim(path)
            return


def trim(path):
    lines = Path(path).read_bytes().splitlines(keepends=True)
    kept, size = [], 0
    for line in reversed(lines):
        if size+len(line) > LOG_MAX // 2:
            break
        kept.append(line)
        size += len(line)
    temporary = path.with_name(path.name+'.tmp')
    with open(temporary, 'wb') as out:
        os.fchmod(out.fileno(), 0o600)
        out.writelines(reversed(kept))
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m gene.maintenance',
                                     description='Critical fixes: exempt from the daily batch cap, never from the window.')
    parser.add_argument('--root', type=Path, default=Path(os.getenv('GENE_ROOT', '/srv/gene')),
                        help="Gene's host root, with the controller DB and the production-action log")
    commands = parser.add_subparsers(dest='command', required=True)
    mark = commands.add_parser('critical', help="mark an approved feature or repair job a critical fix (P0 only)")
    mark.add_argument('job_id')
    mark.add_argument('--reason', required=True, help='the P0 problem it fixes: crash, data loss, privacy leak, broken rollback')
    commands.add_parser('critical-list', help='the critical marks, read-only')
    args = parser.parse_args(argv)
    database = args.root/'data/controller/gene.sqlite3'
    if args.command == 'critical-list':
        try:
            print(json.dumps(critical_marks(database), indent=2))
        except sqlite3.Error as exc:
            raise SystemExit(f'Cannot read {database}: {exc}') from None
        return
    actor = os.environ.get('SUDO_USER') or 'root'
    log = dict(actor=actor, trigger='gene.maintenance critical', reason=f'mark job {args.job_id} a critical fix',
               exception=critical_reason(args.reason))
    try:
        sha = mark_critical(database, args.job_id, args.reason, 'maintainer:'+actor)
    except (ValueError, sqlite3.Error) as exc:
        try:
            record(args.root/'production-actions.jsonl', f'mark critical fix job {args.job_id}',
                   outcome=f'refused: {exc}', **log)
        except OSError as missing:
            print(f'WARN: the production-action log was not written ({type(missing).__name__})', file=sys.stderr)
        raise SystemExit(f'Not marked: {exc}') from None
    try:
        record(args.root/'production-actions.jsonl', f'mark critical fix job {args.job_id}', outcome='ok',
               after={'critical': sha}, **log)
    except OSError as exc:
        raise SystemExit(f'Marked {args.job_id} critical at {sha}, but the production-action log was not written '
                         f'({type(exc).__name__})') from None
    print(json.dumps({'job_id': args.job_id, 'sha': sha, 'critical': True}))


if __name__ == '__main__':
    main()
