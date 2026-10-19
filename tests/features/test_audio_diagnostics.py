import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import lavalink
import pytest
from lavalink import lavalink as client

from cogs.gene.gene import Gene


def environment():
    # No Gene constructor: no HTTP session, health task, or live services.
    cog = object.__new__(Gene)
    cog.guild_id = 123
    cog.api = AsyncMock()
    cog.health = SimpleNamespace(cancel=Mock())
    cog.panel = SimpleNamespace(stop=Mock())
    cog.session = SimpleNamespace(close=AsyncMock())
    cog.private_panels = SimpleNamespace(close_all=AsyncMock())
    track = SimpleNamespace(length=180000, title='synthetic private title',
                            uri='https://invalid.test/private', requester=object())
    player = SimpleNamespace(
        guild=SimpleNamespace(id=123), current=track, queue=[track, object()],
        position=42000, paused=False, is_playing=False, connected=True,
        node=SimpleNamespace(ready=True), skip=AsyncMock(), play=AsyncMock(),
        stop=AsyncMock(), disconnect=AsyncMock(), store=Mock(),
    )
    return cog, player


def diagnostics(caplog):
    return [json.loads(record.getMessage().removeprefix('Playback diagnostic: '))
            for record in caplog.records if record.name == 'gene.audio']


def test_stuck_records_scalar_threshold_and_state_without_claiming_playback(caplog):
    caplog.set_level(logging.WARNING, logger='gene.audio')
    cog, player = environment()
    current, queue = player.current, list(player.queue)
    asyncio.run(cog.audio_event(player, lavalink.LavalinkEvents.TRACK_STUCK, 10000))
    assert diagnostics(caplog) == [{
        'event': 'TrackStuckEvent', 'threshold_ms': 10000, 'position_ms': 42000,
        'current_track_present': True, 'current_track_length_ms': 180000,
        'paused': False, 'player_reports_playing': False, 'connected': True,
        'node_ready': True, 'queue_size': 2,
    }]
    assert player.current is current
    assert player.queue == queue
    for method in (player.skip, player.play, player.stop, player.disconnect, cog.api):
        method.assert_not_awaited()
    player.store.assert_not_called()
    assert 'synthetic private title' not in caplog.text
    assert 'invalid.test' not in caplog.text
    assert '123' not in caplog.text


@pytest.mark.parametrize('threshold', [None, -1, True, 'private text\nnext line',
                                       {'thresholdMs': 10000, 'message': 'private payload'}])
def test_invalid_values_are_unknown_and_never_stringified(caplog, threshold):
    cog, player = environment()
    player.position = 'private position'
    player.paused = 'false'
    player.is_playing = 1
    player.connected = None
    player.node = None
    player.current = None
    player.queue = None
    asyncio.run(cog.audio_event(player, lavalink.LavalinkEvents.TRACK_STUCK, threshold))
    assert diagnostics(caplog) == [{
        'event': 'TrackStuckEvent', 'threshold_ms': None, 'position_ms': None,
        'current_track_present': False, 'current_track_length_ms': None,
        'paused': None, 'player_reports_playing': None, 'connected': None,
        'node_ready': None, 'queue_size': None,
    }]
    assert 'private' not in caplog.text


@pytest.mark.parametrize('guild_id, player_guild', [
    (0, 123), (123, 456), (123, None),
])
def test_listener_ignores_unconfigured_other_and_missing_guilds(caplog, guild_id, player_guild):
    cog, player = environment()
    cog.guild_id = guild_id
    player.guild = SimpleNamespace(id=player_guild) if player_guild else None
    asyncio.run(cog.audio_event(player, lavalink.LavalinkEvents.TRACK_STUCK, 10000))
    assert not diagnostics(caplog)


@pytest.mark.parametrize('event_type', [
    event for event in lavalink.LavalinkEvents if event != lavalink.LavalinkEvents.TRACK_STUCK
])
def test_other_events_do_not_read_player_or_log_payload(caplog, event_type):
    cog, _ = environment()
    asyncio.run(cog.audio_event(None, event_type, {'message': 'private payload'}))
    assert not diagnostics(caplog)


def test_listener_lifecycle_deduplicates_and_removes_only_its_callback(monkeypatch, caplog):
    monkeypatch.setattr(client, '_event_listeners', [])

    async def scenario():
        unrelated = AsyncMock()
        lavalink.register_event_listener(unrelated)
        cog, player = environment()
        await cog.cog_load()
        await cog.cog_load()
        assert client._event_listeners == [unrelated, cog.audio_event]
        await cog.cog_unload()
        assert client._event_listeners == [unrelated]
        cog.health.cancel.assert_called_once()
        cog.panel.stop.assert_called_once()
        cog.private_panels.close_all.assert_awaited_once()
        cog.session.close.assert_awaited_once()

        replacement, _ = environment()
        await replacement.cog_load()
        callbacks = list(client._event_listeners)
        await asyncio.gather(*(callback(player, lavalink.LavalinkEvents.TRACK_STUCK, 10000)
                               for callback in callbacks))
        assert len(diagnostics(caplog)) == 1
        await replacement.cog_unload()
        assert client._event_listeners == [unrelated]

    asyncio.run(scenario())


@pytest.mark.parametrize('with_diagnostics', [False, True])
def test_real_red_notification_and_recovery_run_once(monkeypatch, caplog, with_diagnostics):
    # Exercise installed Red's handler and Lavalink's raw-event normalization.
    # Only Discord delivery, configuration, and node lookup are local doubles.
    from redbot.cogs.audio.core.events import lavalink as red_events

    monkeypatch.setattr(client, '_event_listeners', [])
    monkeypatch.setattr(red_events, 'set_contextual_locales_from_guild', AsyncMock())

    async def scenario():
        cog, player = environment()
        channel = SimpleNamespace(send=AsyncMock())
        player.guild.get_channel_or_thread = Mock(return_value=channel)
        player.channel = SimpleNamespace(guild=player.guild)
        player.fetch = Mock(side_effect=lambda key: 456 if key == 'notify_channel' else None)
        current = player.current
        player.queue = [current, current, object()]
        audio = SimpleNamespace(
            bot=SimpleNamespace(cog_disabled_in_guild=AsyncMock(return_value=False),
                                get_embed_color=AsyncMock(return_value=0)),
            rgetattr=lambda obj, name, default=None: getattr(obj, name, default),
            config=SimpleNamespace(
                guild=Mock(return_value=SimpleNamespace(all=AsyncMock(return_value={
                    'disconnect': False, 'repeat': False, 'notify': True, 'auto_play': False,
                }))), status=AsyncMock(return_value=False)),
            get_track_description=AsyncMock(return_value='Synthetic track'),
            local_folder_current_path=None, maybe_reset_error_counter=AsyncMock(),
            increase_error_counter=AsyncMock(return_value=False), _error_counter={},
        )

        async def red_handler(*args):
            await red_events.LavalinkEvents.lavalink_event_handler(audio, *args)

        lavalink.register_event_listener(red_handler)
        if with_diagnostics:
            await cog.cog_load()
        monkeypatch.setattr(client.node, 'get_node', Mock(return_value=SimpleNamespace(
            get_player=Mock(return_value=player))))
        before = asyncio.all_tasks()
        client.dispatch(lavalink.enums.LavalinkIncomingOp.EVENT,
                        lavalink.LavalinkEvents.TRACK_STUCK,
                        {'guildId': '123', 'thresholdMs': 10000})
        await asyncio.gather(*(asyncio.all_tasks() - before))

        channel.send.assert_awaited_once()
        embed = channel.send.call_args.kwargs['embed']
        assert embed.title == 'Track Stuck'
        assert 'Playback of the song has stopped' in embed.description
        player.skip.assert_awaited_once_with()
        audio.increase_error_counter.assert_awaited_once_with(player)
        assert current not in player.queue
        assert len(player.queue) == 1
        player.play.assert_not_awaited()
        player.stop.assert_not_awaited()
        player.disconnect.assert_not_awaited()
        cog.api.assert_not_awaited()
        assert len(diagnostics(caplog)) == int(with_diagnostics)
        # Notifications are output, not another input to Gene's diagnostics.
        assert not any(name == 'on_message' for name, _ in Gene.__cog_listeners__)
        if with_diagnostics:
            assert diagnostics(caplog)[0]['threshold_ms'] == 10000
            await cog.cog_unload()

    asyncio.run(scenario())
