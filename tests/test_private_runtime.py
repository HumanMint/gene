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


def test_gene_runtime_rejects_all_dm_components_after_fri_migration(monkeypatch):
    import discord
    import redbot.__main__ as red_main
    import redbot.core.bot as red_bot
    monkeypatch.setenv('GENE_GUILD_ID', '123')
    monkeypatch.setenv('GENE_OWNER_ID', '10')
    original_red, original_tree = red_main.Red, red_bot.RedTree
    try:
        main = install_private_runtime()
        with patch.object(red_bot.Red, '__init__', return_value=None):
            instance = main.Red()
            instance._connection = SimpleNamespace(user=SimpleNamespace(id=99))
        event = SimpleNamespace(guild_id=None, type=discord.InteractionType.component,
            user=SimpleNamespace(id=10), message=SimpleNamespace(guild=None, author=SimpleNamespace(id=99)),
            data={'custom_id': 'gene:review:i:a:012345abcdef:' + 'a' * 64})
        with patch.object(red_bot.Red, 'dispatch') as dispatch:
            instance.dispatch('interaction', event)
            dispatch.assert_not_called()
            dispatch.reset_mock()
            event.user.id = 11
            instance.dispatch('interaction', event)
            event.user.id = 10
            event.data['custom_id'] = 'gene:home:settings'
            instance.dispatch('interaction', event)
            instance.dispatch('message', SimpleNamespace(guild=None, author=SimpleNamespace(id=10)))
            dispatch.assert_not_called()
    finally:
        red_main.Red, red_bot.RedTree = original_red, original_tree



def test_red_cog_loader_does_not_confuse_gene_cog_with_control_package():
    import os
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    code = '''
import importlib.util
import gene.runtime
spec = importlib.util.spec_from_file_location('gene', 'cogs/gene/__init__.py')
cog = spec.loader.load_module()
assert cog.Gene.__module__ == 'gene.gene'
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=root,
                            env={**os.environ, 'PYTHONPATH': str(root / 'src')}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
