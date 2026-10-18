from __future__ import annotations

import re

from .privacy import clean

FAILED = re.compile(r'(?i)\b(error|failed|not found|unknown command|missing permissions|'
                    r'could not|couldn\x27t|invalid|not loaded|not installed)\b')
CONFIRMED = re.compile(r'(?i)\b(successfully|has been set|have been set|now set|enabled|disabled|'
                       r'loaded|unloaded|installed|added|removed|updated)\b')


def minimize_message(message: dict, guild_id: str, channel_id: str):
    """Keep only redacted evidence needed to recover configuration. No attachments or profiles."""
    return {
        'id': message['id'], 'at': message.get('timestamp', ''),
        'bot': bool(message.get('author', {}).get('bot')),
        'content': clean(message.get('content', ''), 4000),
        'reply_to': message.get('message_reference', {}).get('message_id'),
        'url': f'https://discord.com/channels/{guild_id}/{channel_id}/{message["id"]}',
    }


def recover_evidence(messages: list[dict], prefix: str):
    """Pair commands with explicit bot replies; adjacency alone is not proof of success."""
    ordered = sorted(messages, key=lambda m: int(m['id']))
    replies = {m['reply_to']: m for m in ordered if m['bot'] and m.get('reply_to')}
    evidence = []
    for index, message in enumerate(ordered):
        if message['bot'] or not message['content'].startswith(prefix):
            continue
        command = message['content'][len(prefix):].strip()
        if not command:
            continue
        reply = replies.get(message['id'])
        # Adjacent historical replies are useful evidence but cannot confirm the setting.
        adjacent = ordered[index + 1] if index + 1 < len(ordered) else None
        candidate = reply or (adjacent if adjacent and adjacent['bot'] else None)
        status = 'attempted'
        if candidate and FAILED.search(candidate['content']):
            status = 'failed' if reply else 'uncertain'
        elif reply and CONFIRMED.search(reply['content']):
            status = 'confirmed'
        evidence.append({'command': command, 'family': command.split()[0].lower(),
                         'status': status, 'source': message['url'],
                         'reply': candidate['content'] if candidate else '',
                         'reply_source': candidate['url'] if candidate else None,
                         'requires_owner_review': True})
    return evidence

