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

from .fri_chat import Chat
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

    async def setup_hook(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self.add_view(HomeView(self))
        self.add_dynamic_items(ReviewButton)

        @self.tree.command(name='fri', description='Chat, request a feature or fix, and follow development.',
                           guild=discord.Object(id=self.guild_id))
        async def menu(interaction: discord.Interaction):
            await reply(self, interaction, embed=card('Fri', 'Ideas, fixes, and occasional sarcasm. '
                'Chat here privately, or @mention me in this workshop. I remember your last six exchanges '
                'for up to seven days; private and public chats stay separate.'),
                view=HomeView(self, interaction.user.id))

        await self.tree.sync(guild=discord.Object(id=self.guild_id))
        self.health.start()
        self.owner_reviews.start()

    def dispatch(self, event, /, *args, **kwargs):
        if event == 'interaction' and args:
            interaction = args[0]
            channel = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
            allowed = interaction.guild_id == self.guild_id and channel == self.workshop_id
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

    async def talk(self, guild_id, channel_id, user_id, text, visibility):
        if user_id in self.busy:
            raise commands.UserFeedbackCheckFailure('I’m still replying to you. Give me a moment.')
        if not 1 <= len(text) <= 4000:
            raise commands.UserFeedbackCheckFailure('Keep each message between 1 and 4000 characters.')
        self.busy.add(user_id)
        scope = {'guild_id': str(guild_id), 'user_id': str(user_id), 'channel_id': str(self.workshop_id),
                 'conversation_id': f'{visibility}:{channel_id}'}
        try:
            async with self.chat_slots:
                context = await self.api('POST', '/chat/context', scope)
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
                await self.api('POST', '/chat/turn', {**scope, 'revision': context['revision'],
                    'input': text, 'reply': result.reply, 'model': context['model'], 'effort': context['effort'],
                    'input_tokens': result.counters.get('input_tokens'), 'output_tokens': result.counters.get('output_tokens')})
                return result
        finally:
            self.busy.discard(user_id)

    async def chat_interaction(self, interaction, text):
        await defer_panel(interaction)
        await reply(self, interaction, 'Thinking…', view=HomeView(self, interaction.user.id))
        try:
            result = await self.talk(interaction.guild_id, interaction.channel_id, interaction.user.id, text, 'private')
            view = DraftView(self, interaction.user.id, result.draft) if result.draft else HomeView(self, interaction.user.id)
            await reply(self, interaction, embed=card('Fri', result.reply), view=view)
        except commands.UserFeedbackCheckFailure as exc:
            await reply(self, interaction, str(exc), view=HomeView(self, interaction.user.id))

    async def on_message(self, message):
        if (message.author.bot or not message.guild or message.guild.id != self.guild_id or
                self.user.id not in {user.id for user in message.mentions}):
            return
        if (getattr(message.channel, 'parent_id', None) or message.channel.id) != self.workshop_id:
            return
        text = message.content.replace(f'<@{self.user.id}>', '').replace(f'<@!{self.user.id}>', '').strip()
        try:
            async with message.channel.typing():
                result = await self.talk(message.guild.id, message.channel.id, message.author.id, text, 'public')
            view = DraftView(self, message.author.id, result.draft, public=True) if result.draft else None
            await message.reply(embed=card('Fri', result.reply), view=view, mention_author=False)
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
            'sha': os.getenv('GENE_RELEASE_SHA', 'development'), 'presence': 'invisible'})

    @health.before_loop
    async def before_health(self):
        await self.wait_until_ready()

    async def close(self):
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
