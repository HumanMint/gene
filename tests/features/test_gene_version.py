import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redbot.core import commands

from cogs.gene.gene import Gene


def environment():
    # Skip initialization: tests must not start health tasks or HTTP sessions.
    cog = object.__new__(Gene)
    cog.guild_id = 123
    cog.api = AsyncMock()
    ctx = SimpleNamespace(guild=SimpleNamespace(id=123), send=AsyncMock())
    return cog, ctx


@pytest.mark.parametrize('sha', ['a' * 40, '', None])
def test_version_reports_only_status_release_and_selected_model(sha):
    async def scenario():
        cog, ctx = environment()
        cog.api.return_value = {
            'active_sha': sha, 'model': 'test-model',
            'previous_sha': 'b' * 40, 'effort': 'high', 'jobs': {'queued': 2},
            'extra': 'must not be displayed',
        }
        await Gene.version.callback(cog, ctx)
        cog.api.assert_awaited_once_with('GET', '/status')
        ctx.send.assert_awaited_once()
        args, kwargs = ctx.send.call_args
        assert args == (f"Release: `{sha or 'not initialized'}`\nWorker: `test-model`",)
        mentions = kwargs['allowed_mentions']
        assert not mentions.everyone
        assert not mentions.users
        assert not mentions.roles
        assert not mentions.replied_user
    asyncio.run(scenario())


def test_version_propagates_controller_failure_without_claiming_success():
    async def scenario():
        cog, ctx = environment()
        cog.api.side_effect = commands.UserFeedbackCheckFailure('Controller unavailable')
        with pytest.raises(commands.UserFeedbackCheckFailure, match='Controller unavailable'):
            await Gene.version.callback(cog, ctx)
        ctx.send.assert_not_awaited()
        cog.api.side_effect = None
        cog.api.return_value = {'active_sha': 'c' * 40, 'model': 'recovered-model'}
        await Gene.version.callback(cog, ctx)
        assert 'recovered-model' in ctx.send.call_args.args[0]
    asyncio.run(scenario())


def test_version_is_registered_as_a_hybrid_subcommand():
    assert isinstance(Gene.version, commands.HybridCommand)
    assert Gene.gene.get_command('version') is Gene.version
    assert Gene.version.app_command is not None


@pytest.mark.parametrize('guild_id, allowed', [(123, True), (456, False), (None, False)])
def test_cog_check_restricts_version_to_configured_server(guild_id, allowed):
    cog, ctx = environment()
    ctx.guild = SimpleNamespace(id=guild_id) if guild_id is not None else None
    assert asyncio.run(cog.cog_check(ctx)) is allowed


@pytest.mark.parametrize('name', [
    'approve', 'models', 'model', 'reject', 'pause', 'rollback', 'usage',
    'remember', 'memories', 'forget',
])
def test_version_does_not_restore_controls_moved_to_fri(name):
    assert Gene.gene.get_command(name) is None
