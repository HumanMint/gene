import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import parse_qs, urlparse

import discord
import httpx
import lavalink
import pytest
from fastapi.testclient import TestClient
from lavalink.enums import ExceptionSeverity, LavalinkEvents, NodeState
from lavalink.node import Node
from lavalink.player import Player
from lavalink.rest_api import Track
from redbot.cogs.audio.core.events.lavalink import LavalinkEvents as RedLavalinkEvents
from redbot.cogs.audio.core.utilities.formatting import FormattingUtilities
from redbot.cogs.audio.core.utilities.miscellaneous import MiscellaneousUtilities
from redbot.cogs.audio.core.utilities.player import PlayerUtilities

from gene import audio_resolver, runtime_audio
from gene.runtime_audio import AudioPreparation

TOKEN, RELAY_TOKEN = 'x' * 40, 'y' * 40
create_resolver = audio_resolver.create_app
LONG, SHORT = 'aBcDeFgH123', 'bBcDeFgH123'
# Red's search picked a 90m18s first result in the gene #73 incident.
DURATIONS = {LONG: 5418, SHORT: 174}
GENERIC = 'The audio download could not be prepared. Try again shortly or choose another track.'
TOO_LONG = "That track is longer than 30 minutes, so Gene can't play it. Pick a shorter one."


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


def test_other_servers_and_non_youtube_tracks_keep_normal_transport():
    async def scenario():
        adapter, node, track = harness()
        adapter.prepare = AsyncMock()
        await node.no_stop_play(777, track)
        await node.no_stop_play(123, SimpleNamespace(uri='https://example.com/song'))
        adapter.prepare.assert_not_awaited()
        assert len(node.sent) == 2
    asyncio.run(scenario())


def song(identifier, title='Song'):
    return Track({'track': 'encoded-' + identifier, 'info': {
        'identifier': identifier, 'title': title, 'author': 'Artist', 'isStream': False,
        'isSeekable': True, 'length': DURATIONS[identifier] * 1000,
        'uri': 'https://www.youtube.com/watch?v=' + identifier}})


class RedAudio:
    """The Audio cog state that installed Red's own Lavalink event handler reads on a track error."""
    lavalink_event_handler = RedLavalinkEvents.lavalink_event_handler
    get_track_description = FormattingUtilities.get_track_description
    rgetattr = MiscellaneousUtilities.rgetattr
    maybe_reset_error_counter = PlayerUtilities.maybe_reset_error_counter
    increase_error_counter = PlayerUtilities.increase_error_counter

    def __init__(self, repeat=False):
        settings = {'disconnect': False, 'repeat': repeat, 'notify': False, 'auto_play': False}
        self.bot = SimpleNamespace(cog_disabled_in_guild=AsyncMock(return_value=False), dispatch=Mock(),
            get_embed_color=AsyncMock(return_value=discord.Colour.red()),
            _i18n_cache=SimpleNamespace(get_locale=AsyncMock(return_value='en-US'),
                                        get_regional_format=AsyncMock(return_value=None)))
        self.config = SimpleNamespace(status=AsyncMock(return_value=False),
            guild=lambda guild: SimpleNamespace(all=AsyncMock(return_value=settings)))
        self.local_folder_current_path = None
        self.api_interface = SimpleNamespace(global_cache_api=SimpleNamespace(report_invalid=AsyncMock()),
                                             persistent_queue_api=SimpleNamespace(drop=AsyncMock()))
        self.playlist_api = None
        self._error_counter, self._error_timer = {}, {}


@pytest.fixture
def stack(monkeypatch):
    """Installed red-lavalink's Node, Player, dispatch and event parsing, without a websocket."""
    monkeypatch.setattr(Node, 'no_stop_play', Node.no_stop_play)  # Restore after install().
    monkeypatch.setattr(Node, 'send', Node.send)
    monkeypatch.setattr(Player, 'skip', Player.skip)
    monkeypatch.setattr(lavalink.node, '_nodes', [])
    monkeypatch.setattr(lavalink.lavalink, '_event_listeners', [])

    async def build(adapter, repeat=False):
        adapter.install(Node, Player)
        node = Node(event_handler=lavalink.lavalink.dispatch, host='127.0.0.1', password='offline',
                    user_id=99, num_shards=1, port=2333)
        node.state = NodeState.READY
        channel = SimpleNamespace(id=1, send=AsyncMock())
        guild = SimpleNamespace(id=123, name='server',
                                get_channel_or_thread=lambda id: channel if id == channel.id else None)
        player = Player(SimpleNamespace(user=SimpleNamespace(id=99)), SimpleNamespace(id=2, name='voice', guild=guild))
        node._players_dict[123] = player
        player.store('notify_channel', channel.id)  # As Red's !play does.
        player.repeat = repeat
        events = []
        async def record(player, event, extra):
            events.append((event, extra))
        audio = RedAudio(repeat)
        lavalink.register_event_listener(record)
        lavalink.register_event_listener(audio.lavalink_event_handler)
        return SimpleNamespace(node=node, player=player, channel=channel, events=events, audio=audio)
    return build


async def settle():
    """Finish the listener tasks lavalink's dispatch started, surfacing any failure inside them."""
    while tasks := asyncio.all_tasks() - {asyncio.current_task()}:
        done, _ = await asyncio.wait(tasks)
        for task in done:
            task.result()


def plays(node):
    return [row['track'] for row in node._queue if row['op'] == 'play']


def ops(node):
    return [(row['op'], row.get('track')) for row in node._queue if row['op'] in ('play', 'stop')]


def track_error(red):
    red.channel.send.assert_awaited_once()
    embed = red.channel.send.await_args.kwargs['embed']
    assert embed.title == 'Track Error'
    return embed.description


def test_failed_preparation_reaches_red_through_lavalink_dispatch_and_playback_recovers(stack):
    async def scenario():
        adapter = AudioPreparation('http://gene-audio:2334', TOKEN, 123)
        adapter.prepare = AsyncMock(side_effect=TimeoutError)
        red = await stack(adapter)
        try:
            red.player.add(None, song(LONG))
            await red.player.play()
            assert not plays(red.node) and not adapter.pending
            await settle()
            # Red reports the error once, then its skip ends the now-empty queue.
            assert red.events == [(LavalinkEvents.TRACK_EXCEPTION, {
                'message': GENERIC, 'cause': 'Audio preparation failed before playback',
                'severity': ExceptionSeverity.COMMON}), (LavalinkEvents.QUEUE_END, None)]
            assert track_error(red) == GENERIC + f'\n**[Song - Artist](https://www.youtube.com/watch?v={LONG}) **'
            assert not plays(red.node) and red.player.current is None
            adapter.prepare = AsyncMock()
            red.player.add(None, song(SHORT))  # The next !play still works.
            await red.player.play()
            assert plays(red.node) == ['encoded-' + SHORT] and not adapter.pending
        finally:
            await red.node.session.close()
    asyncio.run(scenario())


def hold(red, name):
    """Pause Red's Track Error handler at one of its awaits (its first, or the embed colour)."""
    reached, release = asyncio.Event(), asyncio.Event()
    original = getattr(red.audio.bot, name)
    async def paused(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original(*args, **kwargs)
    setattr(red.audio.bot, name, paused)
    return reached, release


def failing(*identifiers):
    async def prepare(identifier):
        if identifier in identifiers:
            raise TimeoutError
    return AsyncMock(side_effect=prepare)


@pytest.mark.parametrize('pause', ['cog_disabled_in_guild', 'get_embed_color'])
@pytest.mark.parametrize('repeat', [False, True])
def test_a_member_skip_during_reds_track_error_keeps_the_newer_track_playing(stack, pause, repeat):
    async def scenario():
        adapter = AudioPreparation('http://gene-audio:2334', TOKEN, 123)
        adapter.prepare = failing(LONG)
        red = await stack(adapter, repeat=repeat)
        try:
            reached, release = hold(red, pause)
            red.player.add(None, song(LONG))
            red.player.add(None, song(SHORT))
            await red.player.play()
            await reached.wait()  # Red holds A's Track Error; the member skips to B meanwhile.
            await red.player.skip()
            assert ops(red.node) == [('play', 'encoded-' + SHORT)]
            release.set()
            await settle()
            # Red still reports A, but its automatic skip no longer stops or restarts B.
            assert track_error(red).startswith(GENERIC + '\n')
            assert ops(red.node) == [('play', 'encoded-' + SHORT)]
            assert [event for event, extra in red.events] == [LavalinkEvents.TRACK_EXCEPTION]
            assert red.player.current.track_identifier == 'encoded-' + SHORT
            assert not adapter.pending
        finally:
            await red.node.session.close()
    asyncio.run(scenario())


def test_a_member_stop_during_reds_track_error_is_not_repeated(stack):
    async def scenario():
        adapter = AudioPreparation('http://gene-audio:2334', TOKEN, 123)
        adapter.prepare = failing(LONG)
        red = await stack(adapter)
        try:
            reached, release = hold(red, 'get_embed_color')
            red.player.add(None, song(LONG))
            red.player.add(None, song(SHORT))
            await red.player.play()
            await reached.wait()
            await red.player.stop()
            release.set()
            await settle()
            assert ops(red.node) == [('stop', None)]
            assert [event for event, extra in red.events] == [LavalinkEvents.TRACK_EXCEPTION, LavalinkEvents.QUEUE_END]
            assert red.player.current is None
        finally:
            await red.node.session.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('repeat', [False, True])
def test_failed_preparation_still_skips_to_the_next_track(stack, repeat):
    async def scenario():
        adapter = AudioPreparation('http://gene-audio:2334', TOKEN, 123)
        adapter.prepare = failing(LONG)
        red = await stack(adapter, repeat=repeat)
        try:
            red.player.add(None, song(LONG))
            red.player.add(None, song(SHORT))
            await red.player.play()
            await settle()
            assert ops(red.node) == [('play', 'encoded-' + SHORT)]
            assert red.player.current.track_identifier == 'encoded-' + SHORT
            # The member's own skip afterwards is unaffected: B ends the queue, or repeats under repeat.
            await red.player.skip()
            await settle()
            assert ops(red.node)[1:] == ([('play', 'encoded-' + SHORT)] if repeat else [('stop', None)])
        finally:
            await red.node.session.close()
    asyncio.run(scenario())


def test_production_install_guards_reds_automatic_skip(monkeypatch):
    monkeypatch.setattr(Node, 'no_stop_play', Node.no_stop_play)
    monkeypatch.setattr(Node, 'send', Node.send)
    monkeypatch.setattr(Player, 'skip', Player.skip)
    original = Player.skip
    monkeypatch.setenv('GENE_AUDIO_PREPARE_URL', 'http://gene-audio:2334')
    monkeypatch.setenv('GENE_AUDIO_PREPARE_TOKEN', TOKEN)
    monkeypatch.setenv('GENE_GUILD_ID', '123')
    runtime_audio.install_audio_preparation(None)
    assert Player.skip is not original and Node.no_stop_play.__module__ == 'gene.runtime_audio'


def extractor(identifier, options):
    video = parse_qs(urlparse(identifier).query)['v'][0]
    return {'id': video, 'title': 'Song', 'uploader': 'Artist', 'protocol': 'https',
            'url': 'https://rr1.googlevideo.com/audio?signature=private-signed-url',
            'duration': DURATIONS[video]}


def downloader(result, folder, maximum):
    path = folder / 'audio.webm'
    path.write_bytes(b'complete audio')
    return path


def resolver(tmp_path, topology):
    """The real resolver: on its own, or on Hostinger fed by the real home relay app."""
    if topology == 'host':
        return create_resolver(TOKEN, extractor, cache_dir=tmp_path, downloader=downloader)
    relay = TestClient(create_resolver(RELAY_TOKEN, extractor, cache_dir=tmp_path / 'relay',
                                       downloader=downloader, relay_only=True))
    def tailscale(request):
        upstream = relay.post(request.url.path, content=request.content,
                              headers={'Authorization': request.headers['Authorization'],
                                       'Content-Type': 'application/json'})
        return httpx.Response(upstream.status_code, headers=upstream.headers, content=upstream.content)
    return create_resolver(TOKEN, cache_dir=tmp_path / 'host', relay_url='http://100.64.0.1:2335',
                           relay_token=RELAY_TOKEN, transport=httpx.MockTransport(tailscale))


@pytest.mark.parametrize('topology', ['relay', 'host'])
def test_ninety_minute_search_result_tells_the_member_why_and_red_plays_the_next_track(stack, tmp_path, caplog, topology):
    async def scenario():
        app = resolver(tmp_path, topology)
        adapter = AudioPreparation('http://gene-audio:2334', TOKEN, 123, transport=httpx.ASGITransport(app=app))
        red = await stack(adapter)
        try:
            red.player.add(None, song(LONG, 'Ninety minute mix'))
            red.player.add(None, song(SHORT))
            await red.player.play()
            assert not plays(red.node)
            await settle()
            assert [(event, extra['message'], extra['severity']) for event, extra in red.events] == [
                (LavalinkEvents.TRACK_EXCEPTION, TOO_LONG, ExceptionSeverity.COMMON)]
            assert track_error(red) == (TOO_LONG + '\n**[Ninety minute mix - Artist]'
                                        f'(https://www.youtube.com/watch?v={LONG}) **')
            # Red's own error path skips to the next queued track, which prepares and plays.
            assert plays(red.node) == ['encoded-' + SHORT]
            assert red.player.current.track_identifier == 'encoded-' + SHORT and not adapter.pending
            assert not list(tmp_path.rglob(LONG + '.*'))
            assert 'Selected audio preparation failed (too_long)' in caplog.text
            assert 'signature' not in caplog.text + track_error(red)
        finally:
            await red.node.session.close()
    asyncio.run(scenario())


def refused_by(response):
    async def scenario(stack):
        adapter = AudioPreparation('http://gene-audio:2334', TOKEN, 123,
                                   transport=httpx.MockTransport(lambda request: response))
        red = await stack(adapter)
        try:
            red.player.add(None, song(LONG))
            await red.player.play()
            await settle()
            return ([extra['message'] for event, extra in red.events if event == LavalinkEvents.TRACK_EXCEPTION],
                    plays(red.node))
        finally:
            await red.node.session.close()
    return scenario


@pytest.mark.parametrize('response,message', [
    (httpx.Response(422, headers={'x-gene-refusal': 'too_long'}), TOO_LONG),
    (httpx.Response(422, json={'detail': 'Use a public track no longer than 30 minutes.'}), GENERIC),
    (httpx.Response(422, headers={'x-gene-refusal': 'too_long https://rr1.googlevideo.com/x'}), GENERIC),
    (httpx.Response(422, headers={'x-gene-refusal': 'x' * 4000}), GENERIC),
    (httpx.Response(502, headers={'x-gene-refusal': 'too_long'}, text='private relay failure'), GENERIC),
    (httpx.Response(200, json={'ready': False}), GENERIC),
])
def test_only_known_refusal_codes_change_the_member_message(stack, caplog, response, message):
    assert asyncio.run(refused_by(response)(stack)) == ([message], [])
    assert 'Selected audio preparation failed (' in caplog.text
    assert 'private' not in caplog.text and 'googlevideo' not in caplog.text and 'xxxx' not in caplog.text


def test_refusal_header_on_a_ready_response_is_ignored(stack):
    response = httpx.Response(200, headers={'x-gene-refusal': 'too_long'}, json={'ready': True})
    assert asyncio.run(refused_by(response)(stack)) == ([], ['encoded-' + LONG])


def test_every_resolver_refusal_has_one_bounded_member_line():
    assert set(runtime_audio.REFUSALS) == set(audio_resolver.REFUSALS)
    assert all(len(line) <= 200 and '\n' not in line for line in runtime_audio.REFUSALS.values())


@pytest.mark.parametrize('change,reason', [
    ({'duration': 5418}, 'too_long'), ({'duration': 1801}, 'too_long'),
    ({'filesize': 65 * 1024 ** 2}, 'too_large'), ({'is_live': True}, 'unsupported'),
    ({'url': 'https://example.com/audio'}, 'unsupported'), ({'protocol': 'm3u8_native'}, 'unsupported'),
    ({'duration': None}, 'unsupported'),
])
def test_relay_refusals_carry_only_a_fixed_code(tmp_path, change, reason):
    def extract(identifier, options):
        return {**extractor(identifier, options), **change}
    relay = TestClient(create_resolver(RELAY_TOKEN, extract, cache_dir=tmp_path,
                                       downloader=downloader, relay_only=True))
    response = relay.post('/transfer', headers={'Authorization': 'Bearer ' + RELAY_TOKEN}, json={'identifier': SHORT})
    assert response.status_code == 422 and response.headers['x-gene-refusal'] == reason
    assert 'googlevideo' not in response.text and 'signature' not in response.text
    assert not list(tmp_path.iterdir())


def test_thirty_minutes_exactly_still_plays(tmp_path):
    def extract(identifier, options):
        return {**extractor(identifier, options), 'duration': 1800}
    client = TestClient(create_resolver(TOKEN, extract, cache_dir=tmp_path, downloader=downloader))
    response = client.post('/prepare', headers={'Authorization': 'Bearer ' + TOKEN}, json={'identifier': LONG})
    assert response.json() == {'ready': True} and 'x-gene-refusal' not in response.headers


@pytest.mark.parametrize('headers', [{'x-gene-refusal': 'unknown'}, {'x-gene-refusal': 'too_long; https://x'}, {}])
def test_host_drops_unknown_relay_refusals_and_relay_text(tmp_path, headers):
    relay_text = 'private relay detail https://rr1.googlevideo.com/audio?signature=secret'
    def tailscale(request):
        return httpx.Response(422, headers=headers, text=relay_text)
    client = TestClient(create_resolver(TOKEN, cache_dir=tmp_path, relay_url='http://100.64.0.1:2335',
                                        relay_token=RELAY_TOKEN, transport=httpx.MockTransport(tailscale)))
    response = client.post('/prepare', headers={'Authorization': 'Bearer ' + TOKEN}, json={'identifier': LONG})
    assert response.status_code == 502 and 'x-gene-refusal' not in response.headers
    assert 'private relay detail' not in response.text and 'googlevideo' not in response.text
    assert not list(tmp_path.glob('download-*'))


def test_host_relays_a_known_code_with_its_own_text(tmp_path):
    def tailscale(request):
        return httpx.Response(422, headers={'x-gene-refusal': 'too_long'}, text='private relay detail')
    client = TestClient(create_resolver(TOKEN, cache_dir=tmp_path, relay_url='http://100.64.0.1:2335',
                                        relay_token=RELAY_TOKEN, transport=httpx.MockTransport(tailscale)))
    response = client.post('/prepare', headers={'Authorization': 'Bearer ' + TOKEN}, json={'identifier': LONG})
    assert response.status_code == 422 and response.headers['x-gene-refusal'] == 'too_long'
    assert response.json() == {'detail': audio_resolver.REFUSALS['too_long']}
