"""Gene-side adapter: current member identity, real Red checks, captured command receipts."""

import asyncio
import copy
import logging
import os
import time

import discord
from redbot.core import commands

from .gene_controls import COMMANDS, arguments, requested
from .privacy import clean

LOG = logging.getLogger('gene.controls')


class CapturedMessage:
    def __init__(self, context):
        self.context, self.id = context, context.message.id

    async def edit(self, **kwargs):
        await self.context.send(**kwargs)
        return self

    async def delete(self, **kwargs):
        pass


class ControlContext(commands.Context):
    async def send(self, content=None, **kwargs):
        transform = kwargs.get('filter')
        if content:
            self.outputs.append(str(transform(content) if transform else content)[:1500])
        for embed in ([kwargs['embed']] if kwargs.get('embed') else kwargs.get('embeds', [])):
            self.outputs.extend(str(part)[:1500] for part in (embed.title, embed.description) if part)
            self.outputs.extend(f'{field.name}: {field.value}'[:1500] for field in embed.fields[:10])
            if embed.footer.text:
                self.outputs.append(str(embed.footer.text)[:1500])
        self.outputs = self.outputs[-12:]
        return CapturedMessage(self)

    async def react_quietly(self, reaction, *, message=None):
        await self.send(message or 'Gene acknowledged the command.')
        return True

    async def send_help(self, command=None):
        await self.send('Gene needs different arguments for that command.')


class ControlRunner:
    def __init__(self, bot):
        self.bot, self.task = bot, None

    def catalog(self):
        result = []
        for action, (name, cog, _) in COMMANDS.items():
            command = self.bot.get_command(name)
            if command and command.enabled and getattr(command.cog, 'qualified_name', '') == cog:
                result.append(action)
        return result

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def run(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            cog = self.bot.get_cog('Gene')
            try:
                if cog:
                    claim = await cog.api('POST', '/runtime/controls/poll', {'actions': self.catalog()})
                    if claim:
                        try:
                            # Keep Red's own operation timeouts and cleanup. Cancelling a loading
                            # track here can strand its queue lock. Fri has a separate bounded wait,
                            # and no other bridge action is claimed until this invocation finishes.
                            result = await self.execute(cog, claim)
                        except (discord.HTTPException, commands.CommandError, ValueError):
                            result = {'uncertain': 'Gene could not verify completion. Check channel, voice and DJ permissions before retrying.'}
                        except Exception:
                            LOG.exception('Gene control did not finish')
                            result = {'uncertain': 'Gene could not confirm the result. Do not retry automatically.'}
                        await cog.api('POST', '/runtime/controls/'+claim['id']+'/finish',
                                      {'lease': claim['lease'], **result})
            except (commands.CommandError, discord.HTTPException):
                LOG.warning('Gene control delivery is temporarily unavailable')
            except Exception as exc:
                LOG.error('Gene control delivery failed (%s); no command will be replayed', type(exc).__name__)
            await asyncio.sleep(2)

    async def execute(self, cog, claim):
        action, argument = claim['action'], claim['argument']
        arguments(action, argument)
        if action not in self.catalog() or not requested(claim['request_text'], action):
            return {'blocked': 'That command is unavailable or was not requested.'}
        source = claim['source']
        event_time = discord.utils.snowflake_time(int(source['id'])).timestamp()
        if source['kind'] != 'discord_message' or not -5 <= time.time()-event_time <= 300:
            return {'blocked': 'The request is too old; send a new message.'}
        if str(cog.guild_id) != claim['guild_id'] or source['user_id'] != claim['user_id']:
            return {'blocked': 'The request belongs to another server or member.'}
        guild = self.bot.get_guild(cog.guild_id)
        member = await guild.fetch_member(int(claim['user_id'])) if guild else None
        channel = self.bot.get_channel(int(claim['target_channel_id']))
        if not member or member.bot or not channel or getattr(channel, 'guild', None) != guild:
            return {'blocked': 'Gene cannot access that member or channel.'}
        for who in (member, guild.me):
            permissions = channel.permissions_for(who)
            if not permissions.view_channel or not permissions.read_message_history or not permissions.send_messages:
                return {'blocked': 'The member or Gene cannot use that text channel.'}
        if source['guild_id'] is not None:
            if source['guild_id'] != claim['guild_id'] or source['channel_id'] != str(channel.id):
                return {'blocked': 'Public commands cannot be redirected to a different channel.'}
            original = await channel.fetch_message(int(source['id']))
            if (original.author.id != member.id or original.webhook_id or
                    clean(original.content, 4000) != claim['request_text']):
                return {'blocked': 'The original request was edited, deleted, or has another author.'}
            message = copy.copy(original)
            message.author = member  # Current roles and voice state, not a stale member snapshot.
        else:
            # Fri has verified its own DM message. Gene separately verifies live server membership.
            # This adapter context is never persisted as a Discord conversation or posted to a channel.
            message = discord.Message(state=self.bot._connection, channel=channel, data={
                'id': source['id'], 'type': 0, 'content': '', 'channel_id': str(channel.id),
                'author': {'id': str(member.id), 'username': member.name, 'discriminator': member.discriminator,
                           'avatar': None}, 'attachments': [], 'embeds': [], 'mentions': [],
                'mention_roles': [], 'pinned': False, 'mention_everyone': False, 'tts': False,
                'edited_timestamp': None})
            message.author = member
        name, _, _ = COMMANDS[action]
        message.content = os.getenv('GENE_PREFIX', '!')+name+(' '+argument if argument else '')
        message.attachments, message.embeds = [], []
        ctx = await self.bot.get_context(message, cls=ControlContext)
        ctx.outputs = []
        expected = self.bot.get_command(name)
        if ctx.command is not self.bot.get_command(name.split()[0]) or expected is None:
            return {'blocked': 'Gene did not resolve the expected command.'}
        await cog.api('POST', '/runtime/controls/'+claim['id']+'/start', {'lease': claim['lease']})
        if action in ('queue', 'now_playing'):
            # Red's text views wait for reaction menus. Preserve prepare/cleanup (including
            # Audio's before hook and channel restrictions) around a bounded player read.
            if not await self.bot.can_run(ctx, call_once=True):
                return {'blocked': 'Gene denied access to that command.'}
            await expected.prepare(ctx)
            try:
                return {'output': self.queue(cog.guild_id, action == 'queue')}
            finally:
                if expected._max_concurrency is not None:
                    await expected._max_concurrency.release(ctx)
                await expected.call_after_hooks(ctx)
        if action in ('pause', 'resume'):
            if (not expected.is_enabled(ctx.guild) or not await self.bot.can_run(ctx, call_once=True)
                    or not await expected.can_run(ctx)):
                return {'blocked': 'Gene denied access to that command.'}
            import lavalink
            try:
                paused = lavalink.get_player(cog.guild_id).paused
                if paused == (action == 'pause'):
                    return {'output': 'Playback is already '+('paused.' if paused else 'resumed.')}
            except (lavalink.NodeNotFound, lavalink.PlayerNotFound):
                pass  # Let Red explain its ordinary no-player state.
        await self.bot.invoke(ctx)  # Includes global checks, command checks, permissions, cooldowns and hooks.
        if ctx.command_failed:
            return {'blocked': 'Gene rejected the command. Its normal permissions and cooldowns still apply.'}
        output = clean('\n'.join(ctx.outputs), 3000)
        return {'output': output} if output else {'uncertain': 'Gene returned no confirmation; do not claim the action succeeded.'}

    @staticmethod
    def queue(guild_id, full):
        import lavalink
        try:
            player = lavalink.get_player(guild_id)
        except (lavalink.NodeNotFound, lavalink.PlayerNotFound):
            return 'Nothing is playing and the queue is empty.'
        text = 'Now playing: '+player.current.title if player.current else 'Nothing is playing.'
        if full:
            text += '\n'+('\n'.join(f'{i+1}. {t.title}' for i, t in enumerate(list(player.queue)[:10])) or 'The queue is empty.')
        return clean(text, 3000)
