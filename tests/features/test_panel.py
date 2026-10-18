import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.gene.panel import (
    ApprovalView,
    HomeView,
    InputModal,
    ModelsView,
    ScopedView,
    music_command,
)


def environment():
    response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock(),
                               defer=AsyncMock(), send_modal=AsyncMock())
    bot = SimpleNamespace(is_owner=AsyncMock(return_value=True), invoke=AsyncMock(),
                          get_context=AsyncMock(), get_command=Mock())
    cog = SimpleNamespace(guild_id=123, bot=bot, api=AsyncMock())
    user = SimpleNamespace(id=10)
    message = SimpleNamespace(id=5, author=SimpleNamespace(id=99), content='original', attachments=[], embeds=[])
    interaction = SimpleNamespace(guild_id=123, channel_id=456, id=789, user=user,
                                  message=message, response=response, edit_original_response=AsyncMock(),
                                  followup=SimpleNamespace(send=AsyncMock()))
    return cog, interaction


def test_persistent_home_panel_and_all_callbacks_respect_server_boundary():
    async def scenario():
        cog, interaction = environment()
        panel = HomeView(cog)
        assert panel.is_persistent()
        assert {b.label for b in panel.children} >= {'Music', 'Requests', 'Request a change', 'Owner settings'}
        interaction.guild_id = 999
        assert not await panel.interaction_check(interaction)
        interaction.response.send_message.assert_not_awaited()
        cog.api.assert_not_awaited()
    asyncio.run(scenario())


def test_private_views_recheck_identity_and_owner_role_on_every_click():
    async def scenario():
        cog, interaction = environment()
        view = ScopedView(cog, 10, owner=True)
        assert await view.interaction_check(interaction)
        cog.bot.is_owner.return_value = False
        assert not await view.interaction_check(interaction)
        cog.bot.is_owner.return_value = True
        interaction.user.id = 11
        assert not await view.interaction_check(interaction)
        cog.api.assert_not_awaited()
    asyncio.run(scenario())


def test_modal_rechecks_author_and_guild_before_submitting():
    async def scenario():
        cog, interaction = environment()
        modal = InputModal(cog, interaction, 'request')
        interaction.user.id = 77
        assert not await modal.interaction_check(interaction)
        interaction.user.id, interaction.guild_id = 10, 555
        assert not await modal.interaction_check(interaction)
        cog.api.assert_not_awaited()
    asyncio.run(scenario())


def test_music_dispatch_uses_clicker_and_normal_red_pipeline_without_mutating_menu():
    async def scenario():
        cog, interaction = environment()
        command = object()
        ctx = SimpleNamespace(command=command)
        cog.bot.get_context.return_value = ctx
        cog.bot.get_command.return_value = command
        await music_command(cog, interaction, interaction.message, 'play', 'example music')
        invocation = cog.bot.get_context.call_args.args[0]
        assert invocation.author is interaction.user
        assert invocation.id == interaction.id
        assert invocation.content == '!play example music'
        assert interaction.message.author.id == 99
        assert interaction.message.content == 'original'
        assert ctx.interaction is interaction
        cog.bot.invoke.assert_awaited_once_with(ctx)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        with pytest.raises(ValueError):
            await music_command(cog, interaction, interaction.message, 'eval', 'anything')
    asyncio.run(scenario())


def test_approval_button_submits_only_the_complete_commit_shown_for_review():
    async def scenario():
        cog, interaction = environment()
        sha = 'b' * 40
        view = ApprovalView(cog, 10, 'job', sha)
        assert await view.interaction_check(interaction)
        await view.approve.callback(interaction)
        cog.api.assert_awaited_once_with('POST', '/requests/job/approve',
            {'guild_id': '123', 'user_id': '10', 'sha': sha})
        assert view.approve.disabled
        assert interaction.edit_original_response.call_args.kwargs['view'] is view
    asyncio.run(scenario())


def test_old_effort_menu_cannot_silently_revert_a_new_model_selection():
    async def scenario():
        cog, interaction = environment()
        state = {'selected': 'model-a', 'models': [
            {'id': 'model-a', 'name': 'Model A', 'efforts': ['medium', 'high']}]}
        view = ModelsView(cog, 10, state)
        effort = next(item for item in view.children if getattr(item, 'placeholder', '') == 'Choose reasoning effort')
        effort._values = ['high']
        cog.api.return_value = {'selected': 'model-b'}
        await effort.callback(interaction)
        cog.api.assert_awaited_once_with('POST', '/models', {'guild_id': '123', 'user_id': '10'})
    asyncio.run(scenario())
