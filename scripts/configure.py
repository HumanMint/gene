#!/usr/bin/env python3
"""Interactive local credential setup. Tokens are never echoed or sent to GitHub."""

from __future__ import annotations

import getpass
import json
import os
import secrets
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / '.env.local'
    if path.exists():
        answer = input('A local configuration exists. Replace it? [y/N] ').strip().lower()
        if answer != 'y':
            return
    print('Gene setup: use a Discord BOT token from the existing Gene application.')
    token = getpass.getpass('Discord bot token (hidden): ').strip()

    def api(route):
        request = urllib.request.Request('https://discord.com/api/v10' + route,
            headers={'Authorization': 'Bot ' + token, 'User-Agent': 'GeneSetup/0.1'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    try:
        me = api('/users/@me')
        if not me.get('bot'):
            raise ValueError('This is not a bot account')
        print(f"Connected to {me['username']} (bot id {me['id']}).")
        guilds = api('/users/@me/guilds')
        if not guilds:
            print('Invite this bot to the server first:')
            print(f"https://discord.com/oauth2/authorize?client_id={me['id']}"
                  '&scope=bot%20applications.commands&permissions=36785472')
            return
        for index, guild in enumerate(guilds, 1):
            print(f"{index}. {guild['name']} ({guild['id']})")
        selected = int(input('Server number: ') or '1') - 1
        if selected < 0 or selected >= len(guilds):
            raise ValueError('Invalid server number')
        guild_id = guilds[selected]['id']
        guild = api('/guilds/' + guild_id)
        channels = api('/guilds/' + guild_id + '/channels')
    except (urllib.error.HTTPError, ValueError) as exc:
        print(f'Setup stopped: {type(exc).__name__}. Check the token and server access.')
        return

    text_channels = [c for c in channels if c['type'] == 0]
    for channel in text_channels:
        print(f"#{channel['name']}: {channel['id']}")

    def channel_id(prompt, default_name='', optional=False):
        default = next((c['id'] for c in text_channels if c['name'] == default_name), '')
        answer = input(f'{prompt} [{default or "leave empty to disable"}]: ').strip() or default
        if not answer and optional:
            return ''
        if answer not in {c['id'] for c in text_channels}:
            raise ValueError(f'{prompt} must be a text channel in this server')
        return answer

    owner_id = input(f"Your Discord user ID [{guild['owner_id']}]: ").strip() or guild['owner_id']
    if not owner_id.isdecimal():
        raise ValueError('Owner ID must be numeric')
    workshop = channel_id('Workshop channel ID', 'bot-commands')
    history = channel_id('Old bot-commands channel ID', 'bot-commands', optional=True)
    highlights = channel_id('Meme highlight channel ID', 'doin-numbers', optional=True)
    sources = input('Meme source channel IDs, comma separated (empty disables highlights): ').strip()
    if sources and any(c.strip() not in {c['id'] for c in text_channels} for c in sources.split(',')):
        raise ValueError('Meme source IDs must be channels in this server')
    prefix = input('Gene command prefix [!]: ').strip() or '!'
    if len(prefix) > 10 or any(c.isspace() for c in prefix):
        raise ValueError('Use a short command prefix without spaces')
    values = {
        'GENE_GUILD_ID': guild_id, 'GENE_OWNER_ID': owner_id,
        'GENE_WORKSHOP_CHANNEL_ID': workshop, 'GENE_HISTORY_CHANNEL_ID': history,
        'GENE_HIGHLIGHT_CHANNEL_ID': highlights, 'GENE_MEME_SOURCE_CHANNEL_IDS': sources,
        'GENE_HIGHLIGHT_THRESHOLD': '5', 'GENE_PREFIX': prefix, 'DISCORD_TOKEN': token,
        'GENE_RUNTIME_TOKEN': secrets.token_urlsafe(48),
        'GENE_WORKER_TOKEN': secrets.token_urlsafe(48),
        'GENE_DEPLOY_TOKEN': secrets.token_urlsafe(48),
        'GENE_CONTROLLER_URL': 'http://127.0.0.1:8787',
        'GENE_DB': '.state/gene.sqlite3', 'GENE_REPOSITORY': 'HumanMint/gene',
        'GENE_AUDIO_HOST': '', 'GENE_AUDIO_PORT': '2333',
        'GENE_AUDIO_PASSWORD': secrets.token_urlsafe(32),
    }
    # Use a restricted file from its first byte, independent of the shell's umask.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as output:
        for key, value in values.items():
            if '\n' in value or '\r' in value or '\x00' in value:
                raise ValueError('Configuration values must be single lines')
            output.write(f'{key}={value}\n')
    print(f'Saved private configuration to {path}. No token was printed.')
    print('Enable Message Content Intent for Gene in the Discord Developer Portal.')
    print('Keep Server Members and Presence intents off unless a later feature needs them.')


if __name__ == '__main__':
    main()

