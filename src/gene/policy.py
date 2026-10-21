"""Release scope checks executed by the trusted wrapper, outside candidate code."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ALLOWED = ('cogs/', 'tests/features/', 'docs/features/')


class NoFeatureChanges(ValueError):
    pass


def git(root: Path, *args: str, raw=False):
    # Disable hooks, file protocols and user-supplied Git configuration.
    environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    environment.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
                        'GIT_TERMINAL_PROMPT': '0'})
    result = subprocess.run(['git', '-c', f'safe.directory={root}',
                             '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                             '-c', 'protocol.file.allow=never', '-c', 'core.sshCommand=' +
                             os.getenv('GENE_GIT_SSH_COMMAND', 'ssh -oBatchMode=yes'),
                             *args], cwd=root, env=environment, capture_output=True, text=not raw, check=True)
    return result.stdout if raw else result.stdout.strip()


def validate_paths(paths: list[str]):
    if not paths:
        raise NoFeatureChanges('No feature changes were produced')
    for path in paths:
        if not path.startswith(ALLOWED) or any(p in ('.git', '..') for p in Path(path).parts):
            raise ValueError(f'Feature job changed a protected path: {path}')
        if Path(path).suffix not in ('.py', '.json', '.md', '.txt'):
            raise ValueError(f'Unsupported feature file: {path}')


def validate_checkout(root: Path):
    tracked = git(root, 'diff', '--name-only', '-z', 'HEAD').split('\0')
    untracked = git(root, 'ls-files', '--others', '--exclude-standard', '-z').split('\0')
    paths = sorted({p for p in tracked + untracked if p})
    validate_paths(paths)
    for name in paths:
        path = root / name
        # Reject symlinks anywhere in the path, including deleted files' parents.
        for item in (path, *path.parents):
            if item == root:
                break
            if item.is_symlink():
                raise ValueError(f'Symlinks are not allowed in feature releases: {name}')
        if path.exists() and (not path.is_file() or path.stat().st_size > 1_000_000):
            raise ValueError(f'Unsupported or oversized feature file: {name}')
        if path.exists():
            from .privacy import redact, source_has_credentials
            text = path.read_text()
            has_credentials = source_has_credentials(text) if path.suffix == '.py' else redact(text) != text
            if has_credentials:
                raise ValueError(f'Potential credentials found in candidate file: {name}')
    return paths


def validate_commit(root: Path, base: str, sha: str):
    from .store import SHA
    if not SHA.fullmatch(base) or not SHA.fullmatch(sha):
        raise ValueError('Invalid release SHA')
    git(root, 'merge-base', '--is-ancestor', base, sha)
    names = [p for p in git(root, 'diff', '--name-only', '-z', base, sha).split('\0') if p]
    validate_paths(names)
    # Git mode 120000 is a symlink, 160000 a submodule. Only ordinary files ship.
    for record in git(root, 'ls-tree', '-r', sha).splitlines():
        mode, _, rest = record.partition(' ')
        if mode not in ('100644', '100755'):
            raise ValueError(f'Unsupported Git object in release: {rest}')
    return names
