#!/usr/bin/env python3
"""Install the Red-pinned Lavalink node on this Mac without touching Cobalt."""

import hashlib
import json
import os
import plistlib
import subprocess
import sys
import urllib.request
from pathlib import Path

import yaml
from redbot.cogs.audio.managed_node.ll_server_config import get_default_server_config

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://github.com/Cog-Creators/Lavalink-Jars/releases/download/3.7.13%2Bred.5/Lavalink.jar'
DIGEST = '64411785e471e926a2329af600563d6fc2a33428ab2c8f6705dab358460a9bbc'


def main():
    env_path = ROOT / '.env.local'
    env = dict(line.split('=', 1) for line in env_path.read_text().splitlines()
               if line and not line.startswith('#') and '=' in line)
    state = json.loads(subprocess.check_output(['tailscale', 'status', '--json']))
    address = next(ip for ip in state['Self']['TailscaleIPs'] if '.' in ip)
    folder = ROOT / '.audio'
    folder.mkdir(mode=0o700, exist_ok=True)
    jar = folder / 'Lavalink.jar'
    if not jar.exists() or hashlib.sha256(jar.read_bytes()).hexdigest() != DIGEST:
        temporary = folder / 'Lavalink.download'
        urllib.request.urlretrieve(URL, temporary)
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != DIGEST:
            temporary.unlink()
            raise SystemExit('Lavalink artifact checksum did not match the pinned release')
        temporary.replace(jar)
    configuration = get_default_server_config()
    configuration['server']['address'] = '127.0.0.1'
    configuration['lavalink']['server']['password'] = env['GENE_AUDIO_PASSWORD']
    configuration['lavalink']['server']['sources']['local'] = False
    configuration['lavalink']['server']['sources']['http'] = False
    config_path = folder / 'application.yml'
    fd = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as output:
        yaml.safe_dump(configuration, output)
    agents = Path.home() / 'Library/LaunchAgents'
    agents.mkdir(parents=True, exist_ok=True)
    definitions = {
        'local.dan.gene.audio': {'ProgramArguments': ['/opt/homebrew/opt/openjdk@21/bin/java',
            '-Xms64m', '-Xmx384m', '-jar', str(jar)]},
        'local.dan.gene.audio.proxy': {'ProgramArguments': [sys.executable, str(ROOT / 'scripts/audio_proxy.py')],
            'EnvironmentVariables': {'GENE_AUDIO_BIND': address, 'GENE_VPS_IP': '100.72.42.107'}},
    }
    for label, definition in definitions.items():
        path = agents / (label + '.plist')
        definition.update(Label=label, WorkingDirectory=str(folder), RunAtLoad=True, KeepAlive=True,
                          ThrottleInterval=10, StandardOutPath=str(folder / (label + '.log')),
                          StandardErrorPath=str(folder / (label + '.error.log')))
        path.write_bytes(plistlib.dumps(definition))
        path.chmod(0o600)
        subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
        subprocess.run(['launchctl', 'bootstrap', f'gui/{os.getuid()}', str(path)], check=True)
    env['GENE_AUDIO_HOST'] = address
    env['GENE_AUDIO_PORT'] = '2333'
    env_path.write_text(''.join(f'{key}={value}\n' for key, value in env.items()))
    env_path.chmod(0o600)
    print('Installed local Gene audio and the VPS-only tailnet relay. Cobalt was not changed.')


if __name__ == '__main__':
    main()

