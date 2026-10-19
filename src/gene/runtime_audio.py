"""Prepare selected YouTube audio before Red's Lavalink transport starts playback.

A pinned-client adapter, installed outside Red. No player/queue implementation is
replaced. Stop, skip, disconnect, pause and seek remain effective during preparation.
"""

import asyncio
import logging
import os
import re
from urllib.parse import parse_qs, urlsplit

import httpx

LOG = logging.getLogger('gene.audio.prepare')


def youtube_id(track):
    url = urlsplit(track.uri or '')
    if url.scheme != 'https' or url.hostname not in {'youtube.com', 'www.youtube.com', 'music.youtube.com', 'youtu.be'}:
        return None
    identifier = url.path.strip('/') if url.hostname == 'youtu.be' else parse_qs(url.query).get('v', [''])[0]
    return identifier if re.fullmatch(r'[A-Za-z0-9_-]{11}', identifier) else None


class AudioPreparation:
    def __init__(self, url, token, guild_id):
        if url not in {'http://gene-audio:2334', 'http://127.0.0.1:2334'} or len(token) < 32:
            raise ValueError('Audio preparation requires the private resolver and its service token')
        self.url, self.token, self.guild_id = url, token, guild_id
        self.pending = {}

    async def prepare(self, identifier):
        async with httpx.AsyncClient(timeout=httpx.Timeout(95, connect=5), trust_env=False) as client:
            response = await client.post(self.url+'/prepare', json={'identifier': identifier},
                                         headers={'Authorization': 'Bearer '+self.token})
            response.raise_for_status()
            if response.json().get('ready') is not True:
                raise ValueError('Audio preparation was not confirmed')

    def install(self, node_class):
        original_play, original_send = node_class.no_stop_play, node_class.send
        pending = self.pending

        async def send(node, data):
            key = (node, int(data.get('guildId', 0)))
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
            marker = {'pause': pause, 'start': start}
            pending[key] = marker  # A newer skip/play supersedes an older download.
            identifier = youtube_id(track)
            try:
                if guild_id == self.guild_id and identifier:
                    try:
                        async with asyncio.timeout(100):
                            await self.prepare(identifier)
                    except (httpx.HTTPError, TimeoutError, ValueError):
                        if pending.get(key) is marker:
                            from lavalink.enums import LavalinkEvents, LavalinkIncomingOp
                            LOG.warning('Selected audio preparation failed; playback was not started')
                            node.event_handler(LavalinkIncomingOp.EVENT, LavalinkEvents.TRACK_EXCEPTION,
                                {'guildId': str(guild_id), 'exception': {
                                    'message': 'The audio download could not be prepared. Try again shortly or choose another track.',
                                    'cause': 'Audio preparation failed before playback', 'severity': 'common'}})
                        return
                if pending.get(key) is marker:
                    return await original_play(node, guild_id, track, replace=replace,
                                               start=marker['start'], pause=marker['pause'])
            finally:
                if pending.get(key) is marker:
                    pending.pop(key, None)

        node_class.no_stop_play, node_class.send = play, send


def install_audio_preparation(bot):
    url = os.getenv('GENE_AUDIO_PREPARE_URL')
    if not url:
        return
    from lavalink.node import Node
    AudioPreparation(url, os.environ['GENE_AUDIO_PREPARE_TOKEN'], int(os.environ['GENE_GUILD_ID'])).install(Node)
