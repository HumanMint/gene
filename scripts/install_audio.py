#!/usr/bin/env python3
"""Stage or install Gene's external audio services without modifying Red."""

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import urllib.request
from pathlib import Path

import yaml
from build_audio_plugin import build
from redbot.cogs.audio.managed_node.ll_server_config import get_default_server_config

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://github.com/Cog-Creators/Lavalink-Jars/releases/download/3.7.13%2Bred.5/Lavalink.jar'
DIGEST = '64411785e471e926a2329af600563d6fc2a33428ab2c8f6705dab358460a9bbc'


def private_write(path, content):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(content)


def prepare(folder, env):
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    jar = folder / 'Lavalink.jar'
    if not jar.exists() or hashlib.sha256(jar.read_bytes()).hexdigest() != DIGEST:
        existing = ROOT / '.audio/Lavalink.jar'
        temporary = folder / 'Lavalink.download'
        if existing.exists() and hashlib.sha256(existing.read_bytes()).hexdigest() == DIGEST:
            shutil.copyfile(existing, temporary)
        else:
            urllib.request.urlretrieve(URL, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != DIGEST:
            temporary.unlink()
            raise SystemExit('Lavalink artifact checksum did not match the pinned release')
        temporary.replace(jar)
    plugin = build(folder)
    (folder / 'plugins').mkdir(exist_ok=True)
    shutil.copyfile(plugin, folder / 'plugins/gene-youtube.jar')
    token = secrets.token_urlsafe(40)
    private_write(folder / 'resolver.env', 'GENE_RESOLVER_TOKEN=' + token + '\n')
    configuration = get_default_server_config()
    configuration['server']['address'] = '127.0.0.1'
    configuration['lavalink']['server']['password'] = env['GENE_AUDIO_PASSWORD']
    configuration['lavalink']['server']['sources'].update(youtube=False, local=False, http=False)
    configuration['lavalink']['plugins'] = []
    configuration['plugins']['youtube']['enabled'] = False
    configuration['plugins']['geneYoutube'] = {'url': 'http://127.0.0.1:2334', 'token': token, 'cache': str(folder / 'media')}
    private_write(folder / 'application.yml', yaml.safe_dump(configuration))
    shutil.copyfile(ROOT / 'src/gene/audio_resolver.py', folder / 'audio_resolver.py')
    shutil.copyfile(ROOT / 'scripts/run_audio_resolver.py', folder / 'run_audio_resolver.py')
    requirements = subprocess.check_output(['uv', 'export', '--frozen', '--no-dev', '--extra', 'audio',
                                            '--no-emit-project'], cwd=ROOT, text=True)
    (folder / 'requirements.txt').write_text(requirements)
    if not (folder / 'venv/bin/python').exists():
        subprocess.run(['uv', 'venv', '--python', '3.11', str(folder / 'venv')], check=True)
    subprocess.run(['uv', 'pip', 'sync', '--require-hashes', '--python', str(folder / 'venv/bin/python'),
                    str(folder / 'requirements.txt')], check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stage', type=Path, help='Prepare an isolated folder; do not change running services')
    parser.add_argument('--release-sha', '--approved-sha', dest='release_sha',
                        help='Pin this authorized maintainer installation to the complete HEAD commit SHA')
    args = parser.parse_args()
    env_path = ROOT / '.env.local'
    env = dict(line.split('=', 1) for line in env_path.read_text().splitlines()
               if line and not line.startswith('#') and '=' in line)
    if args.stage:
        prepare(args.stage.resolve(), env)
        print('Prepared audio candidate. Running services were not changed.')
        return
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    if not re.fullmatch('[a-f0-9]{40}', args.release_sha or '') or args.release_sha != sha:
        raise SystemExit('Installation requires --release-sha matching the complete HEAD commit')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise SystemExit('Commit all changes before installing a maintainer release')
    folder = ROOT / '.audio/releases' / sha
    if folder.exists():
        raise SystemExit('This audio release already exists. Inspect or restore it; do not overwrite a running release.')
    prepare(folder, env)
    state = json.loads(subprocess.check_output(['tailscale', 'status', '--json']))
    address = next(ip for ip in state['Self']['TailscaleIPs'] if '.' in ip)
    agents = Path.home() / 'Library/LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    definitions = {
        'local.dan.gene.audio.resolver': {'ProgramArguments': [str(folder / 'venv/bin/python'),
            '-u', str(folder / 'run_audio_resolver.py')]},
        'local.dan.gene.audio': {'ProgramArguments': ['/opt/homebrew/opt/openjdk@21/bin/java',
            '-Xms64m', '-Xmx384m', '-jar', str(folder / 'Lavalink.jar')],
            # Standard launchd services have throttled timers on macOS. Discord
            # needs one UDP frame every 20 ms; background scheduling starves it.
            'ProcessType': 'Interactive'},
        'local.dan.gene.audio.proxy': {'ProgramArguments': [str(folder / 'venv/bin/python'),
            str(folder / 'audio_proxy.py')],
            'EnvironmentVariables': {'GENE_AUDIO_BIND': address, 'GENE_VPS_IP': '100.72.42.107'}},
    }
    shutil.copyfile(ROOT / 'scripts/audio_proxy.py', folder / 'audio_proxy.py')
    for label, definition in definitions.items():
        path = agents / (label + '.plist')
        if path.exists():
            shutil.copyfile(path, folder / (label + '.previous.plist'))
        definition.update(Label=label, WorkingDirectory=str(folder), RunAtLoad=True, KeepAlive=True,
                          ThrottleInterval=10, StandardOutPath=str(ROOT / '.audio' / (label + '.log')),
                          StandardErrorPath=str(ROOT / '.audio' / (label + '.error.log')))
        path.write_bytes(plistlib.dumps(definition))
        path.chmod(0o600)
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
    env.update(GENE_AUDIO_HOST=address, GENE_AUDIO_PORT='2333')
    private_write(env_path, ''.join(f'{key}={value}\n' for key, value in env.items()))
    private_write(ROOT / '.audio/release.json', json.dumps({'sha': sha, 'folder': str(folder)}))
    print('Installed the maintainer audio release. Red and Cobalt were not modified.')


if __name__ == '__main__':
    main()
