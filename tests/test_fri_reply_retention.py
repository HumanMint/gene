import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from gene.fri_workspace import execute as execute_workspace
from tests.test_gene_controls import controls_api  # noqa: F401

TARGET_TEXT = 'my recommendation was Paddington'


@pytest.mark.parametrize('kind', ['work_save', 'prepare_post'])
def test_workspace_adapter_itself_refuses_persistent_writes_after_read_evidence(kind):
    async def scenario():
        event = Mock(spec=discord.Message, guild=None, content='save and send this',
            author=SimpleNamespace(id=10), webhook_id=None)
        event.channel = Mock(spec=discord.DMChannel)
        event.channel.fetch_message = AsyncMock(return_value=event)
        actions = SimpleNamespace(event=event, user=10, owner=True, revision=0, scope={}, private_evidence=True,
            fri=SimpleNamespace(api=AsyncMock(return_value={'revision': 0, 'personal_memory': {'enabled': True}})))
        result = await execute_workspace(actions, {'kind': kind})
        assert 'blocked' in result and actions.fri.api.await_count == 1
        assert actions.fri.api.await_args.args[1] == '/chat/context'
    asyncio.run(scenario())
