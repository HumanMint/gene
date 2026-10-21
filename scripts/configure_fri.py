#!/usr/bin/env python3
"""Configure Fri separately from Gene; credentials are entered only in the terminal."""

import getpass
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line) if path.exists() else {}


def main():
    base = read_env(ROOT / '.env.local')
    destination = ROOT / '.secrets/fri.env'
    previous = read_env(destination)
    token = getpass.getpass('Fri Discord BOT token (hidden; blank keeps saved token): ').strip()
    token = token or previous.get('DISCORD_TOKEN', '')
    if not token or token == base.get('DISCORD_TOKEN'):
        raise SystemExit('Use a new Fri bot application, separate from Gene.')

    def discord(route):
        request = urllib.request.Request('https://discord.com/api/v10' + route,
            headers={'Authorization': 'Bot ' + token, 'User-Agent': 'FriSetup/1.0'})
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.load(response)

    try:
        me = discord('/users/@me')
        if not me.get('bot'):
            raise ValueError('A bot account is required')
        guilds = discord('/users/@me/guilds')
    except (urllib.error.URLError, ValueError):
        raise SystemExit('Fri could not authenticate. Check the bot token.') from None
    values = {key: base.get(key, '') for key in
              ('GENE_GUILD_ID', 'GENE_OWNER_ID', 'GENE_WORKSHOP_CHANNEL_ID')}
    if not all(value.isdecimal() for value in values.values()):
        raise SystemExit('Configure Gene with the Discord Lab server and workshop first.')
    values.update(DISCORD_TOKEN=token, FRI_APPLICATION_ID=me['id'])
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as output:
        for key, value in values.items():
            if any(character in value for character in '\r\n\x00'):
                raise ValueError('Configuration must use single-line values')
            output.write(f'{key}={value}\n')
    print(f'Saved Fri configuration to {destination}. Tokens were not printed.')
    if values['GENE_GUILD_ID'] not in {guild['id'] for guild in guilds}:
        permissions = 1024 + 2048 + 16384 + 32768 + 65536 + 274877906944
        print('Invite Fri to Discord Lab with this URL, then tell Codex setup is ready:')
        print(f'https://discord.com/oauth2/authorize?client_id={me["id"]}'
              f'&scope=bot%20applications.commands&permissions={permissions}'
              f'&guild_id={values["GENE_GUILD_ID"]}&disable_guild_select=true')
    else:
        print('Fri is already in the configured Discord Lab server.')
    print('Fri uses your ChatGPT subscription. Run scripts/connect_fri_chatgpt.py for sign-in.')
    print('Enable Message Content and Server Members Intent for Fri. Presence Intent is not needed to show Fri online.')


if __name__ == '__main__':
    main()
