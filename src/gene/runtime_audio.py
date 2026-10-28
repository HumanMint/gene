"""Prepare selected YouTube audio before Red's Lavalink transport starts playback.

A pinned-client adapter, installed outside Red. No player/queue implementation is
replaced. Stop, skip, disconnect, pause and seek remain effective during preparation.
Player.skip is wrapped only to drop Red's automatic skip after a failed preparation once a
newer play or stop has superseded the failed track (gene #75).
"""

import asyncio
import contextvars
import logging
import os
import re
from urllib.parse import parse_qs, urlsplit

import httpx

LOG = logging.getLogger('gene.audio.prepare')
# Red shows the member a refusal's fixed line from the resolver's code (its X-Gene-Refusal
# header). Nothing else the resolver or YouTube says reaches Red, logs or the member.
REFUSALS = {
    'too_long': "That track is longer than 30 minutes, so Gene can't play it. Pick a shorter one.",
    'too_large': "That track's audio is over Gene's 64 MiB limit, so it can't play. Pick a shorter one.",
    'unsupported': "Gene plays ordinary public YouTube tracks only, not live streams. Pick another one.",
}
FAILED = 'The audio download could not be prepared. Try again shortly or choose another track.'
# The failed play whose Track Error is being handled. lavalink's dispatch starts its listener
# tasks inside report_failure's call, so only those tasks (Red's handler among them) carry it.
HANDLING = contextvars.ContextVar('gene_audio_failed_play', default=None)


def youtube_id(track):
    url = urlsplit(track.uri or '')
    if url.scheme != 'https' or url.hostname not in {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'}:
        return None
    identifier = url.path.strip('/') if url.hostname == 'youtu.be' else parse_qs(url.query).get('v', [''])[0]
    return identifier if re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier) else None


class Refused(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def report_failure(node, guild_id, reason=None):
    """Hand Red's Audio cog a track exception: it posts the message as a Track Error, then skips."""
    from lavalink.enums import ExceptionSeverity, LavalinkEvents, LavalinkIncomingOp
    node.event_handler(LavalinkIncomingOp.EVENT, LavalinkEvents.TRACK_EXCEPTION, {
        'guildId': str(guild_id), 'exception': {
            'message': REFUSALS.get(reason, FAILED), 'cause': 'Audio preparation failed before playback',
            # Parsed by red-lavalink's ExceptionSeverity, which has no lowercase values.
            'severity': ExceptionSeverity.COMMON.value}})


class AudioPreparation:
    def __init__(self, url, token, guild_id, *, transport=None):
        if url not in {'http://gene-audio:2334', 'http://127.0.0.1:2334'} or len(token) < 32:
            raise ValueError('Audio preparation requires the private resolver and its service token')
        self.url, self.token, self.guild_id = url, token, guild_id
        self.transport = transport  # Offline tests only; production uses httpx's network transport.
        self.pending = {}

    async def prepare(self, identifier):
        async with httpx.AsyncClient(timeout=httpx.Timeout(95, connect=5), trust_env=False,
                                     transport=self.transport) as client:
            response = await client.post(self.url+'/prepare', json={'identifier': identifier},
                                         headers={'Authorization': 'Bearer '+self.token})
            reason = response.headers.get('x-gene-refusal')
            if response.status_code == 422 and reason in REFUSALS:
                raise Refused(reason)
            response.raise_for_status()
            if response.json().get('ready') is not True:
                raise ValueError('Audio preparation was not confirmed')

    def install(self, node_class, player_class=None):
        original_play, original_send = node_class.no_stop_play, node_class.send
        pending, latest = self.pending, {}  # latest: each player's newest play marker, cleared on stop.

        async def send(node, data):
            key = (node, int(data.get('guildId', 0)))
            if data.get('op') in ('stop', 'destroy'):
                latest.pop(key, None)
            current = pending.get(key)
            if current:
                if data.get('op') in ('stop', 'destroy'):
                    pending.pop(key, None)
                elif data.get('op') == 'pause':
                    current['pause'] = data['pause']
                elif data.get('op') == 'seek':
                    current['start'] = int(data['position'])
            return await original_send(node, data)

        async def play(node, guild_id, track, replace=True, start=0, pause=False):
            key = (node, guild_id)
            marker = {'pause': pause, 'start': start, 'track': track}
            pending[key] = latest[key] = marker  # A newer skip/play supersedes an older download.
            identifier = youtube_id(track)
            try:
                if guild_id == self.guild_id and identifier:
                    try:
                        async with asyncio.timeout(100):
                            await self.prepare(identifier)
                    except (httpx.HTTPError, TimeoutError, ValueError) as exc:
                        if pending.get(key) is marker:
                            reason = exc.reason if isinstance(exc, Refused) else None
                            status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                            # Only a fixed code, status or exception class: never URLs or upstream text.
                            LOG.warning('Selected audio preparation failed (%s); playback was not started',
                                        reason or status or type(exc).__name__)
                            handling = HANDLING.set((key, marker))
                            try:
                                report_failure(node, guild_id, reason)
                            finally:
                                HANDLING.reset(handling)
                        return
                if pending.get(key) is marker:
                    return await original_play(node, guild_id, track, replace=replace,
                                               start=marker['start'], pause=marker['pause'])
            finally:
                if pending.get(key) is marker:
                    pending.pop(key, None)

        node_class.no_stop_play, node_class.send = play, send
        if player_class is None:
            return
        original_skip = player_class.skip

        async def skip(player):
            failed = HANDLING.get()
            if failed is None:
                return await original_skip(player)
            HANDLING.set(None)  # Only the handler's own skip for this failure is checked.
            key, marker = failed
            newer = latest.get(key)
            if newer is marker:
                return await original_skip(player)
            # A member's skip, play or stop superseded the failed track while Red reported it.
            # Red's repeat path cleared player.current: point it back at the track now playing.
            if player.current is None and newer:
                player.current = newer['track']
            LOG.info('A newer play superseded the failed track; its automatic skip was not applied')

        player_class.skip = skip


def install_audio_preparation(bot):
    url = os.getenv('GENE_AUDIO_PREPARE_URL')
    if not url:
        return
    from lavalink.node import Node
    from lavalink.player import Player
    AudioPreparation(url, os.environ['GENE_AUDIO_PREPARE_TOKEN'],
                     int(os.environ['GENE_GUILD_ID'])).install(Node, Player)
