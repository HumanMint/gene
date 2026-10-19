from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import tarfile
import time
from pathlib import Path

from .client import ControllerClient
from .policy import git, validate_commit
from .privacy import clean
from .store import Store
from .subscription import private_json

LOG = logging.getLogger('gene.deploy')


class Deployer:
    def __init__(self):
        self.api = ControllerClient('deploy')
        self.root = Path(os.getenv('GENE_ROOT', '/srv/gene'))
        self.source = self.root / 'source'
        self.repository = os.getenv('GENE_REPOSITORY', 'HumanMint/gene')
        self.health_file = self.root / 'data/red/gene-health.json'

    def compose(self, *args):
        subprocess.run(['docker', 'compose', '--project-name', 'gene', '--env-file',
                        str(self.root / 'release.env'), '-f', str(self.source / 'ops/compose.yml'),
                        *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def activate(self, sha):
        destination = self.root / 'release.env'
        temporary = destination.with_suffix('.tmp')
        # Cog releases inherit the maintainer-installed control-plane image.
        # A feature job cannot select or replace the trusted runtime.
        existing = dict(line.split('=', 1) for line in destination.read_text().splitlines()
                        if '=' in line) if destination.exists() else {}
        image = existing.get('GENE_RUNTIME_IMAGE', 'gene-runtime:bootstrap')
        temporary.write_text(f'GENE_RELEASE_SHA={sha}\nGENE_ROOT={self.root}\nGENE_RUNTIME_IMAGE={image}\n')
        temporary.replace(destination)
        self.compose('up', '-d', '--no-deps', '--force-recreate', 'red')

    def healthy(self, sha, after):
        deadline = time.monotonic() + 120
        consecutive = 0
        while time.monotonic() < deadline:
            try:
                report = json.loads(self.health_file.read_text())
                valid = (report['sha'] == sha and report['guild_connected']
                         and report['at'] > after and time.time() - report['at'] < 25)
            except (OSError, ValueError, KeyError):
                valid = False
            consecutive = consecutive + 1 if valid else 0
            if consecutive >= 6:
                return True
            time.sleep(5)
        return False

    def prepare(self, job):
        sha, base = job['proposal']['sha'], job['proposal']['base_sha']
        destination = self.root / 'releases' / sha
        if job['kind'] == 'rollback':
            if not (destination / 'cogs/gene/__init__.py').is_file():
                raise RuntimeError('The previous approved release is missing')
            return
        repository = self.root / 'release-repo'
        if not repository.exists():
            git(self.root, 'clone', '--no-checkout', f'git@github.com:{self.repository}.git', str(repository))
        git(repository, 'fetch', 'origin', sha)
        validate_commit(repository, base, sha)
        if destination.exists():
            raise RuntimeError('A release directory already exists; inspect it before retrying')
        destination.mkdir(parents=True)
        archive = self.root / 'work' / f'{sha}.tar'
        git(repository, 'archive', '--format=tar', '--output=' + str(archive), sha, 'cogs')
        try:
            with tarfile.open(archive) as bundle:
                for member in bundle.getmembers():
                    if member.issym() or member.islnk() or member.name.startswith('/') or '..' in Path(member.name).parts:
                        raise ValueError('Invalid release archive')
                bundle.extractall(destination, filter='data')
        finally:
            archive.unlink(missing_ok=True)
        for directory, dirs, files in os.walk(destination):
            Path(directory).chmod(0o755)
            for name in files:
                (Path(directory) / name).chmod(0o444)

    def process(self, job):
        sha, previous = job['proposal']['sha'], job['proposal']['base_sha']
        self.prepare(job)
        backup_dir = self.root / 'backups' / job['id']
        backup_dir.mkdir(parents=True, mode=0o700)
        Store(self.root / 'data/controller/gene.sqlite3').backup(backup_dir / 'controller.sqlite3')
        self.compose('stop', 'red')
        try:
            with tarfile.open(backup_dir / 'red.tar.gz', 'w:gz') as archive:
                archive.add(self.root / 'data/red', arcname='red')
            started = time.time()
            self.activate(sha)
            if not self.healthy(sha, started):
                raise RuntimeError('New release did not become healthy')
        except Exception:
            restored = time.time()
            self.activate(previous)
            if not self.healthy(previous, restored):
                raise RuntimeError('Release and automatic rollback both failed. Operator recovery required') from None
            raise RuntimeError('Release failed health checks; the previous code was restored') from None

    def finish_pending(self):
        """Retry only the durable completion receipt, never repeat its deployment."""
        path = self.root / 'deploy-result.json'
        receipt = getattr(self, 'pending_result', None)
        if receipt is None:
            try:
                receipt = json.loads(path.read_text())
            except FileNotFoundError:
                return False
        # Corrupt state needs operator reconciliation, not a new claim or guessed outcome.
        if (not isinstance(receipt, dict) or set(receipt) != {'job_id', 'lease', 'success', 'detail'}
                or not re.fullmatch(r'[a-f0-9]{12}', str(receipt['job_id']))
                or not re.fullmatch(r'[a-f0-9]{32}', str(receipt['lease']))
                or type(receipt['success']) is not bool or not isinstance(receipt['detail'], str)):
            raise ValueError('Invalid deployment completion receipt; operator reconciliation required')
        self.pending_result = receipt
        private_json(path, receipt)
        self.sync_directory()
        payload = {key: receipt[key] for key in ('lease', 'success', 'detail')}
        acknowledgement = self.api.post(f'/deploy/{receipt["job_id"]}/finish', payload)
        if acknowledgement != {'ok': True}:
            raise RuntimeError('Controller did not acknowledge the deployment receipt')
        path.unlink()
        self.sync_directory()
        self.pending_result = None
        return True

    def sync_directory(self):
        descriptor = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def tick(self):
        if self.finish_pending():
            return True
        job = self.api.post('/deploy/claim')
        if not job:
            return False
        LOG.info('Deploying approved request %s', job['id'])
        try:
            self.process(job)
            success, detail = True, 'Deployed approved commit; Discord connection and Gene heartbeat healthy.'
        except Exception as exc:
            success, detail = False, str(exc)
            LOG.error('Deployment %s failed (%s)', job['id'], type(exc).__name__)
        # Keep the result in memory as well, so a temporary disk failure cannot cause
        # the running process to forget it. A crash before persistence still fails closed.
        self.pending_result = {'job_id': job['id'], 'lease': job['lease'],
                               'success': success, 'detail': clean(detail)}
        return self.finish_pending()


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    deployer = Deployer()
    with (deployer.root / 'deploy.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        while True:
            try:
                deployer.tick()
            except Exception as exc:
                LOG.error('Deployer waiting after %s', type(exc).__name__)
            time.sleep(10)


if __name__ == '__main__':
    main()
