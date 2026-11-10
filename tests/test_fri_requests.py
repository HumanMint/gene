from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from gene.previews import pages


def setup(user=10, guild=None):
    cog = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, user=SimpleNamespace(id=99), api=AsyncMock(),
                          agent=SimpleNamespace(member=AsyncMock(return_value=object()), allowed=lambda c: c.id == 456))
    event = SimpleNamespace(user=SimpleNamespace(id=user), guild_id=guild, channel_id=456, channel=SimpleNamespace(id=456),
        type=discord.InteractionType.component, data={'custom_id': 'fri:request:preview:'+'a'*32},
        message=SimpleNamespace(author=cog.user, flags=SimpleNamespace(ephemeral=False)),
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock())
    return cog, event


@pytest.mark.parametrize('body', ['🎮'*4000, '\\*' * 2000, '@everyone ' * 400])
def test_preview_handles_discord_unicode_limits_and_untrusted_formatting(body):
    for page in pages(body):
        assert len(page.encode('utf-16-le'))//2 < 1500
        assert '@everyone' not in page
