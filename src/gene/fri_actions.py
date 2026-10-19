"""One bounded conversational action. Discord text never selects authority or scope."""

import datetime as dt
import time

import discord
from redbot.core import commands

from .fri_web import lookup, public_query
from .privacy import clean


class Actions:
    def __init__(self, agent, user, channel, public):
        self.agent, self.fri = agent, agent.fri
        self.user, self.channel, self.public = user, channel, public
        self.guild = agent.channel_guild(channel) if public else self.fri.guild_id
        self.owner = user == self.fri.owner_id and not public
        self.sources, self.revisions, self.overview = set(), {}, None
        self.message, self.model, self.effort = '', '', ''

    async def channels(self, fresh=False):
        member = await self.agent.member(self.user, fresh=fresh, guild_id=self.guild)
        if not member or not self.agent.config['enabled']:
            return {}
        ids = [str(self.channel)] if self.public else list(dict.fromkeys(
            self.agent.state['history_channels'] + self.agent.state['channels'] + [str(self.fri.workshop_id)]))
        result = {}
        for channel_id in ids[:10]:
            if not channel_id.isdigit():
                continue
            channel = self.fri.get_channel(int(channel_id))
            if (not channel or getattr(getattr(channel, 'guild', None), 'id', None) != self.guild or
                    not self.agent.allowed(channel) or getattr(channel, 'nsfw', False) or
                    (isinstance(channel, discord.Thread) and (channel.is_private() or getattr(channel.parent, 'nsfw', True)))):
                continue
            permissions = channel.permissions_for(member)
            if permissions.view_channel and permissions.read_message_history:
                result[str(channel.id)] = channel
        return result

    async def available(self):
        channels = await self.channels()
        if not channels:
            return {}
        actions = {'read_recent': {'channels': [{'channel_id': k, 'name': clean(v.name, 60)}
                                                for k, v in channels.items()],
                    'default_channel_id': next(iter(channels)), 'limit': 20, 'max_age_days': 7,
                    'description': 'Read one small recent text batch when asked. No images or saved personal notes.'}}
        if self.owner:
            actions['learning_overview'] = {'description': 'Read the latest learning reports and summaries. channel_id="".'}
            actions['run_learning'] = {'description': 'Run one learning pass only when explicitly requested, within the existing budget. channel_id="".'}
        if self.agent.state.get('web_search_enabled'):
            actions['web_search'] = {'description': 'Look up current public facts with live web access and sources. '
                'query must be a short exact excerpt of the current message, with no private details. One lookup per reply.'}
        return actions

    def scoped_overview(self, overview, channels):
        # The owner still needs current channel access. Reports without a channel only describe failures.
        return {**overview,
            'reports': [r for r in overview['reports'] if not r.get('channel_id') or r['channel_id'] in channels],
            'summaries': [r for r in overview['summaries'] if r['channel_id'] in channels],
            'history_channels': [c for c in overview['history_channels'] if c in channels]}

    async def execute(self, action):
        channels = await self.channels(fresh=True)
        kind = action.get('kind')
        if not channels:
            return {'blocked': 'No approved channels are currently readable.'}
        if kind == 'web_search':
            if not self.agent.state.get('web_search_enabled') or not public_query(action.get('query'), self.message):
                return {'blocked': 'Provide a short public topic in the current message; private context cannot be sent to web search.'}
            result, counters, status = await lookup(self.fri.chat, action['query'], self.model, self.effort)
            await self.agent.record('web_search', self.model, self.effort, counters, status)
            return result
        if kind in ('learning_overview', 'run_learning'):
            if not self.owner:
                return {'blocked': 'Learning administration is available only to the verified owner in private.'}
            receipt = None
            if kind == 'run_learning':
                if not set(self.agent.state['history_channels']) <= set(channels):
                    return {'blocked': 'Some configured learning channels are not currently readable.'}
                claim = await self.fri.api('POST', '/agent/learning/request', self.agent.identity(self.user))
                if 'blocked' in claim:
                    return claim
                receipt = await self.agent.learn(claim)
            overview = await self.fri.api('POST', '/agent/learning/overview', self.agent.identity(self.user))
            self.overview = self.scoped_overview(overview, channels)
            self.sources.update(self.overview['history_channels'])
            return {'kind': kind, 'completion': receipt, 'evidence': self.overview}
        if kind != 'read_recent':
            return {'blocked': 'That action is not available.'}
        channel_id = action.get('channel_id') or next(iter(channels))
        if channel_id not in channels:
            return {'blocked': 'That channel is outside this conversation’s readable scope.'}
        channel = channels[channel_id]
        self.sources.add(channel_id)
        scope = getattr(channel, 'parent_id', None) or channel.id
        after = time.time()-min(7, self.agent.config['history_days'])*86400
        if channel.id == self.agent.dedicated:
            after = max(after, self.agent.state.get('started', time.time()))
        records, scanned, profiles = [], 0, {}
        async for message in channel.history(limit=20, after=dt.datetime.fromtimestamp(after, dt.timezone.utc), oldest_first=False):
            scanned += 1
            if message.author.bot or message.webhook_id or not message.content.strip():
                continue
            user = message.author.id
            if user not in profiles:
                profiles[user] = await self.agent.profile(scope, user)
            profile = profiles[user]
            if not profile['enabled']:
                continue
            self.revisions[(scope, user)] = profile['revision']
            records.append({'source': message.jump_url, 'user_id': user, 'name': clean(message.author.display_name, 60),
                            'at': message.created_at.isoformat(), 'text': clean(message.content, 500)})
        return {'kind': kind, 'channel_id': channel_id, 'channel_name': clean(channel.name, 60),
                'scanned': scanned, 'messages': list(reversed(records)), 'lookback_floor': after,
                'images_inspected': False, 'personal_notes_saved': 0}

    async def valid(self):
        """Fail closed if access or source privacy changed while the model was answering."""
        try:
            channels = await self.channels(fresh=True)
            if not channels or not self.sources <= set(channels):
                return False
            for (scope, user), revision in self.revisions.items():
                profile = await self.agent.profile(scope, user)
                if not profile['enabled'] or profile['revision'] != revision:
                    return False
            if self.overview is not None:
                latest = self.scoped_overview(await self.fri.api('POST', '/agent/learning/overview',
                    self.agent.identity(self.user)), channels)
                # A new report may arrive during inference; all evidence used must still exist unchanged.
                for key in ('reports', 'summaries'):
                    if any(row not in latest[key] for row in self.overview[key]):
                        return False
            return True
        except (commands.UserFeedbackCheckFailure, discord.HTTPException):
            return False
