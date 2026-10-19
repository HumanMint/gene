import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gene.runtime_audio import AudioPreparation


def harness():
    class Node:
        def __init__(self):
            self.sent = []
            self.event_handler = Mock()
        async def send(self, data):
            self.sent.append(data)
        async def no_stop_play(self, guild_id, track, replace=True, start=0, pause=False):
            await self.send({'op': 'play', 'guildId': str(guild_id), 'track': track.uri, 'start': start, 'pause': pause})
    adapter = AudioPreparation('http://gene-audio:2334', 'x'*40, 123)
    adapter.install(Node)
    return adapter, Node(), SimpleNamespace(uri='https://www.youtube.com/watch?v=aBcDeFgH123')


@pytest.mark.parametrize('command', ['complete', 'stop', 'destroy', 'skip', 'pause', 'seek'])
def test_download_finishes_before_playback_and_pending_controls_remain_effective(command):
    async def scenario():
        adapter, node, track = harness()
        entered, ready = asyncio.Event(), asyncio.Event()
        async def prepare(identifier):
            entered.set()
            await ready.wait()
        adapter.prepare = AsyncMock(side_effect=prepare)
        work = asyncio.create_task(node.no_stop_play(123, track))
        await entered.wait()
        assert not node.sent  # No playback timer starts while the download is pending.
        if command in ('stop', 'destroy'):
            await node.send({'op': command, 'guildId': '123'})
        elif command == 'pause':
            await node.send({'op': 'pause', 'guildId': '123', 'pause': True})
        elif command == 'seek':
            await node.send({'op': 'seek', 'guildId': '123', 'position': 60000})
        elif command == 'skip':
            await node.no_stop_play(123, SimpleNamespace(uri='https://example.com/non-youtube'))
        ready.set()
        await work
        played = [r for r in node.sent if r['op'] == 'play']
        if command in ('stop', 'destroy'):
            assert not played
        elif command == 'skip':
            assert len(played) == 1 and 'example.com' in played[0]['track']
        else:
            assert len(played) == 1
            assert played[0]['pause'] == (command == 'pause')
            assert played[0]['start'] == (60000 if command == 'seek' else 0)
        assert not adapter.pending
    asyncio.run(scenario())


def test_failed_preparation_uses_red_error_handling_without_starting_audio():
    async def scenario():
        adapter, node, track = harness()
        adapter.prepare = AsyncMock(side_effect=TimeoutError)
        await node.no_stop_play(123, track)
        assert not node.sent
        assert node.event_handler.call_count == 1
        payload = node.event_handler.call_args.args[2]
        assert payload['guildId'] == '123' and 'download' in payload['exception']['message']
        assert not adapter.pending
    asyncio.run(scenario())


def test_other_servers_and_non_youtube_tracks_keep_normal_transport():
    async def scenario():
        adapter, node, track = harness()
        adapter.prepare = AsyncMock()
        await node.no_stop_play(777, track)
        await node.no_stop_play(123, SimpleNamespace(uri='https://example.com/song'))
        adapter.prepare.assert_not_awaited()
        assert len(node.sent) == 2
    asyncio.run(scenario())
