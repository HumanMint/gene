#!/usr/bin/env python3
"""Owner-run, channel-scoped, resumable historical specification import."""

import argparse
import json
import os
import sqlite3
import time
from pathlib import Path

import httpx

from gene.history import minimize_message, recover_evidence


def read_env(path):
    return dict(line.split('=', 1) for line in path.read_text().splitlines()
                if line and not line.startswith('#') and '=' in line)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=Path('.env.local'))
    parser.add_argument('--output', type=Path, default=Path('.state/history'))
    parser.add_argument('--delete-evidence', action='store_true', help='Delete the local import and report')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    db_path = args.output / 'evidence.sqlite3'
    report_path = args.output / 'recovery.json'
    if args.delete_evidence:
        db_path.unlink(missing_ok=True)
        report_path.unlink(missing_ok=True)
        print('Deleted local historical evidence and report.')
        return
    env = read_env(args.env)
    channel_id, guild_id = env['GENE_HISTORY_CHANNEL_ID'], env['GENE_GUILD_ID']
    if not channel_id or not guild_id:
        raise SystemExit('Configure the allowed history channel and guild first.')
    os.umask(0o077)
    with httpx.Client(base_url='https://discord.com/api/v10', timeout=30,
        headers={'Authorization': 'Bot ' + env['DISCORD_TOKEN'], 'User-Agent': 'GeneRecovery/0.1'}) as client:
        def get(route, params=None):
            for _ in range(6):
                response = client.get(route, params=params)
                if response.status_code == 429:
                    time.sleep(min(float(response.json().get('retry_after', 5)), 60))
                    continue
                response.raise_for_status()
                return response.json()
            raise RuntimeError('Discord rate limit persisted; rerun to resume later')

        channel = get('/channels/' + channel_id)
        if channel.get('guild_id') != guild_id or channel.get('type') != 0:
            raise SystemExit('The configured import channel is not a text channel in the allowed server.')
        with sqlite3.connect(db_path) as db:
            db.executescript('CREATE TABLE IF NOT EXISTS messages(id TEXT PRIMARY KEY, body TEXT);'
                             'CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT);')
            saved_channel = db.execute('SELECT value FROM state WHERE key="channel"').fetchone()
            if saved_channel and saved_channel[0] != channel_id:
                raise SystemExit('This import belongs to another channel. Use a separate output folder.')
            db.execute('INSERT OR IGNORE INTO state VALUES ("channel",?)', (channel_id,))
            cursor = db.execute('SELECT value FROM state WHERE key="before"').fetchone()
            before = cursor[0] if cursor else str(int(channel.get('last_message_id') or 0) + 1)
            done = db.execute('SELECT 1 FROM state WHERE key="complete"').fetchone()
            while not done:
                page = get(f'/channels/{channel_id}/messages', {'limit': 100, 'before': before})
                if not page:
                    db.execute('INSERT OR REPLACE INTO state VALUES ("complete","true")')
                    db.commit()
                    break
                for message in page:
                    minimized = minimize_message(message, guild_id, channel_id)
                    db.execute('INSERT OR IGNORE INTO messages VALUES (?,?)',
                               (message['id'], json.dumps(minimized)))
                before = str(min(int(m['id']) for m in page))
                db.execute('INSERT OR REPLACE INTO state VALUES ("before",?)', (before,))
                db.commit()
                print(f'Imported {db.execute("SELECT count(*) FROM messages").fetchone()[0]} redacted records.')
                time.sleep(0.5)
            messages = [json.loads(row[0]) for row in db.execute('SELECT body FROM messages')]
    evidence = recover_evidence(messages, env.get('GENE_PREFIX', '!'))
    report_path.write_text(json.dumps({'channel_id': channel_id, 'evidence': evidence,
        'note': 'Review chronologically. Later confirmed settings may supersede earlier ones. '
                'Conflicts and uncertain replies require owner review. No commands were executed.'}, indent=2))
    report_path.chmod(0o600)
    print(f'Saved {len(evidence)} command evidence records to {report_path}. Nothing was executed or posted.')


if __name__ == '__main__':
    main()

