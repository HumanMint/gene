"""Pinned Red integration. Apply the server boundary before any cog handles messages."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def allowed_guild(guild_id: int | None) -> bool:
    configured = os.environ.get('GENE_GUILD_ID', '')
    return bool(configured) and str(guild_id) == configured


def configured_presence():
    # Deployment configuration controls visibility; Discord commands cannot change it.
    return 'online' if os.getenv('GENE_PRESENCE') == 'online' else 'invisible'


def install_private_runtime():
    import discord
    import redbot.__main__ as red_main
    import redbot.core.bot as red_bot
    from redbot.core import Config
    from redbot.core.tree import RedTree

    class PrivateTree(RedTree):
        async def interaction_check(self, interaction):
            if not allowed_guild(interaction.guild_id):
                return False
            return await super().interaction_check(interaction)

        async def sync(self, *, guild=None):
            # Never publish workshop commands globally or into another server.
            if guild is None or not allowed_guild(guild.id):
                return []
            return await super().sync(guild=guild)

    class PrivateRed(red_bot.Red):
        def __init__(self, *args, **kwargs):
            # Included in the first gateway IDENTIFY, avoiding a brief online flash.
            kwargs['status'] = discord.Status(configured_presence())
            kwargs['activity'] = None
            super().__init__(*args, **kwargs)

        async def change_presence(self, **kwargs):
            kwargs.update(status=discord.Status(configured_presence()), activity=None)
            return await super().change_presence(**kwargs)

        def dispatch(self, event, /, *args, **kwargs):
            private_events = ('message', 'message_edit', 'message_delete', 'bulk_message_delete',
                              'raw_message_edit', 'raw_message_delete', 'raw_bulk_message_delete',
                              'reaction_add', 'reaction_remove', 'reaction_clear',
                              'raw_reaction_add', 'raw_reaction_remove', 'raw_reaction_clear',
                              'raw_reaction_clear_emoji', 'interaction')
            if event in private_events and args:
                item = args[0]
                if isinstance(item, list):
                    item = item[0] if item else None
                if hasattr(item, 'message'):
                    item = item.message
                guild_id = getattr(item, 'guild_id', None) or getattr(getattr(item, 'guild', None), 'id', None)
                if not allowed_guild(guild_id):
                    return
            return super().dispatch(event, *args, **kwargs)

        async def process_commands(self, message, /):
            if allowed_guild(getattr(message.guild, 'id', None)):
                return await super().process_commands(message)

        async def setup_hook(self):
            if os.getenv('GENE_AUDIO_HOST'):
                audio = Config.get_conf(None, identifier=2711759130, cog_name='Audio')
                await audio.set_raw('use_external_lavalink', value=True)
                await audio.set_raw('host', value=os.environ['GENE_AUDIO_HOST'])
                await audio.set_raw('password', value=os.environ['GENE_AUDIO_PASSWORD'])
                await audio.set_raw('ws_port', value=int(os.getenv('GENE_AUDIO_PORT', '2333')))
                await audio.set_raw('secured_ws', value=False)
                await audio.set_raw('status', value=False)
                await audio.set_raw('global_db_enabled', value=False)
            await super().setup_hook()
            missing = [name for name in self._cli_flags.load_cogs if name not in self.extensions]
            if missing:
                raise RuntimeError('Required Gene cogs did not load: ' + ', '.join(missing))
            cog = self.get_cog('Gene')
            if cog:
                guild = discord.Object(id=int(os.environ['GENE_GUILD_ID']))
                self.tree.add_command(cog.gene.app_command, guild=guild, override=True)
                await self.tree.sync(guild=guild)

    red_bot.RedTree = PrivateTree
    red_main.Red = PrivateRed
    return red_main


def main():
    if not os.getenv('DISCORD_TOKEN') or not os.getenv('GENE_GUILD_ID'):
        raise SystemExit('Discord credentials and the allowed server must be configured')
    os.environ['RED_TOKEN'] = os.environ['DISCORD_TOKEN']
    from .logging import install_secret_filter
    install_secret_filter()
    os.environ.setdefault('XDG_CONFIG_HOME', '/data/config')
    from redbot.core import data_manager
    if not data_manager.config_file.exists():
        subprocess.run(['redbot-setup', '--no-prompt', '--instance-name', 'gene',
                        '--data-path', '/data/red', '--backend', 'json'], check=True)
    cog_path = Path(os.getenv('GENE_COG_PATH', '/app/cogs'))
    cogs = sorted(p.name for p in cog_path.iterdir() if (p / '__init__.py').is_file())
    if os.getenv('GENE_AUDIO_HOST'):
        cogs.append('audio')
    sys.argv = ['redbot', 'gene', '--no-prompt', '--prefix', os.getenv('GENE_PREFIX', '!'),
                '--owner', os.environ['GENE_OWNER_ID'], '--cog-path', str(cog_path),
                '--disable-intent', 'members', '--disable-intent', 'presences',
                '--no-cogs', '--load-cogs', *cogs]
    install_private_runtime().main()


if __name__ == '__main__':
    main()
