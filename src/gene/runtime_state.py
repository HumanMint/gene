"""Whether the controller knows which Gene release is running (gene #48).

A deployment that stopped Red and then failed, with its restore never proving healthy either, leaves the
runtime unknown: active_sha still names the last release a health check proved, but nothing says it is
what's running. Until a maintainer reconciles it, or a later deployment proves a release healthy, automatic
releases wait and nothing tells a member "fixed now". An owner's rollback can still go out to restore
service, under the deployer's usual backup, health check and restore.

Maintainers, on the host, once the live runtime is checked by hand:

    python -m gene.runtime_state status <database>
    python -m gene.runtime_state clear <database> --sha <active_sha> --actor maintainer:<name> \\
        --reason '<what was checked>' --health /srv/gene/data/red/gene-health.json \\
        [--log /srv/gene/production-actions.jsonl]

Clearing needs the release the controller records as live, running with a fresh healthy heartbeat.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path

from . import maintenance
from .privacy import clean
from .store import SHA, Conflict, Store

KEY = 'runtime_unknown'
ACTOR = re.compile(r'^maintainer:[A-Za-z0-9_.-]{1,40}$')
FRESH = 25  # seconds; the deployer's own health check uses the same bound


def initialize(db):
    db.execute("INSERT OR IGNORE INTO settings VALUES (?, '')", (KEY,))


def current(db):
    """The deployment that left the runtime unknown, or None while the controller knows it."""
    row = db.execute('SELECT value FROM settings WHERE key=?', (KEY,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def mark(db, job, proposal):
    db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps({
        'job_id': job['id'], 'kind': job['kind'], 'sha': proposal['sha'], 'restore_sha': proposal['base_sha'],
        'since': time.time()}, sort_keys=True), KEY))
    Store._audit(db, 'deployer', KEY, job['id'],
                 f"Switching to {proposal['sha']} failed and restoring {proposal['base_sha']} never proved healthy")


def late(db, job_id):
    """A replayed receipt's evidence that its deployment left the runtime unknown, which the controller that
    committed the receipt ignored (one from before this). It counts once, and only while nothing has proved a
    release since that deployment was claimed: no later claim, no maintainer reconciliation, and active_sha still
    the release it tried to restore. It never replays the deployment."""
    if db.execute('SELECT 1 FROM audit WHERE action=? AND target=?', (KEY, job_id)).fetchone():
        return  # Already recorded.
    job = db.execute('SELECT j.*, d.at AS claimed FROM jobs j JOIN deployments d ON d.job_id=j.id WHERE j.id=?',
                     (job_id,)).fetchone()
    proposal = json.loads(job['proposal'])
    active = db.execute("SELECT value FROM settings WHERE key='active_sha'").fetchone()[0]
    if (active != proposal['base_sha']
            or db.execute('SELECT 1 FROM deployments WHERE at>?', (job['claimed'],)).fetchone()
            or db.execute("SELECT 1 FROM audit WHERE action='runtime_reconciled' AND at>?",
                          (job['claimed'],)).fetchone()):
        return
    mark(db, job, proposal)


def known(db):
    db.execute("UPDATE settings SET value='' WHERE key=?", (KEY,))


def reconcile(db, sha, actor, reason):
    """A maintainer found the release the controller records as live running and healthy."""
    if not SHA.fullmatch(sha):
        raise ValueError('Name the full SHA of the release that is running')
    if not ACTOR.fullmatch(actor):
        raise ValueError('Name the maintainer as maintainer:<name>')
    reason = clean(reason, 500).strip()
    if not reason:
        raise ValueError('Say what was checked')
    db.execute('BEGIN IMMEDIATE')
    state = current(db)
    if state is None:
        raise Conflict('The controller already knows which release is running; nothing to clear')
    if db.execute("SELECT 1 FROM jobs WHERE state='deploying'").fetchone():
        raise Conflict('A deployment is going out now; check the runtime again once it lands')
    active = db.execute("SELECT value FROM settings WHERE key='active_sha'").fetchone()[0]
    if sha != active:
        raise Conflict(f'The controller records {active} as live. Restore that release, or record the '
                       'running one through a maintainer release, before clearing')
    known(db)
    Store._audit(db, actor, 'runtime_reconciled', sha, f"{reason} (unknown since job {state['job_id']})")
    return state


def heartbeat(path, sha):
    """The same evidence the deployer accepts, read once: this release, connected, controls running, fresh."""
    try:
        report = json.loads(Path(path).read_text())
        if (report['sha'] == sha and report['guild_connected'] is True and report.get('controls_running') is True
                and time.time()-float(report['at']) < FRESH):
            return
    except (OSError, ValueError, KeyError, TypeError):
        pass
    raise ValueError(f'No fresh healthy Gene heartbeat for {sha} in {path}')


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m gene.runtime_state')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status').add_argument('database')
    clear = commands.add_parser('clear')
    clear.add_argument('database')
    for name in ('--sha', '--actor', '--reason', '--health'):
        clear.add_argument(name, required=True)
    clear.add_argument('--log', help='production-action log to append this to')
    args = parser.parse_args(argv)
    path = Path(args.database)
    if not path.is_file():
        print(f'No controller database at {path}', file=sys.stderr)
        return 2
    # Not Store(path): opening the store would apply this release's migrations to a database it may not own.
    if args.command == 'status':
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            active = db.execute("SELECT value FROM settings WHERE key='active_sha'").fetchone()
            print(json.dumps({'active_sha': active[0] if active else None, KEY: current(db)}, sort_keys=True))
        return 0
    started = maintenance.clock()
    try:
        heartbeat(args.health, args.sha)
        with closing(sqlite3.connect(path, timeout=30)) as db, db:
            state = reconcile(db, args.sha, args.actor, args.reason)
    except (ValueError, Conflict) as exc:
        print(exc, file=sys.stderr)
        return 1
    if args.log:
        maintenance.record(args.log, 'clear unknown Gene runtime', actor=args.actor,
                           trigger='maintainer reconciliation', reason=clean(args.reason, 500), outcome='ok',
                           before={'gene': None}, after={'gene': args.sha}, started=started,
                           unknown_since_job=state['job_id'])
    print(f"Runtime known again: {args.sha} (unknown since job {state['job_id']})")
    return 0


if __name__ == '__main__':
    sys.exit(main())
