#!/usr/bin/env python3
"""Install only the home download relay; no audio decoder or Discord voice sender."""

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LABEL = 'local.dan.gene.download-relay'


def private_write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(value)


def prepare(folder, env):
    required = ('GENE_RELAY_TOKEN', 'GENE_RELAY_BIND', 'GENE_RELAY_PEER')
    if not all(env.get(key) for key in required) or len(env['GENE_RELAY_TOKEN']) < 32:
        raise ValueError('Configure the private relay token and Tailscale peer/bind addresses')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    (folder / 'gene').mkdir(exist_ok=True)
    (folder / 'gene/__init__.py').write_text('')
    for name in ('audio_resolver.py', 'audio_cache.py', 'audio_relay_server.py'):
        shutil.copyfile(ROOT / 'src/gene' / name, folder / 'gene' / name)
    shutil.copyfile(ROOT / 'scripts/run_audio_resolver.py', folder / 'run_audio_resolver.py')
    requirements = subprocess.check_output(['uv', 'export', '--frozen', '--no-dev', '--extra', 'audio',
                                            '--no-emit-project'], cwd=ROOT, text=True)
    (folder / 'requirements.txt').write_text(requirements)
    subprocess.run(['uv', 'venv', '--python', '3.11', str(folder / 'venv')], check=True)
    subprocess.run(['uv', 'pip', 'sync', '--require-hashes', '--python', str(folder / 'venv/bin/python'),
                    str(folder / 'requirements.txt')], check=True)
    values = {'GENE_RESOLVER_TOKEN': env['GENE_RELAY_TOKEN'], 'GENE_RELAY_ONLY': 'true',
              'GENE_RESOLVER_BIND': env['GENE_RELAY_BIND'], 'GENE_RELAY_PEER': env['GENE_RELAY_PEER'],
              'GENE_RESOLVER_PORT': env.get('GENE_RELAY_PORT', '2335'),
              'GENE_AUDIO_CACHE': str(folder / 'transfers')}
    private_write(folder / 'resolver.env', ''.join(f'{key}={value}\n' for key, value in values.items()))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', type=Path)
    parser.add_argument('--release-sha')
    args = parser.parse_args()
    env = dict(line.split('=', 1) for line in (ROOT / '.env.local').read_text().splitlines()
               if '=' in line and not line.startswith('#'))
    if args.stage:
        prepare(args.stage.resolve(), env)
        print('Prepared the download-only relay. Running services are unchanged.')
        return
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if args.release_sha != sha or not re.fullmatch('[a-f0-9]{40}', sha):
        raise SystemExit('--release-sha must match the complete HEAD commit')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise SystemExit('Commit changes before installing a release')
    folder = ROOT / '.audio/relay-releases' / sha
    if folder.exists():
        raise SystemExit('This relay release already exists; do not overwrite it')
    prepare(folder, env)
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    if plist.exists():
        shutil.copyfile(plist, folder / 'previous.plist')
    definition = {'Label': LABEL, 'ProgramArguments': [str(folder / 'venv/bin/python'),
                  '-u', str(folder / 'run_audio_resolver.py')], 'WorkingDirectory': str(folder),
                  'RunAtLoad': True, 'KeepAlive': True, 'ProcessType': 'Background', 'Nice': 10,
                  'ThrottleInterval': 10, 'StandardOutPath': str(ROOT / '.audio/relay.log'),
                  'StandardErrorPath': str(ROOT / '.audio/relay.error.log')}
    plist.write_bytes(plistlib.dumps(definition))
    plist.chmod(0o600)
    subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(plist)], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(plist)], check=True)
    private_write(ROOT / '.audio/relay-release.json', json.dumps({'sha': sha, 'folder': str(folder)}))
    print('Installed the download-only relay. Stop legacy Mac audio after Hostinger playback passes.')


if __name__ == '__main__':
    main()
