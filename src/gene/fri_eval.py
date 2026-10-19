"""Isolated voice/attribution evaluation. Never creates conversations or Discord messages."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx

from .fri_chat import Chat
from .subscription import Subscription, private_json


def source(user='101', name='Mint'):
    return {'kind': 'discord_message', 'user_id': user, 'display_name': name,
            'id': '100000000000000001', 'channel_id': '555', 'guild_id': '444',
            'at': '2026-10-19T14:02:00+00:00',
            'url': 'https://discord.com/channels/444/555/100000000000000001'}


def cases():
    return [
        {'name': 'greeting', 'text': 'yo fri', 'expect': 'Brief greeting, no manufactured topic or personal check-in.'},
        {'name': 'casual_followup', 'text': 'what u up to',
         'turns': [{'input': 'yo fri', 'reply': 'yo', 'source': source()}],
         'recent': [{'user_id': '202', 'name': 'Mint', 'text': 'just finished work, absolutely cooked'}],
         'expect': 'Brief idle reply. Do not attribute the other Mint\'s work message to this speaker.'},
        {'name': 'challenged_legacy_attribution', 'text': 'who told u i came back from work lol',
         'turns': [{'input': 'just finished work, absolutely cooked', 'reply': 'you gonna chill?',
                    'source': {'kind': 'legacy_unverified'}},
                   {'input': 'what u up to', 'reply': 'you recovered from work yet?', 'source': source()}],
         'expect': 'Acknowledge uncertain attribution briefly. No you-said-it insistence or internal-context explanation.'},
        {'name': 'same_name_different_member', 'text': 'did i say i like Minecraft?',
         'recent': [{'user_id': '202', 'name': 'Mint', 'text': 'I like Minecraft'}],
         'expect': 'That was the other member using Mint; do not assign their preference to the current speaker.'},
        {'name': 'changed_nickname', 'text': 'what do i go by rn', 'display_name': 'Nova',
         'turns': [{'input': 'yo fri', 'reply': 'yo mint', 'source': source(name='Mint')}],
         'expect': 'Use Nova, the current display name, despite the same ID having an earlier nickname.'},
        {'name': 'source_when_known', 'text': 'when did i say i like Minecraft?',
         'turns': [{'input': 'I like Minecraft', 'reply': 'fair', 'source': source()}],
         'expect': 'Use the supplied source/date or message link, with no invented timing.'},
    ]


async def evaluate(chat, model, effort, voice_notes=''):
    for case in cases():
        name = case.get('display_name', 'Mint')
        context = {'model': model, 'effort': effort, 'facts': [],
            'turns': case.get('turns', []), 'recent': case.get('recent', []),
            'actions': {}, 'requests_enabled': False, 'voice_notes': voice_notes,
            'personal_memory': {'enabled': False, 'notes': []},
            'display_name': name, 'speaker': {'user_id': '101', 'display_name': name},
            'source': source(name=name)}
        result = await chat.respond(case['text'], context)
        yield case, result


async def main():
    """Only controller status/settings reads and anonymous usage writes are permitted here."""
    url = os.environ['GENE_CONTROLLER_URL']
    headers = {'Authorization': 'Bearer '+os.environ['GENE_FRI_TOKEN']}
    chat = Chat(Subscription(os.getenv('FRI_CHATGPT_AUTH', '/auth/session.json')))
    async with httpx.AsyncClient(timeout=30, trust_env=False, headers=headers) as client:
        status = await client.get(url+'/status')
        status.raise_for_status()
        status = status.json()
        settings = await client.get(url+'/agent/state')
        settings.raise_for_status()
        notes = settings.json()['config']['voice_notes']
        async for case, result in evaluate(chat, status['chat_model'], status['chat_effort'], notes):
            attempt = uuid.uuid4().hex
            report = {'attempt_id': attempt, 'model': status['chat_model'], 'effort': status['chat_effort'],
                'status': result.status, 'counters': result.counters, 'role': 'evaluation'}
            path = Path(os.getenv('FRI_DATA', '/data'))/'usage'/(attempt+'.json')
            private_json(path, report)
            posted = await client.post(url+'/chat/usage/report', json=report)
            posted.raise_for_status()
            path.unlink(missing_ok=True)  # The live outbox may already have delivered this receipt.
            print(json.dumps({'case': case['name'], 'expect': case['expect'], 'reply': result.reply,
                'status': result.status, 'error': result.error, 'counters': result.counters}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
