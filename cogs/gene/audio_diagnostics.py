"""Read-only playback diagnostics; Red remains responsible for recovery."""

import json
import logging

import lavalink

LOG = logging.getLogger('gene.audio')


def milliseconds(value):
    # Do not stringify event data: it can contain private text or signed URLs.
    return value if type(value) is int and value >= 0 else None


def boolean(value):
    return value if type(value) is bool else None


def log_track_stuck(player, event_type, extra, *, guild_id):
    if event_type != lavalink.LavalinkEvents.TRACK_STUCK:
        return
    if not guild_id or getattr(getattr(player, 'guild', None), 'id', None) != guild_id:
        return

    # Lavalink passes thresholdMs as a scalar, not a dict. Its listeners run
    # concurrently, so this is an observation, not a recovery result.
    current = getattr(player, 'current', None)
    queue = getattr(player, 'queue', None)
    snapshot = {
        'event': event_type.value,
        'threshold_ms': milliseconds(extra),
        'position_ms': milliseconds(getattr(player, 'position', None)),
        'current_track_present': current is not None,
        'current_track_length_ms': milliseconds(getattr(current, 'length', None)),
        'paused': boolean(getattr(player, 'paused', None)),
        'player_reports_playing': boolean(getattr(player, 'is_playing', None)),
        'connected': boolean(getattr(player, 'connected', None)),
        'node_ready': boolean(getattr(getattr(player, 'node', None), 'ready', None)),
        'queue_size': len(queue) if isinstance(queue, (list, tuple)) else None,
    }
    LOG.warning('Playback diagnostic: %s', json.dumps(snapshot, sort_keys=True))
