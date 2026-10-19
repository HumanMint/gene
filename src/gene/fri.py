"""Fri's separate Discord identity; trusted UI, bounded chat, one controller authority."""

import asyncio
import json
import logging
import os
import time
import uuid
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from redbot.core import commands

from .fri_addressing import addressed_text
from .fri_chat import Chat, ChatResult
from .fri_memory import describe_memory, memory_intent
from .fri_panel import DraftView, HomeView, authorized, card, reply, sessions
from .fri_reviews import ReviewButton, notify_owner
from .panels import defer_panel
from .privacy import clean
from .reviews import owner_dm_component
from .subscription import Subscription, private_json

LOG = logging.getLogger('fri')
NO_MENTIONS = discord.AllowedMentions.none()


class FriTree(app_commands.CommandTree):
    async def interaction_check(self, interaction):
        return await authorized(self.client, interaction)

    async def on_error(self, interaction, error):
        LOG.warning('Fri command failed: %s', type(error).__name__)
        await reply(self.client, interaction, 'That action did not complete. Please try again.')


class Fri(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = intents.guild_messages = intents.dm_messages = True
        intents.message_content = True
        super().__init__(intents=intents, status=discord.Status.invisible,
                         allowed_mentions=NO_MENTIONS, max_messages=None)
        self.bot = self
        self.guild_id = int(os.environ['GENE_GUILD_ID'])
        self.owner_id = int(os.environ['GENE_OWNER_ID'])
        self.workshop_id = int(os.environ['GENE_WORKSHOP_CHANNEL_ID'])
        self.url = os.getenv('GENE_CONTROLLER_URL', 'http://controller:8787').rstrip('/')
        self.token = os.environ['GENE_FRI_TOKEN']
        self.data = Path(os.getenv('FRI_DATA', '/data'))
        self.chat = Chat(Subscription(os.getenv('FRI_CHATGPT_AUTH', '/auth/session.json')))
        self.tree = FriTree(self)
        self.busy = set()
        self.usage_lock = asyncio.Lock()
        self.chat_slots = asyncio.Semaphore(2)
        self.session = None
        self.agent = None

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        from .fri_agent import Agent
        self.agent = Agent(self)
        await self.agent.start()
        self.add_view(HomeView(self))
        self.add_dynamic_items(ReviewButton)

        @self.tree.command(name='fri', description='Chat, request a feature or fix, and follow development.',
                           guild=discord.Object(id=self.guild_id))
        async def menu(interaction: discord.Interaction):
            await reply(self, interaction, embed=card('Fri', 'Say “fri”, @mention me, or reply to one of my messages '
                'in this workshop. This panel is here for private chat and settings. '
                'I can keep small preferences you share; ask what I remember or tell me to forget you.'),
                view=HomeView(self, interaction.user.id))

        await self.tree.sync(guild=discord.Object(id=self.guild_id))
        self.health.start()
        self.owner_reviews.start()

    def dispatch(self, event, /, *args, **kwargs):
        if event == 'interaction' and args:
            interaction = args[0]
            channel = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
            allowed = interaction.guild_id == self.guild_id and (channel == self.workshop_id or
                (self.agent and self.agent.allowed(interaction.channel)))
            behavior = (interaction.user.id == self.owner_id and
                str((interaction.data or {}).get('custom_id', '')).startswith('fri:behavior:') and
                getattr(getattr(interaction, 'message', None), 'author', None) and
                interaction.message.author.id == getattr(self.user, 'id', None))
            if behavior:
                allowed = True
            if not allowed and not owner_dm_component(interaction, getattr(self.user, 'id', None), self.owner_id):
                return
        super().dispatch(event, *args, **kwargs)

    async def is_owner(self, user):
        return user.id == self.owner_id

    async def api(self, method, path, payload=None):
        try:
            async with self.session.request(method, self.url + path, json=payload,
                    headers={'Authorization': 'Bearer ' + self.token}) as response:
                data = await response.json()
                if response.status >= 400:
                    raise commands.UserFeedbackCheckFailure(str(data.get('detail', 'Controller rejected this request')))
                return data
        except (aiohttp.ClientError, TimeoutError):
            raise commands.UserFeedbackCheckFailure('The workshop controller is temporarily unavailable.') from None

    async def flush_usage(self):
        async with self.usage_lock:
            for path in sorted((self.data / 'usage').glob('*.json')):
                await self.api('POST', '/chat/usage/report', json.loads(path.read_text()))
                path.unlink()

    async def talk(self, guild_id, channel_id, user_id, text, visibility, display_name=''):
        channel = self.get_channel(channel_id) if self.agent else None
        source_channel = (getattr(channel, 'parent_id', None) or channel_id) if visibility == 'public' and self.agent else self.workshop_id
        scope = {'guild_id': str(guild_id), 'user_id': str(user_id), 'channel_id': str(source_channel),
                 'conversation_id': f'{visibility}:{channel_id}'}
        # Privacy controls must work while inference is busy or the subscription is down.
        intent = memory_intent(text)
        if intent == 'show':
            context = await self.api('POST', '/chat/context', scope)
            profile = context['personal_memory']
            profile['notes'] = [{**note, 'evidence': discord.utils.escape_markdown(note['evidence'])}
                                for note in profile['notes']]
            return ChatResult(reply=describe_memory(profile), status='completed')
        if intent in ('disable', 'enable'):
            await self.api('POST', '/chat/memory/control', {**scope, 'action': intent})
            if self.agent:
                self.agent.forget(user_id)
            text = ('Done. I erased our saved chats and personal notes, and turned memory off. '
                    'Say “remember me again” if you change your mind. Discord messages and backups are separate.'
                    if intent == 'disable' else 'Memory’s back on. I’ll pick up small preferences as we talk.')
            return ChatResult(reply=text, status='completed')
        if user_id in self.busy:
            raise commands.UserFeedbackCheckFailure('I’m still replying to you. Give me a moment.')
        if not 1 <= len(text) <= 4000:
            raise commands.UserFeedbackCheckFailure('Keep each message between 1 and 4000 characters.')
        self.busy.add(user_id)
        try:
            if self.agent and self.agent.config['enabled']:
                admission = await self.api('POST', '/agent/admit', {**self.agent.identity(user_id),
                    'space': 'dm' if visibility == 'private' else 'public'})
                if not admission['allowed']:
                    minutes = max(1, int((admission['until']-time.time()+59)//60))
                    notice = (f'brb, virtual cigarette break. back in about {minutes} min. '
                              'daniel’s token stash needs a breather.') if admission.get('notice') else ''
                    return ChatResult(reply=notice, status='cooldown')
            async with self.chat_slots:
                context = await self.api('POST', '/chat/context', scope)
                if self.agent:
                    context.update(await self.agent.extra_context(channel_id, user_id, visibility == 'public'))
                context['display_name'] = clean(display_name, 80)
                text = clean(text, 4000)
                result = await self.chat.respond(text, context)
                attempt = uuid.uuid4().hex
                private_json(self.data / 'usage' / (attempt + '.json'), {
                    'attempt_id': attempt, 'model': context['model'], 'effort': context['effort'],
                    'status': result.status, 'counters': result.counters})
                try:
                    await self.flush_usage()
                except commands.UserFeedbackCheckFailure:
                    LOG.warning('Usage is saved locally; controller delivery will retry')
                if result.error:
                    raise commands.UserFeedbackCheckFailure(result.error)
                saved = await self.api('POST', '/chat/turn', {**scope, 'revision': context['revision'],
                    'input': text, 'reply': result.reply, 'model': context['model'], 'effort': context['effort'],
                    'notes': result.notes,
                    'input_tokens': result.counters.get('input_tokens'), 'output_tokens': result.counters.get('output_tokens')})
                if saved.get('first_memory'):
                    result.reply += ('\n\n-# I kept a small preference from this. Ask “what do you remember about me?” '
                                     'or say “forget me” any time.')
                return result
        finally:
            self.busy.discard(user_id)

    async def chat_interaction(self, interaction, text):
        await defer_panel(interaction)
        await reply(self, interaction, 'Thinking…', view=HomeView(self, interaction.user.id))
        try:
            result = await self.talk(interaction.guild_id, interaction.channel_id, interaction.user.id, text, 'private',
                                     interaction.user.display_name)
            view = DraftView(self, interaction.user.id, result.draft) if result.draft else HomeView(self, interaction.user.id)
            await reply(self, interaction, embed=card('Fri', result.reply), view=view)
        except commands.UserFeedbackCheckFailure as exc:
            await reply(self, interaction, str(exc), view=HomeView(self, interaction.user.id))

    async def on_message(self, message):
        if self.agent:
            try:
                await self.agent.handle(message)
            except (commands.UserFeedbackCheckFailure, discord.HTTPException):
                LOG.warning('Fri interaction could not complete')
            return
        if (message.author.bot or getattr(message, 'webhook_id', None) or not message.guild or
                message.guild.id != self.guild_id):
            return
        if (getattr(message.channel, 'parent_id', None) or message.channel.id) != self.workshop_id:
            return
        text = await addressed_text(message, self.user.id)
        if text is None:
            return
        try:
            async with message.channel.typing():
                result = await self.talk(message.guild.id, message.channel.id, message.author.id, text, 'public',
                                         message.author.display_name)
            view = DraftView(self, message.author.id, result.draft, public=True) if result.draft else None
            await message.reply(result.reply, view=view, mention_author=False, allowed_mentions=NO_MENTIONS,
                                suppress_embeds=True)
        except commands.UserFeedbackCheckFailure as exc:
            await message.reply(str(exc)[:1800], mention_author=False)

    @tasks.loop(seconds=15)
    async def owner_reviews(self):
        try:
            await self.flush_usage()
            await notify_owner(self)
        except (commands.UserFeedbackCheckFailure, aiohttp.ClientError, TimeoutError):
            LOG.warning('Owner notification or usage delivery pending')

    @owner_reviews.before_loop
    async def before_reviews(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=10)
    async def health(self):
        guild = self.get_guild(self.guild_id)
        private_json(self.data / 'fri-health.json', {'at': time.time(),
            'guild_connected': bool(self.is_ready() and guild and not guild.unavailable),
            'sha': os.getenv('GENE_RELEASE_SHA', 'development'), 'guild_id': self.guild_id,
            'presence': str(self._connection._status)})

    @health.before_loop
    async def before_health(self):
        await self.wait_until_ready()

    async def close(self):
        if self.agent:
            self.agent.close()
        self.health.cancel()
        self.owner_reviews.cancel()
        await sessions(self).close_all()
        if self.session:
            await self.session.close()
        await super().close()


def main():
    from .logging import install_secret_filter
    install_secret_filter()
    Fri().run(os.environ['DISCORD_TOKEN'], log_level=logging.INFO)


if __name__ == '__main__':
    main()
