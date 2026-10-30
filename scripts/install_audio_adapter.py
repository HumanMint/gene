#!/usr/bin/env python3
"""Install Gene's pinned image/development audio bundle without changing its core lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import sysconfig
import tempfile
from importlib.metadata import Distribution, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = 'https://github.com/HumanMint/red-audio-adapter'
MODULES = {'__init__.py', 'audio_cache.py', 'audio_relay_server.py', 'audio_resolver.py', 'runtime_audio.py'}


def load_pin(path):
    pin = json.loads(path.read_text())
    if (pin.get('schema') != 2 or pin.get('repository') != REPOSITORY
            or not re.fullmatch('[0-9a-f]{40}', pin.get('sha', ''))
            or not re.fullmatch(r'\d+\.\d+\.\d+', pin.get('version', ''))
            or set(pin.get('modules', {})) != MODULES
            or not all(re.fullmatch('[0-9a-f]{64}', x) for x in pin['modules'].values())):
        raise ValueError('Use the complete reviewed audio-adapter pin')
    return pin


def verify(target, pin):
    package = target / 'red_audio_adapter'
    metadata = target / f'red_audio_adapter-{pin["version"]}.dist-info'
    if target.is_symlink() or package.is_symlink() or metadata.is_symlink():
        raise ValueError('The adapter bundle must use ordinary directories')
    if set(p.name for p in target.iterdir()) - {'red_audio_adapter', metadata.name, 'bin', '.lock'}:
        raise ValueError('The adapter bundle contains unrelated files')
    for name, expected in pin['modules'].items():
        path = package / name
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('The adapter module bytes differ from the reviewed pin')
    for name in ('METADATA', 'direct_url.json'):
        if (metadata / name).is_symlink() or not (metadata / name).is_file():
            raise ValueError('The adapter provenance is missing or indirect')
    installed = json.loads((metadata / 'direct_url.json').read_text())
    if (installed.get('url') != REPOSITORY
            or installed.get('vcs_info', {}).get('commit_id') != pin['sha']):
        raise ValueError('The adapter Git provenance differs from the reviewed pin')
    distribution = Distribution.at(metadata)
    if distribution.metadata['Name'] != 'red-audio-adapter' or distribution.version != pin['version']:
        raise ValueError('The adapter distribution differs from the reviewed pin')
    # Both the locked Red image and the developer/test environment provide packaging.
    from packaging.requirements import Requirement
    for text in distribution.requires or []:
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
            continue
        if requirement.url or version(requirement.name) not in requirement.specifier:
            raise ValueError('The adapter runtime requirements do not match the prepared Gene environment')


def install(pin, target, site):
    target = target.absolute()
    if any(ord(c) < 32 for c in str(target)):
        raise ValueError('The adapter target must be a single ordinary path')
    if target.is_symlink():
        raise ValueError('The adapter bundle must use an ordinary directory')
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        with tempfile.TemporaryDirectory(prefix='.audio-adapter-', dir=target.parent) as scratch:
            bundle = Path(scratch) / 'bundle'
            subprocess.run(['uv', 'pip', 'install', '--no-cache', '--no-deps', '--target', str(bundle),
                            'red-audio-adapter @ git+' + REPOSITORY + '@' + pin['sha']], check=True)
            verify(bundle, pin)
            # Build contexts can use restrictive modes. Bundle files remain root-owned
            # in images, and readable by the same UID10001 as the runtime/worker.
            subprocess.run(['chmod', '-R', 'a+rX', str(bundle)], check=True)
            os.rename(bundle, target)
    else:
        verify(target, pin)
    site.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.gene-audio-', dir=site)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, 'w') as stream:
            stream.write(str(target) + '\n')
        os.replace(temporary, site / '_gene_audio_adapter.pth')
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pin', type=Path, default=ROOT / 'ops/audio-adapter.json')
    parser.add_argument('--target', type=Path)
    args = parser.parse_args()
    if sys.prefix == sys.base_prefix:
        parser.error('Run this helper using the prepared Gene Red/development virtual environment')
    pin = load_pin(args.pin)
    target = args.target or Path(sys.prefix) / 'audio-adapter' / pin['sha']
    install(pin, target, Path(sysconfig.get_path('purelib')))
    print('Installed the reviewed audio adapter ' + pin['sha'])


if __name__ == '__main__':
    main()
