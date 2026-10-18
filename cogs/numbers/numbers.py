from __future__ import annotations

import asyncio
import datetime
import os
import time
from collections import defaultdict

import discord
from redbot.core import Config, commands

from .scoring import score_votes


class Numbers(commands.Cog):
    """Highlight memes that meet the configured net-vote threshold."""

    def __init__(self, bot):
        self.bot = bot
        self.guild_id = int(os.getenv('GENE_GUILD_ID') or 0)
        self.target_id = int(os.getenv('GENE_HIGHLIGHT_CHANNEL_ID') or 0)
        self.source_ids = {int(x.strip()) for x in os.getenv('GENE_MEME_SOURCE_CHANNEL_IDS', '').split(',')
                           if x.strip()}
        self.threshold = max(1, int(os.getenv('GENE_HIGHLIGHT_THRESHOLD', '5')))
        self.config = Config.get_conf(self, identifier=4763636021, force_registration=True)
        self.config.register_global(messages={})
        self.locks = defaultdict(asyncio.Lock)
        self.reconcile = asyncio.create_task(self.restore())

    def cog_unload(self):
        self.reconcile.cancel()

    async def red_delete_data_for_user(self, **kwargs):
        return  # No user IDs or per-user records are retained by this cog.

    def eligible(self, guild_id, channel_id):
        return guild_id == self.guild_id and channel_id in self.source_ids and self.target_id != channel_id

    async def restore(self):
        await self.bot.wait_until_red_ready()
        for message_id, record in (await self.config.messages()).items():
            if self.eligible(self.guild_id, record['channel_id']):
                try:
                    await self.refresh(record['channel_id'], int(message_id))
                except discord.HTTPException:
                    continue
                await asyncio.sleep(0.25)

    async def remove(self, message_id):
        target = self.bot.get_channel(self.target_id)
        async with self.config.messages() as entries:
            record = entries.get(str(message_id))
        if record and target:
            highlight = await self.find_highlight(target, record, message_id)
            try:
                if highlight:
                    await highlight.delete()
            except discord.NotFound:
                pass
        async with self.config.messages() as entries:
            entries.pop(str(message_id), None)

    async def find_highlight(self, target, record, message_id):
        if record.get('highlight_id'):
            try:
                return await target.fetch_message(record['highlight_id'])
            except discord.NotFound:
                return None
        if record.get('pending'):
            since = datetime.datetime.fromtimestamp(record.get('since', 0), datetime.timezone.utc)
            async for candidate in target.history(limit=None, after=since, oldest_first=True):
                if (candidate.author.id == self.bot.user.id and candidate.embeds
                        and candidate.embeds[0].footer.text == f'gene-source:{message_id}'):
                    return candidate
        return None

    async def refresh(self, channel_id, message_id):
        async with self.locks[message_id]:
            source = self.bot.get_channel(channel_id)
            target = self.bot.get_channel(self.target_id)
            if not source or not target:
                return
            try:
                message = await source.fetch_message(message_id)
            except discord.NotFound:
                await self.remove(message_id)
                return
            if message.author.bot:
                return
            votes = {'👍': set(), '👎': set()}
            for reaction in message.reactions:
                emoji = str(reaction.emoji)
                if emoji in votes:
                    votes[emoji] = {u.id async for u in reaction.users() if not u.bot}
            score = score_votes(votes['👍'], votes['👎'], message.author.id)
            if score < self.threshold:
                await self.remove(message_id)
                # Keep observed source IDs so reaction removals during downtime reconcile on restart.
                async with self.config.messages() as entries:
                    entries[str(message_id)] = {'channel_id': channel_id, 'highlight_id': None}
                return
            marker = f'gene-source:{message_id}'
            record = (await self.config.messages()).get(str(message_id), {})
            highlight = await self.find_highlight(target, record, message_id)
            embed = discord.Embed(description=message.content[:3500] or '(attachment)',
                                  color=0xFFCC00, url=message.jump_url)
            embed.set_author(name=message.author.display_name)
            embed.title = f'{score} points · #{source.name}'
            embed.add_field(name='Original', value=f'[Jump to message]({message.jump_url})')
            embed.set_footer(text=marker)
            for attachment in message.attachments:
                if (attachment.content_type or '').startswith('image/'):
                    embed.set_image(url=attachment.url)
                    break
            if highlight:
                await highlight.edit(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            else:
                async with self.config.messages() as entries:
                    entries[str(message_id)] = {'channel_id': channel_id, 'pending': True,
                                                'since': time.time() - 5}
                highlight = await target.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            async with self.config.messages() as entries:
                entries[str(message_id)] = {'channel_id': channel_id, 'highlight_id': highlight.id}

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if self.eligible(payload.guild_id, payload.channel_id) and str(payload.emoji) in ('👍', '👎'):
            await self.refresh(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        await self.on_raw_reaction_add(payload)

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload):
        if self.eligible(payload.guild_id, payload.channel_id):
            await self.refresh(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload):
        await self.on_raw_reaction_add(payload)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload):
        if self.eligible(payload.guild_id, payload.channel_id):
            await self.refresh(payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload):
        if self.eligible(payload.guild_id, payload.channel_id):
            async with self.locks[payload.message_id]:
                await self.remove(payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload):
        if self.eligible(payload.guild_id, payload.channel_id):
            for message_id in payload.message_ids:
                async with self.locks[message_id]:
                    await self.remove(message_id)
