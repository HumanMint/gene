import asyncio
from collections import defaultdict
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from cogs.numbers.numbers import Numbers


class Memory:
    def __init__(self):
        self.data = {}

    def __call__(self):
        return self

    def __await__(self):
        async def read():
            return deepcopy(self.data)
        return read().__await__()

    async def __aenter__(self):
        return self.data

    async def __aexit__(self, *args):
        return False


class Reaction:
    def __init__(self, emoji, users):
        self.emoji = emoji
        self.people = users

    async def users(self):
        for user in self.people:
            yield SimpleNamespace(id=user, bot=False)


def environment():
    cog = object.__new__(Numbers)
    memory = Memory()
    cog.config = SimpleNamespace(messages=memory)
    cog.locks = defaultdict(asyncio.Lock)
    cog.target_id = 20
    cog.threshold = 2
    message = SimpleNamespace(id=30, author=SimpleNamespace(id=99, display_name='Author', bot=False),
        content='a meme', jump_url='https://discord.com/channels/1/10/30', attachments=[],
        reactions=[Reaction('👍', [1, 2, 3])])
    post = SimpleNamespace(id=40, edit=AsyncMock(), delete=AsyncMock(), author=SimpleNamespace(id=50), embeds=[])
    source = SimpleNamespace(name='memes', fetch_message=AsyncMock(return_value=message))
    target = SimpleNamespace(send=AsyncMock(return_value=post), fetch_message=AsyncMock(return_value=post))
    cog.bot = SimpleNamespace(user=SimpleNamespace(id=50), get_channel=lambda cid: {10: source, 20: target}[cid])
    return cog, memory, message, post, source, target


def test_duplicate_events_and_restart_edit_one_persistent_highlight():
    async def scenario():
        cog, memory, message, post, source, target = environment()
        await asyncio.gather(cog.refresh(10, 30), cog.refresh(10, 30))
        target.send.assert_awaited_once()
        assert memory.data['30']['highlight_id'] == 40
        cog.locks = defaultdict(asyncio.Lock)  # restart; persistent state remains
        await cog.refresh(10, 30)
        assert target.send.await_count == 1
        assert post.edit.await_count == 2
    asyncio.run(scenario())


def test_vote_removal_and_original_deletion_remove_highlight():
    async def scenario():
        cog, memory, message, post, source, target = environment()
        await cog.refresh(10, 30)
        message.reactions = [Reaction('👍', [1])]
        await cog.refresh(10, 30)
        post.delete.assert_awaited_once()
        assert memory.data['30']['highlight_id'] is None
        source.fetch_message.side_effect = discord.NotFound(SimpleNamespace(status=404, reason='gone'), 'gone')
        await cog.refresh(10, 30)
        assert '30' not in memory.data
    asyncio.run(scenario())


def test_crash_after_send_recovers_post_even_if_vote_score_falls():
    async def scenario():
        cog, memory, message, post, source, target = environment()
        embed = discord.Embed()
        embed.set_footer(text='gene-source:30')
        post.embeds = [embed]
        memory.data['30'] = {'channel_id': 10, 'pending': True, 'since': 100}

        async def history(**kwargs):
            assert kwargs['limit'] is None
            yield post

        target.history = history
        await cog.refresh(10, 30)
        target.send.assert_not_awaited()
        assert memory.data['30']['highlight_id'] == 40
        memory.data['30'] = {'channel_id': 10, 'pending': True, 'since': 100}
        message.reactions = []
        await cog.refresh(10, 30)
        post.delete.assert_awaited_once()
    asyncio.run(scenario())
