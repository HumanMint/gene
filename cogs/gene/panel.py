"""Discord controls for Gene; music runs through Red's normal command pipeline."""

from __future__ import annotations

import copy
import logging
import os

import discord
from redbot.core import commands

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


async def reply(interaction, text=None, **kwargs):
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send(content=text, ephemeral=True, allowed_mentions=NO_MENTIONS, **kwargs)


async def authorized(cog, interaction, user_id=None, owner=False):
    # Component and modal callbacks have their own checks; they do not pass through
    # Red's command checks or the slash-command tree's interaction_check.
    if interaction.guild_id != cog.guild_id:
        return False
    if user_id is not None and interaction.user.id != user_id:
        await reply(interaction, 'This menu belongs to another person. Open your own Gene panel.')
        return False
    if owner and not await cog.bot.is_owner(interaction.user):
        await reply(interaction, 'Only Gene’s owner can use these settings.')
        return False
    return True


class PanelContext(commands.Context):
    async def send(self, content=None, **kwargs):
        kwargs.setdefault('ephemeral', True)
        kwargs['allowed_mentions'] = NO_MENTIONS
        return await super().send(content, **kwargs)

    async def react_quietly(self, reaction, *, message=None):
        # The invocation is an interaction, not a real user message to react to.
        if message:
            await self.send(message)
        return True


async def music_command(cog, interaction, original_message, action, query=''):
    if action not in {'play', 'pause', 'skip', 'stop', 'queue'}:
        raise ValueError('Unsupported music action')
    if not await authorized(cog, interaction):
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    message = copy.copy(original_message)
    message.author, message.id = interaction.user, interaction.id
    message.content = os.getenv('GENE_PREFIX', '!') + action + (' ' + query if query else '')
    message.attachments, message.embeds = [], []
    ctx = await cog.bot.get_context(message, cls=PanelContext)
    ctx.interaction = interaction
    if ctx.command is None or ctx.command is not cog.bot.get_command(action):
        await reply(interaction, 'Music is currently unavailable. Try again shortly.')
        return
    # Do not call command.callback / ctx.invoke: those bypass parts of Red's checks.
    await cog.bot.invoke(ctx)


class ScopedView(discord.ui.View):
    def __init__(self, cog, user_id=None, *, owner=False, timeout=600):
        super().__init__(timeout=timeout)
        self.cog, self.user_id, self.owner = cog, user_id, owner

    async def interaction_check(self, interaction):
        return await authorized(self.cog, interaction, self.user_id, self.owner)

    async def on_error(self, interaction, error, item):
        LOG.warning('Panel action failed: %s', type(error).__name__)
        detail = str(error) if isinstance(error, commands.UserFeedbackCheckFailure) else 'That action did not complete. Try again shortly.'
        await reply(interaction, detail[:1500])


class InputModal(discord.ui.Modal):
    def __init__(self, cog, interaction, kind):
        titles = {'play': 'Add music', 'request': 'Request a change', 'memory': 'Save a server fact'}
        super().__init__(title=titles[kind], timeout=600)
        self.cog, self.kind, self.user_id = cog, kind, interaction.user.id
        self.original = interaction.message
        labels = {'play': 'Song name or YouTube link', 'request': 'What should Gene do or fix?',
                  'memory': 'A small fact Gene should remember'}
        self.value = discord.ui.TextInput(label=labels[kind], max_length=1000 if kind != 'request' else 4000,
                                         style=discord.TextStyle.short if kind == 'play' else discord.TextStyle.paragraph)
        self.add_item(self.value)

    async def interaction_check(self, interaction):
        return await authorized(self.cog, interaction, self.user_id, self.kind == 'memory')

    async def on_submit(self, interaction):
        if self.kind == 'play':
            await music_command(self.cog, interaction, self.original, 'play', self.value.value)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload = {**actor(interaction), 'body': self.value.value,
                   'source_url': source_url(interaction, self.original)}
        if self.kind == 'memory':
            await self.cog.api('POST', '/memory', payload)
            await reply(interaction, 'Saved. Future development jobs can use this fact.')
        else:
            channel = getattr(interaction.channel, 'parent_id', None) or interaction.channel_id
            payload.update(source_id=str(interaction.id), channel_id=str(channel))
            job = await self.cog.api('POST', '/requests', payload)
            await reply(interaction, f"Request `{job['id']}` is queued. Use **Requests** to follow its progress.")

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
        await music_command(self.cog, interaction, interaction.message, 'queue')

    @discord.ui.button(label='Skip', row=1)
    async def skip(self, interaction, button):
        await music_command(self.cog, interaction, interaction.message, 'skip')

    @discord.ui.button(label='Stop', style=discord.ButtonStyle.danger, row=1)
    async def stop_music(self, interaction, button):
        await music_command(self.cog, interaction, interaction.message, 'stop')


async def show_requests(cog, interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    jobs = await cog.api('GET', '/requests')
    if not jobs:
        await reply(interaction, 'No requests yet. Choose **Request a change** to start one.')
        return
    view = ScopedView(cog, interaction.user.id)
    select = discord.ui.Select(placeholder='Choose a request', options=[
        discord.SelectOption(label=f"{j['state']} · {j['body'][:65]}", value=j['id'], description=j['id']) for j in jobs])

    async def selected(event):
        await event.response.defer(ephemeral=True, thinking=True)
        job = await cog.api('GET', '/requests/' + select.values[0])
        description = job['body'][:1500]
        if job.get('model'):
            description += f"\n\nWorker: `{job['model']}` · {job.get('effort', '')}"
        controls = ScopedView(cog, event.user.id)
        proposal = job.get('proposal')
        if proposal:
            description += f"\n\n{proposal.get('summary', '')[:800]}\n\nCommit: `{proposal['sha']}`"
            url = proposal.get('url', '')
            if url.startswith('https://github.com/HumanMint/gene/'):
                controls.add_item(discord.ui.Button(label='View code changes', url=url))
            if job['state'] == 'review' and await cog.bot.is_owner(event.user):
                approve = discord.ui.Button(label='Review release approval', style=discord.ButtonStyle.primary)

                async def review(approval_event):
                    if not await authorized(cog, approval_event, event.user.id, owner=True):
                        return
                    await reply(approval_event, embed=card('Approve this exact commit?',
                        f"Request `{job['id']}`\n\n`{proposal['sha']}`\n\nApprove only after reviewing the code changes. "
                        'Gene will back up state, check the release, and restore the previous code if health checks fail.'),
                        view=ApprovalView(cog, event.user.id, job['id'], proposal['sha']))
                approve.callback = review
                controls.add_item(approve)
        if job.get('result'):
            description += '\n\n' + job['result'][:700]
        if job['state'] in ('queued', 'review') and await cog.bot.is_owner(event.user):
            reject = discord.ui.Button(label='Reject request', style=discord.ButtonStyle.danger)

            async def reject_request(rejection):
                if not await authorized(cog, rejection, event.user.id, owner=True):
                    return
                await rejection.response.defer(ephemeral=True, thinking=True)
                await cog.api('POST', f"/requests/{job['id']}/reject", actor(rejection))
                await reply(rejection, 'Request rejected.')
            reject.callback = reject_request
            controls.add_item(reject)
        await reply(event, embed=card(f"Request · {job['state']}", description), view=controls)
    select.callback = selected
    view.add_item(select)
    await reply(interaction, embed=card('Requests', 'Choose a request to see progress, errors, or its proposed release.'), view=view)


class ApprovalView(ScopedView):
    def __init__(self, cog, user_id, job_id, sha):
        super().__init__(cog, user_id, owner=True)
        self.job_id, self.sha = job_id, sha

    @discord.ui.button(label='Approve this commit', style=discord.ButtonStyle.success)
    async def approve(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        await self.cog.api('POST', f'/requests/{self.job_id}/approve', {**actor(interaction), 'sha': self.sha})
        button.disabled = True
        await interaction.edit_original_response(content='Approved this commit. Follow the release from **Requests**.',
                                                 embed=None, view=self, allowed_mentions=NO_MENTIONS)
        self.stop()


async def show_models(cog, interaction, *, refresh=False, page=0):
    await interaction.response.defer(ephemeral=True, thinking=True)
    state = await cog.api('POST', '/models/refresh' if refresh else '/models', actor(interaction))
    text = f"Selected: **{state['selected']}**\nReasoning: **{state['effort']}**"
    if state['refresh_pending']:
        text += '\n\nCatalog refresh queued. Use **Reload list** after the worker finishes its current job.'
    if state['error']:
        text += '\n\n' + state['error']
    if not state['selected_available']:
        text += '\n\nThe selected model is unavailable. Choose a model to resume coding.'
    await reply(interaction, embed=card('Worker model', text), view=ModelsView(cog, interaction.user.id, state, page))


class ModelsView(ScopedView):
    def __init__(self, cog, user_id, state, page=0):
        super().__init__(cog, user_id, owner=True)
        self.page = page
        models = state['models'][page * 25:(page + 1) * 25]
        if models:
            choose = discord.ui.Select(placeholder='Choose a worker model', row=0, options=[
                discord.SelectOption(label=m['name'][:100], value=m['id'], default=m['id'] == state['selected']) for m in models])

            async def selected(event):
                await event.response.defer(ephemeral=True, thinking=True)
                await cog.api('POST', '/models/select', {**actor(event), 'model_id': choose.values[0], 'effort': ''})
                await reply(event, 'Model updated for new jobs. Reopen **Models** to choose its reasoning effort.')
            choose.callback = selected
            self.add_item(choose)
        current = next((m for m in state['models'] if m['id'] == state['selected']), None)
        if current:
            effort = discord.ui.Select(placeholder='Choose reasoning effort', row=1, options=[
                discord.SelectOption(label='Model default', value='default'),
                *[discord.SelectOption(label=e, value=e) for e in current['efforts']]])

            async def set_effort(event):
                await event.response.defer(ephemeral=True, thinking=True)
                latest = await cog.api('POST', '/models', actor(event))
                if latest['selected'] != state['selected']:
                    await reply(event, 'The selected model changed. Reopen **Models** before changing its effort.')
                    return
                await cog.api('POST', '/models/select', {**actor(event), 'model_id': state['selected'],
                    'effort': '' if effort.values[0] == 'default' else effort.values[0]})
                await reply(event, 'Reasoning effort updated for new jobs.')
            effort.callback = set_effort
            self.add_item(effort)
        for label, target in [('Previous', page - 1), ('Next', page + 1)]:
            if 0 <= target and target * 25 < len(state['models']):
                button = discord.ui.Button(label=label, row=3)

                async def navigate(event, target=target):
                    await show_models(cog, event, page=target)
                button.callback = navigate
                self.add_item(button)

    @discord.ui.button(label='Refresh available models', row=2)
    async def refresh(self, interaction, button):
        await show_models(self.cog, interaction, refresh=True, page=self.page)

    @discord.ui.button(label='Reload list', row=2)
    async def reload(self, interaction, button):
        await show_models(self.cog, interaction, page=self.page)


class SettingsView(ScopedView):
    def __init__(self, cog, user_id):
        super().__init__(cog, user_id, owner=True)

    @discord.ui.button(label='Models', row=0)
    async def models(self, interaction, button):
        await show_models(self.cog, interaction)

    @discord.ui.button(label='Saved context', row=0)
    async def memories(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self.cog.api('POST', '/memory/list', actor(interaction))
        text = '\n\n'.join(row['body'] for row in rows) or 'No saved facts. Gene does not collect ordinary chat.'
        await reply(interaction, embed=card('Approved server context', text), view=ContextView(self.cog, interaction.user.id, rows))

    @discord.ui.button(label='Usage', row=0)
    async def usage(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        rows = await self.cog.api('POST', '/usage', actor(interaction))
        text = '\n\n'.join(f"**{r['model']}** · {r['attempts']} attempts\nInput: {r['input_tokens']} · "
                            f"output: {r['output_tokens']} · cached input: {r['cached_input_tokens']}" for r in rows)
        await reply(interaction, embed=card('Worker usage', text or 'No recorded usage yet.'))

    @discord.ui.button(label='Pause development', row=1)
    async def pause(self, interaction, button):
        await self.set_paused(interaction, True)

    @discord.ui.button(label='Resume development', row=1)
    async def resume(self, interaction, button):
        await self.set_paused(interaction, False)

    async def set_paused(self, interaction, paused):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.api('POST', '/pause', {**actor(interaction), 'paused': paused})
        await reply(interaction, 'New development paused. Running work can finish.' if paused else 'Development resumed.')


class ContextView(ScopedView):
    def __init__(self, cog, user_id, rows):
        super().__init__(cog, user_id, owner=True)
        if rows:
            select = discord.ui.Select(placeholder='Forget a saved fact', row=1, options=[
                discord.SelectOption(label=row['body'][:100], value=row['id']) for row in rows[:25]])

            async def forget(event):
                await event.response.defer(ephemeral=True, thinking=True)
                await cog.api('POST', '/memory/' + select.values[0] + '/forget', actor(event))
                await reply(event, 'Forgot that fact. Already running jobs keep their original input.')
            select.callback = forget
            self.add_item(select)

    @discord.ui.button(label='Add a fact', style=discord.ButtonStyle.primary)
    async def remember(self, interaction, button):
        await interaction.response.send_modal(InputModal(self.cog, interaction, 'memory'))


class HomeView(ScopedView):
    def __init__(self, cog):
        super().__init__(cog, timeout=None)

    @discord.ui.button(label='Music', style=discord.ButtonStyle.primary, custom_id='gene:home:music', row=0)
    async def music(self, interaction, button):
        await reply(interaction, embed=card('Music', 'Join a voice channel, then add a song name or YouTube link. '
            'The usual voice and DJ permissions apply.'), view=MusicView(self.cog, interaction.user.id))

    @discord.ui.button(label='Request a change', style=discord.ButtonStyle.primary, custom_id='gene:home:request', row=0)
    async def request(self, interaction, button):
        await interaction.response.send_modal(InputModal(self.cog, interaction, 'request'))

    @discord.ui.button(label='Requests', custom_id='gene:home:requests', row=1)
    async def requests(self, interaction, button):
        await show_requests(self.cog, interaction)

    @discord.ui.button(label='Status', custom_id='gene:home:status', row=1)
    async def status(self, interaction, button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = await self.cog.api('GET', '/status')
        text = ('Development paused.' if state['paused'] else 'Development active.')
        text += f"\nWorker: **{state['model']}**\nRelease: `{state['active_sha']}`"
        text += '\n\n' + (', '.join(f'{n} {s}' for s, n in state['jobs'].items()) or 'No requests yet.')
        await reply(interaction, embed=card('Gene status', text))

    @discord.ui.button(label='Owner settings', custom_id='gene:home:settings', row=1)
    async def settings(self, interaction, button):
        if await authorized(self.cog, interaction, owner=True):
            await reply(interaction, embed=card('Owner settings', 'Choose a setting below.'),
                        view=SettingsView(self.cog, interaction.user.id))
