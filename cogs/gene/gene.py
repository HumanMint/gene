from __future__ import annotations

import json
import os
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import tasks
from redbot.core import commands


class Gene(commands.Cog):
    """Gene's private workshop. Calm, concise, and accountable."""

    def __init__(self, bot):
        self.bot = bot
        self.guild_id = int(os.getenv('GENE_GUILD_ID') or 0)
        self.workshop_id = int(os.getenv('GENE_WORKSHOP_CHANNEL_ID') or 0)
        self.url = os.getenv('GENE_CONTROLLER_URL', 'http://controller:8787').rstrip('/')
        self.token = os.getenv('GENE_RUNTIME_TOKEN', '')
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self.health_path = Path(os.getenv('GENE_HEALTH_FILE', '/data/gene-health.json'))
        self.health.start()

    async def cog_unload(self):
        self.health.cancel()
        await self.session.close()

    async def cog_check(self, ctx):
        return ctx.guild is not None and ctx.guild.id == self.guild_id

    async def red_delete_data_for_user(self, *, requester, user_id):
        await self.api('POST', '/privacy/erase',
                       {'guild_id': str(self.guild_id), 'user_id': str(user_id)})

    @tasks.loop(seconds=10)
    async def health(self):
        if self.bot.is_ready():
            state = {'at': time.time(), 'guild_connected': self.bot.get_guild(self.guild_id) is not None,
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
        """Requests, status, context, and reviewed releases."""
        await self.say(ctx, 'Gene online. Use `gene request` to propose a change, '
                           '`gene status` to inspect progress, or `help gene` for controls.')

    @gene.command(name='request')
    async def request(self, ctx, *, description: str):
        """Describe a feature or repair in the workshop. This creates a development job."""
        channel = ctx.channel.parent_id if isinstance(ctx.channel, discord.Thread) else ctx.channel.id
        payload = {**self.actor(ctx), 'source_id': str(ctx.message.id),
                   'source_url': ctx.message.jump_url, 'body': description, 'channel_id': str(channel)}
        await ctx.defer()
        job = await self.api('POST', '/requests', payload)
        await self.say(ctx, f"Request `{job['id']}` is {job['state']}. I'll prepare a tested proposal; "
                           'the owner approves each live release.')

    @gene.command(name='status')
    async def status(self, ctx, request_id: str = ''):
        """Show system status or a particular request's proposal."""
        if request_id:
            job = await self.api('GET', '/requests/' + request_id)
            text = f"`{job['id']}` · **{job['state']}**\n{job['body']}"
            if job['proposal']:
                proposal = job['proposal']
                text += f"\n{proposal.get('summary', '')}\nCommit: `{proposal['sha']}`"
                if proposal.get('url'):
                    text += '\n' + proposal['url']
            if job.get('result'):
                text += '\n' + job['result']
        else:
            state = await self.api('GET', '/status')
            text = ('Development paused.' if state['paused'] else 'Development active.')
            text += f"\nRelease: `{state['active_sha'] or 'not initialized'}`\n"
            text += ', '.join(f'{count} {name}' for name, count in state['jobs'].items()) or 'No requests yet.'
        await self.say(ctx, text)

    @gene.command(name='approve')
    @commands.is_owner()
    async def approve(self, ctx, request_id: str, commit_sha: str):
        """Approve exactly the tested commit shown by gene status."""
        await self.api('POST', f'/requests/{request_id}/approve',
                       {**self.actor(ctx), 'sha': commit_sha})
        await self.say(ctx, f'Approved `{request_id}`. Deployment will check health and roll back on failure.')

    @gene.command(name='reject')
    @commands.is_owner()
    async def reject(self, ctx, request_id: str):
        """Reject a queued request or pending proposal."""
        await self.api('POST', f'/requests/{request_id}/reject', self.actor(ctx))
        await self.say(ctx, f'Rejected `{request_id}`.')

    @gene.command(name='pause')
    @commands.is_owner()
    async def pause(self, ctx, paused: bool = True):
        """Pause new coding and release jobs. Use false to resume."""
        await self.api('POST', '/pause', {**self.actor(ctx), 'paused': paused})
        await self.say(ctx, 'New development and deployments paused.' if paused else 'Development resumed.')

    @gene.command(name='rollback')
    @commands.is_owner()
    async def rollback(self, ctx):
        """Queue a return to the previous healthy release."""
        job = await self.api('POST', '/rollback', self.actor(ctx))
        await self.say(ctx, f"Rollback `{job['id']}` queued. If development is paused, resume to apply it.")

    @gene.command(name='usage')
    @commands.is_owner()
    async def usage(self, ctx):
        """Show recorded Codex usage. Cached tokens are a subset of input tokens."""
        rows = await self.api('POST', '/usage', self.actor(ctx))
        text = '\n'.join(f"{r['model']}: {r['attempts']} jobs, input {r['input_tokens']}, "
                         f"output {r['output_tokens']}, cached input {r['cached_input_tokens']}, "
                         f"unknown {r['unknown_attempts']}" for r in rows)
        await self.say(ctx, text or 'No Codex usage recorded yet.')

    @gene.command(name='remember')
    @commands.is_owner()
    async def remember(self, ctx, *, fact: str):
        """Approve a small server fact for future development jobs."""
        result = await self.api('POST', '/memory',
            {**self.actor(ctx), 'body': fact, 'source_url': ctx.message.jump_url})
        await self.say(ctx, f"Saved approved context `{result['id']}`.")

    @gene.command(name='memories')
    @commands.is_owner()
    async def memories(self, ctx):
        """Inspect approved server facts."""
        rows = await self.api('POST', '/memory/list', self.actor(ctx))
        await self.say(ctx, '\n'.join(f"`{r['id']}` {r['body']}" for r in rows) or 'No saved context.')

    @gene.command(name='forget')
    @commands.is_owner()
    async def forget(self, ctx, memory_id: str):
        """Delete an approved fact from future job context."""
        await self.api('POST', '/memory/' + memory_id + '/forget', self.actor(ctx))
        await self.say(ctx, f'Forgot `{memory_id}`. Already running jobs keep their existing input.')

    @gene.command(name='forgetme')
    async def forgetme(self, ctx, confirm: bool = False):
        """Erase your stored requests/context and cancel pending work. Pass true to confirm."""
        if not confirm:
            await self.say(ctx, 'Use `gene forgetme true` to erase your request text, links, and saved '
                               'context and cancel pending proposals. Running jobs have already received '
                               'their inputs. Code, Discord posts, and retained backups are separate records.')
            return
        await self.api('POST', '/privacy/erase', self.actor(ctx))
        await self.say(ctx, 'Your stored request text, links, and approved context have been erased.')
