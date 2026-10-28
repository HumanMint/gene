#!/usr/bin/env python3
"""Run Gene's controller, and optionally its Red bot, on this machine for development.

    uv run python scripts/dev.py init         # .env.local with fresh service tokens
    uv run python scripts/dev.py check        # offline config check and controller dry run
    uv run python scripts/dev.py controller   # serve the controller on http://127.0.0.1:8787
    uv run python scripts/dev.py fri-env PATH # the values a local Fri needs, written to PATH
    uv run python scripts/dev.py red          # optional: Gene's Red bot (uv sync --extra red)

Nothing here contacts Discord or prints a token, and tokens are written only to files Git
confirms are untracked and ignored, such as .env.local. State stays under .state/ in this checkout.
Production installation is docs/setup.md.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import secrets
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / '.env.local'
SERVICE_TOKENS = ('GENE_RUNTIME_TOKEN', 'GENE_WORKER_TOKEN', 'GENE_DEPLOY_TOKEN', 'GENE_FRI_TOKEN')
# Fri reads these three as numbers at startup, and the controller scopes every Fri call to them.
FRI_IDS = ('GENE_GUILD_ID', 'GENE_OWNER_ID', 'GENE_WORKSHOP_CHANNEL_ID')
RED_KEYS = ('DISCORD_TOKEN', 'GENE_GUILD_ID', 'GENE_OWNER_ID')
ID_KEYS = ('GENE_GUILD_ID', 'GENE_OWNER_ID', 'GENE_COMMAND_CHANNEL_ID', 'GENE_WORKSHOP_CHANNEL_ID',
           'GENE_HISTORY_CHANNEL_ID', 'GENE_HIGHLIGHT_CHANNEL_ID', 'GENE_RUNTIME_GUILD_ID',
           'FRI_TEST_GUILD_ID', 'FRI_TEST_CHANNEL_ID', 'FRI_LEARNING_ROLE_ID')
ID_LISTS = ('GENE_MEME_SOURCE_CHANNEL_IDS', 'FRI_CHANNEL_IDS', 'FRI_HISTORY_CHANNEL_IDS',
            'FRI_MEME_CHANNEL_IDS', 'FRI_STATUS_CONTEXT_CHANNEL_IDS')
LOOPBACK = ('127.0.0.1', 'localhost', '::1')
TEMPLATE_SUFFIXES = ('.example', '.sample', '.template', '.dist')


def read_env(path: Path) -> dict[str, str]:
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line)


def load(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SystemExit(f'{path} does not exist yet. Run: uv run python scripts/dev.py init')
    return read_env(path)


def write_private(path: Path, lines: list[str]):
    """Replace path atomically with a file that is mode 600 from its first byte."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as output:
            output.write(''.join(line + '\n' for line in lines))
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def merged(lines: list[str], values: dict[str, str]) -> list[str]:
    """Set each key once, where it first appears, keeping comments and other keys; append keys the
    file lacks. Later copies of a key are dropped, since env readers take the last one."""
    for value in values.values():
        if any(character in value for character in '\r\n\x00'):
            raise ValueError('Configuration values must be single lines')
    pending = dict(values)
    result = []
    for line in lines:
        setting = line.strip()
        key = setting.split('=', 1)[0].strip() if '=' in setting and not setting.startswith('#') else None
        if key in values:
            if key in pending:
                result.append(f'{key}={pending.pop(key)}')
            continue
        result.append(line)
    return result + [f'{key}={value}' for key, value in pending.items()]


def unsafe_for_tokens(path: Path) -> str | None:
    """Why path mustn't receive a token, or None once Git confirms it is untracked and ignored.

    The repositories are public, so a token may only go into a git-ignored file such as .env.local.
    When Git can't answer (not installed, not a checkout, any error), that is a refusal too."""
    if path.name.endswith(TEMPLATE_SUFFIXES):
        return 'it is a shared template'
    if not path.parent.is_dir():
        return f'{path.parent} does not exist'

    def git(directory, *command):
        try:
            return subprocess.run(['git', '-C', str(directory), *command], capture_output=True, text=True)
        except OSError:
            return None

    unsure = "Git couldn't confirm it is untracked (is git installed, and is this a git checkout?)"
    folder = path.parent.resolve()
    directory, nearest = folder, True
    while True:  # Every enclosing checkout: a nested one could hide an outer checkout's tracked file.
        top = git(directory, 'rev-parse', '--show-toplevel')
        if top is None or top.returncode != 0 or not top.stdout.strip():
            if nearest:
                return unsure
            break  # No checkout encloses this one.
        top = Path(top.stdout.strip()).resolve()
        try:
            here = folder.relative_to(top).as_posix()
        except ValueError:
            return unsure
        prefix = '' if here == '.' else here + '/'
        # Literal and ignoring case: on a case-insensitive disk, .ENV.REVIEW is the tracked .env.review.
        listed = git(top, 'ls-files', '-z', '--', f':(literal,icase){prefix}{path.name}')
        if listed is None or listed.returncode != 0:
            return unsure
        if listed.stdout:
            return 'Git tracks it'
        if path.exists():  # Git folds only ASCII case; the disk may also fold .env.Ä into .env.ä.
            siblings = git(top, 'ls-files', '-z', '--', f':(literal){prefix or "."}')
            if siblings is None or siblings.returncode != 0:
                return unsure
            for name in filter(None, siblings.stdout.split('\0')):
                other = top / name
                if '/' not in name[len(prefix):] and other.exists() and os.path.samefile(other, path):
                    return 'Git tracks it'
        if top.parent == top:
            break
        directory, nearest = top.parent, False
    ignored = git(path.parent, 'check-ignore', '-q', '--', path.name)
    if ignored is None or ignored.returncode not in (0, 1):
        return "Git couldn't confirm it is ignored"
    if ignored.returncode == 1:
        return "Git doesn't ignore it, so it could be committed"
    return None


def write_tokens(path: Path, lines: list[str]):
    """write_private, but only into a file Git confirms is untracked and ignored."""
    problem = unsafe_for_tokens(path)
    if problem:
        raise SystemExit(f'Refusing to write a token into {path}: {problem}. '
                         'Use a git-ignored env file in a git checkout, such as .env.local.')
    write_private(path, lines)


def init(args):
    path = args.env
    if path.exists():
        lines = path.read_text().splitlines()
        current = read_env(path)
    else:
        lines = (ROOT / '.env.example').read_text().splitlines()
        current = {}
    missing = [key for key in SERVICE_TOKENS if not current.get(key)]
    if path.exists() and not missing:
        print(f'{path} already has its service tokens; nothing changed.')
    else:
        write_tokens(path, merged(lines, {key: secrets.token_urlsafe(48) for key in missing}))
        print(f'Wrote {path} (mode 600) with new {", ".join(missing)}. No token was printed.')
    print('Next: fill in the Discord IDs you need (see docs/quickstart.md), then run'
          ' `uv run python scripts/dev.py check`.')


def apply(env: dict[str, str]):
    """Export the file's values to this process; variables already set in the shell win."""
    for key, value in env.items():
        os.environ.setdefault(key, value)
    database = Path(os.environ.get('GENE_DB') or '.state/gene.sqlite3')
    os.environ['GENE_DB'] = str(database if database.is_absolute() else ROOT / database)


def dry_run(settings) -> tuple[bool, str]:
    """Start the controller app on a throwaway database and call it in-process, without a socket."""
    import httpx

    from gene.api import create_app
    from gene.fri_protocol import CAPABILITIES, compatible

    async def calls(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://controller') as client:
            health = await client.get('/health')
            contract = await client.get('/fri/contract', headers={
                'Authorization': 'Bearer ' + settings.fri_token})
            status = await client.get('/status', headers={
                'Authorization': 'Bearer ' + settings.runtime_token})
            return health, contract, status

    with tempfile.TemporaryDirectory() as directory:
        app = create_app(replace(settings, db=Path(directory) / 'gene.sqlite3'))
        health, contract, status = asyncio.run(calls(app))
    if health.status_code != 200 or not health.json().get('ok'):
        return False, f'/health answered {health.status_code}'
    if contract.status_code != 200 or not compatible(contract.json()):
        return False, f'/fri/contract answered {contract.status_code} with an incompatible contract'
    if status.status_code != 200:
        return False, f'/status answered {status.status_code}'
    body = contract.json()
    return True, (f"/health ok, {body['protocol']} with {len(body['capabilities'])} capabilities"
                  f' ({len(CAPABILITIES)} required), /status ok')


def check(args):
    env = load(args.env)
    problems, todo = [], []
    tokens = [env.get(key, '') for key in SERVICE_TOKENS]
    short = [key for key, token in zip(SERVICE_TOKENS, tokens) if len(token) < 32]
    if short:
        problems.append(f'{", ".join(short)} must be at least 32 characters; `init` fills empty ones')
    elif len(set(tokens)) != len(tokens):
        problems.append('the four service tokens must all differ')
    for key in ID_KEYS:
        if env.get(key) and not env[key].isdecimal():
            problems.append(f'{key} must be a numeric Discord ID')
    for key in ID_LISTS:
        if env.get(key) and not all(item.strip().isdecimal() for item in env[key].split(',')):
            problems.append(f'{key} must be comma-separated numeric Discord IDs')
    url = env.get('GENE_CONTROLLER_URL', '')
    if url and not url.startswith(('http://', 'https://')):
        problems.append('GENE_CONTROLLER_URL must start with http:// or https://')

    print(f'Checking {args.env}. Nothing leaves this machine and no value is printed.')
    if problems:
        for problem in problems:
            print(f'  error  {problem}')
        raise SystemExit('check: fix the errors above')
    print('  ok     service tokens: four distinct, 32+ characters')

    apply(env)
    from gene.config import Settings
    settings = Settings.from_env()
    ok, detail = dry_run(settings)
    print(f'  {"ok" if ok else "error":6} controller dry run: {detail}')
    if not ok:
        raise SystemExit('check: the controller did not start cleanly with this configuration')
    print(f'  ok     database: {settings.db}')

    missing = [key for key in FRI_IDS if not env.get(key)]
    if missing:
        todo.append(f'pair with Fri: set {", ".join(missing)}')
    else:
        print('  ok     Fri pairing: server, owner and chat channel set; next run `fri-env`')
    missing = [key for key in RED_KEYS if not env.get(key)]
    if importlib.util.find_spec('redbot') is None:
        missing.append('the red extra (uv sync --frozen --extra red)')
    if missing:
        todo.append(f'Red (optional): needs {", ".join(missing)}')
    else:
        print('  ok     Red: bot token, server and owner set; red extra installed')
    for item in todo:
        print(f'  todo   {item}')
    print('check: the controller is ready. Start it with `uv run python scripts/dev.py controller`.')


def controller(args):
    apply(load(args.env))
    if args.host not in LOOPBACK:
        print(f'Warning: {args.host} exposes the controller beyond this machine.', file=sys.stderr)
    import uvicorn

    from gene.api import create_app
    print(f"Gene controller on http://{args.host}:{args.port}, database {os.environ['GENE_DB']}",
          flush=True)
    uvicorn.run(create_app(), host=args.host, port=args.port, access_log=False)


def fri_env(args):
    env = load(args.env)
    missing = [key for key in (*FRI_IDS, 'GENE_FRI_TOKEN') if not env.get(key)]
    if missing:
        raise SystemExit(f'Set {", ".join(missing)} in {args.env} first (then run check).')
    target = args.path.expanduser().resolve()
    if not target.parent.is_dir():
        raise SystemExit(f'{target.parent} does not exist. Clone Fri first, then run fri-env.')
    if target == args.env.resolve():
        raise SystemExit(f'Refusing to write a token into {target}: it is the source env file.')
    values = {'GENE_CONTROLLER_URL': f'http://127.0.0.1:{args.port}',
              **{key: env[key] for key in ('GENE_FRI_TOKEN', *FRI_IDS)}}
    lines = target.read_text().splitlines() if target.exists() else []
    write_tokens(target, merged(lines, values))
    print(f'Updated {", ".join(values)} in {target} (mode 600). The token was not printed.')


def red(args):
    if importlib.util.find_spec('redbot') is None:
        raise SystemExit('Red is not installed here. Run: uv sync --frozen --extra red')
    env = load(args.env)
    missing = [key for key in RED_KEYS if not env.get(key)]
    if missing:
        raise SystemExit(f'Set {", ".join(missing)} in {args.env} first.')
    apply(env)
    state = ROOT / '.state' / 'red'
    home = state / 'home'
    home.mkdir(parents=True, exist_ok=True)
    # Red keeps its instance list in the user's config directory (on macOS, always under HOME).
    # A private HOME keeps it inside this checkout instead of the real home or the image's /data.
    os.environ.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'),
                      GENE_COG_PATH=str(ROOT / 'cogs'),
                      GENE_HEALTH_FILE=str(state / 'gene-health.json'))
    from redbot.core import data_manager
    if not data_manager.config_file.exists():
        subprocess.run([sys.executable, '-m', 'redbot.setup', '--no-prompt', '--instance-name', 'gene',
                        '--data-path', str(state / 'data'), '--backend', 'json'], check=True)
    from gene.runtime import main
    main()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--env', type=Path, default=ENV, help='env file (default: .env.local)')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='create .env.local, or fill in missing service tokens')
    commands.add_parser('check', help='validate .env.local and dry-run the controller offline')
    serve = commands.add_parser('controller', help='run the controller')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8787)
    pair = commands.add_parser('fri-env', help="write a local Fri's controller settings to PATH")
    pair.add_argument('path', type=Path)
    pair.add_argument('--port', type=int, default=8787, help="the controller's local port")
    commands.add_parser('red', help="run Gene's Red bot against the local controller")
    args = parser.parse_args(argv)
    args.env = args.env.expanduser().resolve()
    {'init': init, 'check': check, 'controller': controller, 'fri-env': fri_env,
     'red': red}[args.command](args)


if __name__ == '__main__':
    main()
