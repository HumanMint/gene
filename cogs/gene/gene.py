from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import commands

from .panel import HomeView, card, reply, sessions

LOG = logging.getLogger('gene')


class Gene(commands.Cog):
    """Gene's functional tools. Calm, concise, and accountable."""

    def __init__(self, bot):
        self.bot = bot
        self.guild_id = int(os.getenv('GENE_GUILD_ID') or 0)
        self.owner_id = int(os.getenv('GENE_OWNER_ID') or 0)
        self.workshop_id = int(os.getenv('GENE_WORKSHOP_CHANNEL_ID') or 0)
        self.url = os.getenv('GENE_CONTROLLER_URL', 'http://controller:8787').rstrip('/')
        self.token = os.getenv('GENE_RUNTIME_TOKEN', '')
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.health_path = Path(os.getenv('GENE_HEALTH_FILE', '/data/gene-health.json'))
        self.panel = HomeView(self)
        self.bot.add_view(self.panel)
        self.health.start()

    async def cog_unload(self):
        self.health.cancel()
        self.panel.stop()
        await sessions(self).close_all()
        await self.session.close()

    async def cog_check(self, ctx):
        return ctx.guild is not None and ctx.guild.id == self.guild_id

    async def red_delete_data_for_user(self, *, requester, user_id):
        await self.api('POST', '/privacy/erase',
                       {'guild_id': str(self.guild_id), 'user_id': str(user_id)})

    @tasks.loop(seconds=10)
    async def health(self):
        if self.bot.is_ready():
            guild = self.bot.get_guild(self.guild_id)
            state = {'at': time.time(), 'guild_connected': guild is not None and not guild.unavailable,
                     'guild_id': self.guild_id, 'presence': str(self.bot._connection._status),
                     'sha': os.getenv('GENE_RELEASE_SHA', 'development')}
            self.health_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.health_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(state))
            temporary.replace(self.health_path)

    @health.before_loop
    async def before_health(self):
        await self.bot.wait_until_red_ready()

    async def api(self, method, path, payload=None):
        try:
            async with self.session.request(method, self.url + path, json=payload,
                headers={'Authorization': f'Bearer {self.token}'}) as response:
                data = await response.json()
                if response.status >= 400:
                    detail = data.get('detail', 'Controller rejected this request')
                    raise commands.UserFeedbackCheckFailure(str(detail))
                return data
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise commands.UserFeedbackCheckFailure(
                'The workshop controller is unavailable. Gene\'s other commands still work.') from exc

    @staticmethod
    def actor(ctx):
        return {'guild_id': str(ctx.guild.id), 'user_id': str(ctx.author.id)}

    @staticmethod
    async def say(ctx, text):
        # Avoid accidental @everyone, role, or user pings from requests/model output.
        for start in range(0, len(text), 1900):
            await ctx.send(text[start:start + 1900], allowed_mentions=discord.AllowedMentions.none())

    @commands.hybrid_group(name='gene', invoke_without_command=True)
    @commands.guild_only()
    async def gene(self, ctx):
        """Music, server tools, and status."""
        await self.open_panel(ctx)

    async def open_panel(self, ctx):
        if ctx.interaction:
            await reply(self, ctx.interaction, embed=card('Gene', 'Music and server tools. Choose an action below.'),
                        view=HomeView(self, ctx.author.id))
            return
        await ctx.send(embed=card('Gene', 'Music and server tools. Choose an action below. '
            'Your menus and settings are shown only to you.'), view=self.panel,
            allowed_mentions=discord.AllowedMentions.none())

    @gene.command(name='menu')
    async def menu(self, ctx):
        """Open Gene's buttons, menus, and forms."""
        await self.open_panel(ctx)

    @gene.command(name='status')
    async def status(self, ctx):
        """Show the current functional bot release."""
        state = await self.api('GET', '/status')
        await self.say(ctx, f"Gene release: `{state['active_sha']}`. Music and server tools are ready.")

    @gene.command(name='version')
    async def version(self, ctx):
        """Show the running release and selected worker model."""
        state = await self.api('GET', '/status')
        await self.say(ctx, f"Release: `{state['active_sha'] or 'not initialized'}`\n"
                            f"Worker: `{state['model']}`")

    @gene.command(name='forgetme')
    async def forgetme(self, ctx, confirm: bool = False):
        """Erase your stored requests/context and cancel pending work. Pass true to confirm."""
        if not confirm:
            await self.say(ctx, 'Use `gene forgetme true` to erase your request text, links, saved chats and '
                               'context and cancel pending proposals. Code, Discord posts, and backups are separate records.')
            return
        await self.api('POST', '/privacy/erase', self.actor(ctx))
        await self.say(ctx, 'Your stored requests, chats, links, and approved context have been erased.')
