"""Offline Fri -> controller -> Gene flow; only Discord I/O and Audio playback are substitutes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from tests.legacy_fri.fri_actions import Actions
from tests.legacy_fri.fri_agent import Agent
from tests.test_runtime_controls import runtime


@pytest.mark.parametrize('boundary', ['allowed', 'wrong_parent', 'private', 'member_cannot_send', 'bot_cannot_send'])
def test_thread_control_handoff_and_runtime_scope(tmp_path, boundary):
    async def scenario():
        bot, runner, cog, _, member, parent, calls, message = await runtime()
        parent.nsfw = False
        parent.guild.get_channel = lambda _: parent
        thread = discord.Thread(guild=parent.guild, state=bot._connection, data={
            'id': '999', 'parent_id': '1', 'owner_id': '20', 'name': 'Music thread',
            'type': 11, 'message_count': 1, 'member_count': 2,
            'thread_metadata': {'archived': False, 'auto_archive_duration': 60,
                                'archive_timestamp': '2026-10-01T00:00:00+00:00', 'locked': False}})
        bot.get_channel = lambda cid: {1: parent, 999: thread}.get(cid)
        member.voice = SimpleNamespace(channel=SimpleNamespace(id=8))
        message.channel = thread
        message.author = member
        message.content = 'fri play Sad Machine'
        message.webhook_id = None
        member_perms, bot_perms = discord.Permissions.all(), discord.Permissions.all()
        # Sending in a thread is independent of send_messages in the parent.
        member_perms.send_messages = bot_perms.send_messages = False
        settings = Settings(tmp_path/'controller.sqlite3', '123', '10',
                            'r'*32, 'w'*32, 'd'*32, fri_token='f'*32,
                            workshop_channel_id='1', agent_channels='1')
        with TestClient(create_app(settings)) as client:
            def post(role, path, payload):
                response = client.post(path, json=payload, headers={'Authorization': 'Bearer '+role*32})
                response.raise_for_status()
                return response.json()
            post('r', '/runtime/controls/poll', {'actions': runner.catalog()})
            claims = []
            async def runtime_api(method, path, payload):
                return post('r', path, payload)
            cog.api = runtime_api
            async def fri_api(method, path, payload=None):
                if path.endswith('/result'):
                    claim = post('r', '/runtime/controls/poll', {'actions': runner.catalog()})
                    if claim:
                        claims.append(claim)
                        # Permissions/parent can change after Fri's initial checks.
                        if boundary == 'wrong_parent':
                            thread.parent_id = 2
                        elif boundary == 'private':
                            thread._type = discord.ChannelType.private_thread
                        elif boundary == 'member_cannot_send':
                            member_perms.send_messages_in_threads = False
                        elif boundary == 'bot_cannot_send':
                            bot_perms.send_messages_in_threads = False
                        result = await runner.execute(cog, claim)
                        post('r', f"/runtime/controls/{claim['id']}/finish", {'lease': claim['lease'], **result})
                response = client.request(method, path, json=payload, headers={'Authorization': 'Bearer '+'f'*32})
                response.raise_for_status()
                return response.json()
            fri = SimpleNamespace(guild_id=123, workshop_id=1, owner_id=10,
                                  get_channel=bot.get_channel, api=fri_api)
            agent = Agent.__new__(Agent)
            agent.fri = fri
            agent.state = {'config': {'enabled': True}, 'channels': ['1'], 'history_channels': []}
            agent.member = AsyncMock(return_value=member)
            actions = Actions(agent, 20, 999, True)
            actions.event, actions.message = message, message.content
            actions.scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:999'}
            try:
                with patch.object(discord.Thread, 'permissions_for', side_effect=lambda who: member_perms if who.id == 20 else bot_perms), \
                     patch.object(discord.Thread, 'fetch_message', new=AsyncMock(return_value=message)), \
                     patch('tests.legacy_fri.fri_actions.asyncio.sleep', new=AsyncMock()):
                    assert agent.allowed(thread)
                    assert 'gene_command' in await actions.available()
                    result = await actions.execute({'kind': 'gene_command', 'action': 'play', 'argument': 'Sad Machine'})
                assert len(claims) == 1
                assert claims[0]['channel_id'] == '1' and claims[0]['target_channel_id'] == '999'
                if boundary == 'allowed':
                    assert 'Track Enqueued' in result['output']
                    assert len(calls) == 1 and calls[0][0] is member
                else:
                    assert result['blocked'] and not calls
                parent.send.assert_not_awaited()
            finally:
                await runner.close()
                await bot.close()
    asyncio.run(scenario())
