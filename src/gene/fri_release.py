"""Maintainer-only Fri release coordinator. No Discord endpoint invokes this module.

Only Fri code/config changes on rollback. Current controller state, delivery
receipts and subscription identity are never restored from an older snapshot.
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import sqlite3
import subprocess
import tarfile
import time
from pathlib import Path

from . import maintenance
from .private_files import private_json


class FriRelease:
    def __init__(self, root=Path('/srv/fri'), gene=Path('/srv/gene')):
        self.root, self.gene = Path(root), Path(gene)
        self.config = json.loads((self.root / 'install.json').read_text())
        required = {'env_file', 'data_dir', 'auth_dir', 'network', 'controller_url', 'legacy_container'}
        if set(self.config) != required or not all(isinstance(v, str) and '\n' not in v and '\r' not in v for v in self.config.values()):
            raise ValueError('Invalid Fri installation configuration')
        for key in ('env_file', 'data_dir', 'auth_dir'):
            if not Path(self.config[key]).is_absolute() or not Path(self.config[key]).exists():
                raise ValueError('Missing private Fri installation path')
        self.data = Path(self.config['data_dir'])
        self.env = self.root / 'release.env'
        self.db = self.gene / 'data/controller/gene.sqlite3'
        self.compose_file = self.gene / 'source/ops/fri-compose.yml'
        self.record = self.root / 'release.json'
        self.run = self._run
        self.restart_s = None

    @staticmethod
    def _run(args):
        result = subprocess.run(args, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError('Fri release command failed: '+args[0])
        return result.stdout.strip()

    def compose(self, *args):
        return self.run(['docker', 'compose', '--project-name', 'fri', '--env-file', str(self.env),
                         '-f', str(self.compose_file), *args])

    def environment(self, sha):
        values = {'FRI_RELEASE_SHA': sha, 'FRI_RUNTIME_IMAGE': 'fri-runtime:'+sha,
            'FRI_ENV_FILE': self.config['env_file'], 'FRI_DATA_DIR': self.config['data_dir'],
            'FRI_AUTH_DIR': self.config['auth_dir'], 'FRI_NETWORK': self.config['network'],
            'FRI_CONTROLLER_URL': self.config['controller_url']}
        # Compose interpolation values are never shell instructions. Reject syntax
        # that could be interpreted as additional dotenv values or expansions.
        if any(not re.fullmatch(r'[A-Za-z0-9_./:@+\-]+', value) for value in values.values()):
            raise ValueError('Invalid Fri release environment')
        return ''.join(key+'='+value+'\n' for key, value in values.items()).encode()

    @staticmethod
    def write(path, data):
        temporary = path.with_name(path.name+'.tmp')
        with temporary.open('wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)

    def healthy(self, sha, after, container='fri-runtime-1'):
        deadline, good = time.monotonic()+180, 0
        env = dict(line.split('=', 1) for line in Path(self.config['env_file']).read_text().splitlines()
                   if '=' in line and not line.startswith('#'))
        while time.monotonic() < deadline:
            try:
                value = json.loads((self.data/'fri-health.json').read_text())
                state = json.loads(self.run(['docker', 'inspect', '-f', '{{json .State}}', container]))
                loop = value['agent_heartbeat']
                valid = (value['sha'] == sha and value['at'] > after and time.time()-value['at'] < 30
                    and str(value['guild_id']) == env['GENE_GUILD_ID'] and value['guild_connected']
                    and loop['running'] and not loop['failed'] and state['Running']
                    and state['Health']['Status'] == 'healthy')
            except (OSError, ValueError, KeyError, RuntimeError):
                valid = False
            good = good+1 if valid else 0
            if good >= 4:
                return True
            time.sleep(5)
        return False

    def audit(self, sha, outcome, *, rollback_to=None):
        with sqlite3.connect(self.db, timeout=30) as db:
            db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
                (time.time(), 'maintainer:codex', 'fri_release', sha, outcome))
            if outcome == 'deployed':
                db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           ('fri_active_sha', sha))
            elif rollback_to:
                db.execute('INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           ('fri_active_sha', rollback_to))
            else:
                db.execute("DELETE FROM settings WHERE key='fri_active_sha'")

    def deploy(self, sha):
        if not re.fullmatch('[a-f0-9]{40}', sha):
            raise ValueError('A full reviewed Fri commit is required')
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root/'release.lock').open('a') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self._deploy(sha)

    def _deploy(self, sha):
        expected = self.run(['docker', 'image', 'inspect', '-f',
            '{{index .Config.Labels "org.opencontainers.image.revision"}}', 'fri-runtime:'+sha])
        if expected != sha:
            raise ValueError('Fri image does not match the reviewed commit')
        before = json.loads(self.record.read_text()) if self.record.exists() else None
        if before is not None and (not isinstance(before, dict)
                or not re.fullmatch('[a-f0-9]{40}', str(before.get('sha', '')))):
            raise ValueError('Invalid Fri release record; reconcile before continuing')
        if before and before['sha'] == sha:
            raise ValueError('This Fri release is already active')
        old_env = self.env.read_bytes() if self.env.exists() else None
        if (before is None) != (old_env is None):
            raise ValueError('Inconsistent Fri release state; reconcile before continuing')
        if before and old_env != self.environment(before['sha']):
            # A killed coordinator can leave the new env beside the previous
            # record. Never turn that partial state into a rollback baseline.
            raise ValueError('Inconsistent Fri release pins; reconcile before continuing')
        content = self.environment(sha)
        backup = self.root/'backups'/(str(time.time_ns())+'-'+sha)
        backup.mkdir(parents=True, mode=0o700)
        private_json(backup/'before.json', before)
        if old_env:
            self.write(backup/'release.env', old_env)
        # Require a valid existing controller; never create a new empty database.
        if not self.db.is_file():
            raise ValueError('Controller database is missing')
        with sqlite3.connect('file:'+str(self.db)+'?mode=ro', uri=True) as db:
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Controller database needs attention')
            active = db.execute("SELECT value FROM settings WHERE key='fri_active_sha'").fetchone()
            if (active[0] if active else None) != (before['sha'] if before else None):
                raise ValueError('Inconsistent controller Fri release; reconcile before continuing')
        legacy = self.config['legacy_container']
        legacy_sha = None
        if not before and legacy:
            health = json.loads((self.data/'fri-health.json').read_text())
            legacy_sha = health['sha']
        stopped = False
        try:
            stopped = True  # Even a partially failed stop needs recovery.
            if before:
                self.compose('stop', 'runtime')
            elif legacy:
                self.run(['docker', 'update', '--restart=no', legacy])
                self.run(['docker', 'stop', legacy])
            # Snapshot current receipts for disaster recovery only, never automatic restore.
            with tarfile.open(backup/'fri-data.tar.gz', 'w:gz') as archive:
                archive.add(self.data, arcname='fri')
            (backup/'fri-data.tar.gz').chmod(0o600)
            with sqlite3.connect('file:'+str(self.db)+'?mode=ro', uri=True) as db:
                with sqlite3.connect(backup/'controller.sqlite3') as snapshot:
                    db.backup(snapshot)
            (backup/'controller.sqlite3').chmod(0o600)
            self.write(self.env, content)
            started = time.time()
            self.compose('up', '-d', '--no-deps', '--force-recreate', 'runtime')
            if not self.healthy(sha, started):
                raise RuntimeError('New Fri failed its health checks')
            self.restart_s = time.time() - started
            self.audit(sha, 'deployed')
            private_json(self.record, {'sha': sha, 'previous': before['sha'] if before else None,
                'legacy_sha': legacy_sha, 'backup': str(backup), 'at': time.time()})
            return {'sha': sha, 'backup': str(backup), 'success': True}
        except BaseException:
            recovered = not stopped
            if stopped:
                # Stop a possibly started new singleton before starting the previous one.
                with contextlib.suppress(Exception):
                    self.compose('stop', 'runtime')
                restored = time.time()
                if old_env:
                    self.write(self.env, old_env)
                    self.compose('up', '-d', '--no-deps', '--force-recreate', 'runtime')
                    recovered = self.healthy(before['sha'], restored)
                elif legacy:
                    self.env.unlink(missing_ok=True)
                    self.run(['docker', 'update', '--restart=unless-stopped', legacy])
                    self.run(['docker', 'start', legacy])
                    recovered = self.healthy(legacy_sha, restored, legacy)
                else:
                    self.env.unlink(missing_ok=True)
                    recovered = True  # First installation had no bot to restore.
            self.audit(sha, 'failed; current state preserved; recovered='+str(recovered),
                       rollback_to=before['sha'] if before else None)
            private_json(backup/'failure.json', {'recovered': recovered, 'state_restored': False})
            raise


def recorded(root):
    try:
        return json.loads((Path(root)/'release.json').read_text())
    except (OSError, ValueError):
        return None


def permitted(sha, root, exception):
    """Inside the maintenance window, a rollback to the Fri this one replaced, or the owner's exception."""
    before = recorded(root)
    return (maintenance.is_open() or bool(exception)
            or (isinstance(before, dict) and before.get('previous') == sha))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('sha', help='full maintainer-reviewed Fri commit; earlier compatible SHAs can roll back')
    parser.add_argument('--root', type=Path, default=Path('/srv/fri'))
    parser.add_argument('--gene-root', type=Path, default=Path('/srv/gene'))
    args = parser.parse_args()
    exception = os.environ.get('MAINTENANCE_EXCEPTION', '').strip()
    previous = recorded(args.root)
    before = previous.get('sha') if isinstance(previous, dict) else None
    log = dict(actor=os.environ.get('SUDO_USER') or 'root', trigger='gene.fri_release', reason='Fri release '+args.sha,
               before={'fri': before}, exception=exception)
    if not permitted(args.sha, args.root, exception):
        maintenance.record(args.gene_root/'production-actions.jsonl', 'fri_release', outcome='refused: outside the window',
                           after={'fri': before}, **log)
        raise SystemExit('Outside the maintenance window [04:00, 05:00) America/Los_Angeles; nothing was changed. '
                         'Only a rollback to the previous Fri runs at any hour.')
    release, started, outcome = FriRelease(args.root, args.gene_root), time.time(), 'failed'
    try:
        result = release.deploy(args.sha)
        outcome = 'ok'
    finally:
        with contextlib.suppress(Exception):  # The record never decides the release's outcome.
            maintenance.record(args.gene_root/'production-actions.jsonl', 'fri_release', outcome=outcome,
                               after={'fri': (recorded(args.root) or {}).get('sha')}, started=started,
                               restart_s=release.restart_s, **log)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
