#!/usr/bin/env python3
"""Run as root on Hostinger after uploading source and the private lab env file."""

import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path('/srv/gene')


def main():
    if os.geteuid() != 0:
        raise SystemExit('Run this installer with sudo on the intended Hostinger machine')
    source = ROOT / 'source'
    upload = ROOT / 'upload.env'
    env = dict(line.split('=', 1) for line in upload.read_text().splitlines()
               if line and not line.startswith('#') and '=' in line)
    if not env.get('GENE_GUILD_ID') or not env.get('DISCORD_TOKEN'):
        raise SystemExit('Complete scripts/configure.py first')
    for relative in ('secrets', 'work', 'backups', 'data/controller', 'data/red', 'releases'):
        path = ROOT / relative
        path.mkdir(parents=True, exist_ok=True, mode=0o700 if relative in ('secrets', 'work', 'backups') else 0o755)
    for relative in ('data/controller', 'data/red'):
        os.chown(ROOT / relative, 10001, 10001)
    base = {'GENE_CONTROLLER_URL': 'http://127.0.0.1:8787', 'GENE_REPOSITORY': 'HumanMint/gene'}
    connection = ('GENE_GUILD_ID', 'GENE_OWNER_ID', 'GENE_WORKSHOP_CHANNEL_ID', 'GENE_HISTORY_CHANNEL_ID')
    controller = {key: env.get(key, '') for key in (*connection, 'GENE_RUNTIME_TOKEN', 'GENE_WORKER_TOKEN', 'GENE_DEPLOY_TOKEN', 'GENE_FRI_TOKEN')}
    runtime_keys = (*connection, 'DISCORD_TOKEN', 'GENE_RUNTIME_TOKEN', 'GENE_HIGHLIGHT_CHANNEL_ID',
                    'GENE_MEME_SOURCE_CHANNEL_IDS', 'GENE_HIGHLIGHT_THRESHOLD', 'GENE_PREFIX',
                    'GENE_AUDIO_HOST', 'GENE_AUDIO_PORT', 'GENE_AUDIO_PASSWORD', 'GENE_LAB_MODE')
    runtime = {key: env.get(key, '') for key in runtime_keys}
    ssh = ('ssh -i /srv/gene/secrets/github_ed25519 -oIdentitiesOnly=yes -oBatchMode=yes '
           '-oStrictHostKeyChecking=yes -oUserKnownHostsFile=/srv/gene/secrets/github_known_hosts')
    worker = {**base, 'GENE_WORKER_TOKEN': env['GENE_WORKER_TOKEN'],
              'GENE_GIT_SSH_COMMAND': ssh, 'GENE_CODEX_HOME': '/srv/gene/codex',
              'GENE_WORKER_IMAGE': 'gene-worker:bootstrap', 'GENE_SANDBOX_UID': '10001'}
    if env.get('GENE_CODEX_MODEL'):
        worker['GENE_CODEX_MODEL'] = env['GENE_CODEX_MODEL']
    deployer = {**base, 'GENE_DEPLOY_TOKEN': env['GENE_DEPLOY_TOKEN'], 'GENE_GIT_SSH_COMMAND': ssh}
    for name, values in [('controller', controller), ('runtime', runtime),
                         ('worker', worker), ('deployer', deployer)]:
        path = ROOT / 'secrets' / (name + '.env')
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as output:
            for key, value in values.items():
                if any(c in value for c in ('\n', '\r', '\x00')):
                    raise ValueError('Environment values must be single lines')
                output.write(f'{key}={value}\n')
    key = ROOT / 'secrets/github_ed25519'
    if not key.exists():
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', 'gene-private-worker',
                        '-f', str(key)], check=True)
    for service in ('gene-worker', 'gene-deployer'):
        shutil.copyfile(source / 'ops' / (service + '.service'),
                        Path('/etc/systemd/system') / (service + '.service'))
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    upload.unlink()
    print('Installed isolated service configuration. Services have not been started.')


if __name__ == '__main__':
    main()

