from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gene.runtime import allowed_guild, install_private_runtime


def test_only_configured_lab_is_allowed(monkeypatch):
    monkeypatch.setenv('GENE_GUILD_ID', '123')
    assert allowed_guild(123)
    assert not allowed_guild(456)
    assert not allowed_guild(None)
    monkeypatch.delenv('GENE_GUILD_ID')
    assert not allowed_guild(123)


def test_initial_identify_and_presence_updates_are_invisible(monkeypatch):
    import asyncio

    import discord
    import redbot.__main__ as red_main
    import redbot.core.bot as red_bot
    original_red, original_tree = red_main.Red, red_bot.RedTree
    try:
        main = install_private_runtime()
        with patch.object(red_bot.Red, '__init__', return_value=None) as init:
            instance = main.Red()
            assert init.call_args.kwargs['status'] == discord.Status.invisible
        with patch.object(red_bot.Red, 'change_presence', new_callable=AsyncMock) as change:
            asyncio.run(instance.change_presence(status=discord.Status.online))
            assert change.call_args.kwargs['status'] == discord.Status.invisible
    finally:
        red_main.Red, red_bot.RedTree = original_red, original_tree


def test_other_server_and_dm_events_never_reach_cogs(monkeypatch):
    import redbot.__main__ as red_main
    import redbot.core.bot as red_bot
    monkeypatch.setenv('GENE_GUILD_ID', '123')
    original_red, original_tree = red_main.Red, red_bot.RedTree
    try:
        main = install_private_runtime()
        with patch.object(red_bot.Red, '__init__', return_value=None):
            instance = main.Red()
        with patch.object(red_bot.Red, 'dispatch') as dispatch:
            instance.dispatch('message', SimpleNamespace(guild=SimpleNamespace(id=456)))
            instance.dispatch('message', SimpleNamespace(guild=None))
            instance.dispatch('raw_reaction_add', SimpleNamespace(guild_id=456))
            assert not dispatch.called
            instance.dispatch('message', SimpleNamespace(guild=SimpleNamespace(id=123)))
            dispatch.assert_called_once()
    finally:
        red_main.Red, red_bot.RedTree = original_red, original_tree

