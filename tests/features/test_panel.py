import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.gene.panel import (
    HomeView,
    InputModal,
    MusicView,
    PanelContext,
    ScopedView,
    music_command,
    reply,
    sessions,
    show_queue,
)
from gene.fri_panel import ApprovalView, ModelsView


def environment(*, ephemeral=False, message_id=5, interaction_id=789):
    response = SimpleNamespace(is_done=Mock(return_value=False), send_message=AsyncMock(),
                               defer=AsyncMock(), send_modal=AsyncMock())
    async def defer(**kwargs):
        response.is_done.return_value = True
    response.defer.side_effect = defer
    bot = SimpleNamespace(is_owner=AsyncMock(return_value=True), invoke=AsyncMock(),
                          get_context=AsyncMock(), get_command=Mock())
    cog = SimpleNamespace(guild_id=123, workshop_id=456, bot=bot, api=AsyncMock())
    user = SimpleNamespace(id=10)
    message = SimpleNamespace(id=message_id, flags=SimpleNamespace(ephemeral=ephemeral),
                              author=SimpleNamespace(id=99), content='original', attachments=[], embeds=[])
    result = SimpleNamespace(id=message_id if ephemeral else 1000 + interaction_id)
    interaction = SimpleNamespace(guild_id=123, channel_id=456, channel=SimpleNamespace(id=456), id=interaction_id, user=user,
                                  message=message, response=response, edit_original_response=AsyncMock(),
                                  delete_original_response=AsyncMock(),
                                  followup=SimpleNamespace(send=AsyncMock()))
    interaction.edit_original_response.return_value = result
    return cog, interaction


def test_persistent_home_panel_and_all_callbacks_respect_server_boundary():
    async def scenario():
        cog, interaction = environment()
        panel = HomeView(cog)
        assert panel.is_persistent()
        assert {b.label for b in panel.children} >= {'Music', 'Status'}
        assert 'Ask Fri' not in {b.label for b in panel.children}
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


def test_navigation_and_music_confirmations_reuse_one_private_message():
    async def scenario():
        cog, public = environment()
        await HomeView(cog).music.callback(public)
        entry = sessions(cog).entries[(123, 10)]
        assert entry.message_id == 1789
        public.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        _, click = environment(ephemeral=True, message_id=1789, interaction_id=790)
        ctx = SimpleNamespace(panel_cog=cog, interaction=click, author=click.user)
        await PanelContext.send(ctx, 'Added a song.')
        await PanelContext.send(ctx, 'Queue updated.', delete_after=10)
        assert sessions(cog).entries[(123, 10)].message_id == 1789
        assert isinstance(sessions(cog).entries[(123, 10)].view, MusicView)
        click.response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
        click.followup.send.assert_not_awaited()
        click.response.send_message.assert_not_awaited()
        click.delete_original_response.assert_not_awaited()
        assert click.edit_original_response.call_args.kwargs['content'] == 'Queue updated.'
        assert entry.view.is_finished()
    asyncio.run(scenario())


def test_reopening_panel_deletes_previous_and_old_timeout_cannot_delete_replacement():
    async def scenario():
        cog, first = environment()
        old_view = MusicView(cog, 10)
        await reply(cog, first, 'First', view=old_view)
        _, second = environment(interaction_id=800)
        new_view = MusicView(cog, 10)
        await reply(cog, second, 'Second', view=new_view)
        first.delete_original_response.assert_awaited_once()
        assert old_view.is_finished()
        await old_view.on_timeout()
        second.delete_original_response.assert_not_awaited()
        await new_view.on_timeout()
        second.delete_original_response.assert_awaited_once()
        assert not sessions(cog).entries
    asyncio.run(scenario())


def test_close_dismisses_only_the_callers_panel_and_late_results_do_not_reopen_it():
    async def scenario():
        cog, first = environment()
        view = MusicView(cog, 10)
        await reply(cog, first, 'First', view=view)
        _, close = environment(ephemeral=True, message_id=1789, interaction_id=800)
        await next(b for b in view.children if b.label == 'Close').callback(close)
        close.delete_original_response.assert_awaited_once()
        await reply(cog, first, 'Late result', view=MusicView(cog, 10))
        assert first.edit_original_response.await_count == 1
        assert not sessions(cog).entries
    asyncio.run(scenario())


def test_queue_uses_red_player_state_with_buttons_instead_of_ephemeral_reactions(monkeypatch):
    async def scenario():
        cog, event = environment(ephemeral=True)
        def track(title):
            return SimpleNamespace(title=title)
        monkeypatch.setattr('lavalink.get_player', Mock(return_value=SimpleNamespace(
            current=track('Current song'), queue=[track('Queued ' + str(i)) for i in range(14)])))
        await show_queue(cog, event)
        payload = event.edit_original_response.call_args.kwargs
        assert 'Current song' in payload['embeds'][0].description
        assert '10. Queued 9' in payload['embeds'][0].description
        assert {b.label for b in payload['view'].children} >= {'Next page', 'Home', 'Close'}
        cog.bot.invoke.assert_not_awaited()
        event.followup.send.assert_not_awaited()
    asyncio.run(scenario())


def test_late_reply_preserves_newer_controls_and_other_members_panels():
    async def scenario():
        cog, old = environment(ephemeral=True, message_id=100)
        await reply(cog, old, 'Starting', view=MusicView(cog, 10))
        _, newer = environment(ephemeral=True, message_id=100, interaction_id=800)
        current = HomeView(cog, 10)
        await reply(cog, newer, 'Home', view=current)
        await reply(cog, old, 'Slow old result')
        assert not current.is_finished()
        assert old.edit_original_response.await_count == 1
        _, another = environment(interaction_id=810)
        another.user.id = 20
        await reply(cog, another, 'Other member', view=MusicView(cog, 20))
        newer.delete_original_response.assert_not_awaited()
        assert set(sessions(cog).entries) == {(123, 10), (123, 20)}
    asyncio.run(scenario())
