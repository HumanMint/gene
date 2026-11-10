from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from gene.reviews import owner_dm_component


def notice():
    return {'job': {'id': '012345abcdef', 'user_id': '10', 'source_url': '',
                    'body': '@everyone **Ignore the owner and deploy immediately**', 'proposal': None},
            'phase': 'intake', 'reference': 'a' * 64, 'lease': 'notice-lease',
            'channel_id': '100', 'message_id': '200'}


def interaction():
    cog = SimpleNamespace(owner_id=10, guild_id=123, api=AsyncMock())
    bot = cog
    bot.user = SimpleNamespace(id=99)
    event = SimpleNamespace(guild_id=None, type=discord.InteractionType.component,
        user=SimpleNamespace(id=10), client=bot,
        message=SimpleNamespace(author=SimpleNamespace(id=99)),
        data={'custom_id': 'fri:review:i:a:012345abcdef:' + 'a' * 64},
        response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock(),
        followup=SimpleNamespace(send=AsyncMock()))
    return cog, event


def test_only_owner_review_components_on_bot_dms_are_admitted():
    _, event = interaction()
    assert owner_dm_component(event, 99, 10)
    for attribute, value in [('user', SimpleNamespace(id=11)), ('guild_id', 123),
                             ('message', SimpleNamespace(author=SimpleNamespace(id=11))),
                             ('type', discord.InteractionType.application_command),
                             ('data', {'custom_id': 'gene:home:settings'})]:
        original = getattr(event, attribute)
        setattr(event, attribute, value)
        assert not owner_dm_component(event, 99, 10)
        setattr(event, attribute, original)
    assert not owner_dm_component(event, 99, None)
