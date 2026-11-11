from types import SimpleNamespace
from unittest.mock import Mock

import discord


def event(user=10, channel=456, guild=123, name='member', kind='message', content='yo'):
    author = SimpleNamespace(id=user, display_name=name)
    if kind == 'interaction':
        return Mock(spec=discord.Interaction, id=100000000000000001, user=author,
                    channel_id=channel, guild_id=guild)
    return Mock(spec=discord.Message, id=100000000000000001, author=author, content=content,
                channel=SimpleNamespace(id=channel), guild=SimpleNamespace(id=guild) if guild else None)
