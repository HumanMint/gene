"""Discord controls for Gene; music runs through Red's normal command pipeline."""

from __future__ import annotations

import copy
import logging
import os

import discord
from redbot.core import commands

from .panel_session import PrivatePanels, defer_panel

LOG = logging.getLogger('gene.panel')
NO_MENTIONS = discord.AllowedMentions.none()


def actor(interaction):
    return {'guild_id': str(interaction.guild_id), 'user_id': str(interaction.user.id)}


def source_url(interaction, message):
    if getattr(getattr(message, 'flags', None), 'ephemeral', False):
        return ''
    return f'https://discord.com/channels/{interaction.guild_id}/{interaction.channel_id}/{message.id}'


def card(title, description):
    return discord.Embed(title=title, description=description[:4000], color=0x5865F2)


def sessions(cog):
    if not hasattr(cog, 'private_panels'):
        cog.private_panels = PrivatePanels()
    return cog.private_panels


async def reply(cog, interaction, text=None, **kwargs):
    panels = sessions(cog)
    view = kwargs.pop('view', None) or panels.current_view(interaction) or ScopedView(cog, interaction.user.id)
    return await panels.show(interaction, content=text, view=view, **kwargs)


async def notice(interaction, text):
    if interaction.response.is_done():
        message = await interaction.followup.send(content=text, ephemeral=True, allowed_mentions=NO_MENTIONS, wait=True)
        await message.delete(delay=10)
    else:
        await interaction.response.send_message(content=text, ephemeral=True, allowed_mentions=NO_MENTIONS, delete_after=10)


async def authorized(cog, interaction, user_id=None, owner=False):
    # Component and modal callbacks have their own checks; they do not pass through
    # Red's command checks or the slash-command tree's interaction_check.
    if interaction.guild_id != cog.guild_id:
        return False
    if user_id is not None and interaction.user.id != user_id:
        await notice(interaction, 'This menu belongs to another person. Open your own Gene panel.')
        return False
    if owner and not await cog.bot.is_owner(interaction.user):
        await notice(interaction, 'Only Gene’s owner can use these settings.')
        return False
    return True


class PanelContext(commands.Context):
    async def send(self, content=None, **kwargs):
        transform = kwargs.pop('filter', None)
        if transform and content:
            content = transform(str(content))
        self.panel_sent = True
        # Red's play confirmations and errors replace the panel's content too.
        # Delayed deletes must not erase a later action that reuses this message.
        return await reply(self.panel_cog, self.interaction, content,
                           embed=kwargs.get('embed'), embeds=kwargs.get('embeds'),
                           view=MusicView(self.panel_cog, self.author.id))

    async def react_quietly(self, reaction, *, message=None):
        # The invocation is an interaction, not a real user message to react to.
        if message:
            await self.send(message)
        return True


async def music_command(cog, interaction, original_message, action, query=''):
    if action not in {'play', 'pause', 'skip', 'stop'}:
        raise ValueError('Unsupported music action')
    if not await authorized(cog, interaction):
        return
    await defer_panel(interaction)
    message = copy.copy(original_message)
    message.author, message.id = interaction.user, interaction.id
    message.content = os.getenv('GENE_PREFIX', '!') + action + (' ' + query if query else '')
    message.attachments, message.embeds = [], []
    ctx = await cog.bot.get_context(message, cls=PanelContext)
    ctx.interaction = interaction
    ctx.panel_cog, ctx.panel_sent = cog, False
    if ctx.command is None or ctx.command is not cog.bot.get_command(action):
        await reply(cog, interaction, 'Music is currently unavailable. Try again shortly.')
        return
    # Do not call command.callback / ctx.invoke: those bypass parts of Red's checks.
    await cog.bot.invoke(ctx)
    if not ctx.panel_sent and not getattr(ctx, 'command_failed', False):
        await reply(cog, interaction, 'Music updated.', view=MusicView(cog, interaction.user.id))


class ScopedView(discord.ui.View):
    def __init__(self, cog, user_id=None, *, owner=False, timeout=600, navigation=True):
        super().__init__(timeout=timeout)
        self.cog, self.user_id, self.owner = cog, user_id, owner
        if user_id is not None:
            if navigation:
                home = discord.ui.Button(label='Home', row=4)

                async def go_home(interaction):
                    await reply(cog, interaction, embed=card('Gene', 'Music and server tools. Choose an action below.'),
                                view=HomeView(cog, user_id))
                home.callback = go_home
                self.add_item(home)
            close = discord.ui.Button(label='Close', row=4)

            async def dismiss(interaction):
                await defer_panel(interaction)
                panels = sessions(cog)
                entry = panels.entries.get(panels.key(interaction))
                if entry and entry.view is self:
                    entry.interaction = interaction
                await panels.close(panels.key(interaction), self)
            close.callback = dismiss
            self.add_item(close)

    async def interaction_check(self, interaction):
        return await authorized(self.cog, interaction, self.user_id, self.owner)

    async def on_timeout(self):
        if self.user_id is not None:
            await sessions(self.cog).close((self.cog.guild_id, self.user_id), self)

    async def on_error(self, interaction, error, item):
        LOG.warning('Panel action failed: %s', type(error).__name__)
        detail = str(error) if isinstance(error, commands.UserFeedbackCheckFailure) else 'That action did not complete. Try again shortly.'
        await reply(self.cog, interaction, detail[:1500])


class InputModal(discord.ui.Modal):
    def __init__(self, cog, interaction, kind='play'):
        super().__init__(title='Add music', timeout=600)
        self.cog, self.user_id, self.original = cog, interaction.user.id, interaction.message
        self.value = discord.ui.TextInput(label='Song name or YouTube link', max_length=1000)
        self.add_item(self.value)

    async def interaction_check(self, interaction):
        return await authorized(self.cog, interaction, self.user_id)

    async def on_submit(self, interaction):
        await music_command(self.cog, interaction, self.original, 'play', self.value.value)

    async def on_error(self, interaction, error):
        await ScopedView.on_error(self, interaction, error, None)


class MusicView(ScopedView):
    @discord.ui.button(label='Add a track', style=discord.ButtonStyle.primary, row=0)
    async def play(self, interaction, button):
        await interaction.response.send_modal(InputModal(self.cog, interaction, 'play'))

    @discord.ui.button(label='Pause / resume', row=0)
    async def pause(self, interaction, button):
        await music_command(self.cog, interaction, interaction.message, 'pause')

    @discord.ui.button(label='Queue', row=0)
    async def queue(self, interaction, button):
        await show_queue(self.cog, interaction)

    @discord.ui.button(label='Skip', row=1)
    async def skip(self, interaction, button):
        await music_command(self.cog, interaction, interaction.message, 'skip')

    @discord.ui.button(label='Stop', style=discord.ButtonStyle.danger, row=1)
    async def stop_music(self, interaction, button):
        await music_command(self.cog, interaction, interaction.message, 'stop')


async def show_queue(cog, interaction, page=0):
    """Present Red's existing queue without its text command's reaction-only UI."""
    import lavalink

    await defer_panel(interaction)
    view = MusicView(cog, interaction.user.id)
    try:
        player = lavalink.get_player(interaction.guild_id)
    except (lavalink.NodeNotFound, lavalink.PlayerNotFound):
        await reply(cog, interaction, 'Nothing is playing yet. Add a track to get started.', view=view)
        return
    queue = list(player.queue)
    page = min(max(0, page), max(0, (len(queue) - 1) // 10))
    current = player.current
    text = ('Now playing: **' + discord.utils.escape_markdown(current.title) + '**' if current else 'Nothing is playing.')
    text += '\n\n' + ('\n'.join(f'{index + 1}. {discord.utils.escape_markdown(track.title)}'
        for index, track in enumerate(queue[page * 10:(page + 1) * 10], start=page * 10)) or 'The queue is empty.')
    for label, target in [('Previous page', page - 1), ('Next page', page + 1)]:
        if 0 <= target and target * 10 < len(queue):
            button = discord.ui.Button(label=label, row=3)

            async def navigate(event, target=target):
                await show_queue(cog, event, target)
            button.callback = navigate
            view.add_item(button)
    await reply(cog, interaction, embed=card('Music queue', text), view=view)


class HomeView(ScopedView):
    def __init__(self, cog, user_id=None):
        super().__init__(cog, user_id, timeout=None if user_id is None else 600, navigation=False)
        if os.getenv('GENE_SHOW_FRI', 'false') != 'true':
            self.remove_item(self.fri)

    @discord.ui.button(label='Music', style=discord.ButtonStyle.primary, custom_id='gene:home:music', row=0)
    async def music(self, interaction, button):
        await reply(self.cog, interaction, embed=card('Music', 'Join a voice channel, then add a song name or YouTube link. '
            'The usual voice and DJ permissions apply.'), view=MusicView(self.cog, interaction.user.id))

    @discord.ui.button(label='Ask Fri', custom_id='gene:home:fri', row=0)
    async def fri(self, interaction, button):
        await reply(self.cog, interaction, 'Open **/fri** in the workshop to chat, request a feature or fix, and follow development.')

    @discord.ui.button(label='Status', custom_id='gene:home:status', row=1)
    async def status(self, interaction, button):
        await defer_panel(interaction)
        state = await self.cog.api('GET', '/status')
        text = f"Release: `{state['active_sha']}`\nMusic and server tools are ready."
        await reply(self.cog, interaction, embed=card('Gene status', text))
