"""The owner's usage report over a time window: attempts are counted by when the controller recorded them."""

from unittest.mock import patch

from gene.fri_protocol import compatible, contract
from tests.test_api import actor, headers


def report(client, attempt, at, role='conversation', tokens=10):
    with patch('gene.store.time.time', return_value=at):
        assert client.post('/chat/usage/report', headers=headers('fri'), json={
            'attempt_id': attempt*32, 'role': role, 'model': 'gpt-6.1-sol', 'effort': 'high', 'status': 'completed',
            'counters': {'input_tokens': tokens, 'output_tokens': 1, 'cached_input_tokens': 4,
                         'reasoning_output_tokens': 0}}).status_code == 200


def usage(client, **body):
    return client.post('/agent/usage', headers=headers('fri'), json={**actor(), **body})


def test_a_window_keeps_only_attempts_recorded_since_then(client):
    report(client, 'a', 1_000)
    report(client, 'b', 5_000)
    report(client, 'c', 9_000, role='meme', tokens=7)
    everything = usage(client).json()
    assert sum(row['attempts'] for row in everything) == 3
    recent = {row['role']: row for row in usage(client, since=5_000).json()}
    assert recent['conversation']['attempts'] == 1 and recent['conversation']['input_tokens'] == 10
    assert recent['conversation']['cached_tokens'] == 4 and recent['conversation']['reasoning_tokens'] == 0
    assert recent['meme']['input_tokens'] == 7
    assert usage(client, since=9_001).json() == []
    assert usage(client, since=None).json() == everything
    assert all(row['since'] == 5_000 for row in recent.values())  # What Fri checks before naming a window.
    assert not any('since' in row for row in everything)


def test_a_window_is_validated_and_stays_owner_only(client):
    report(client, 'a', 1_000)
    for since in (-1, 'soon', [1]):
        assert usage(client, since=since).status_code == 422
    for raw in ('Infinity', 'NaN'):  # Python's JSON reads these; a window can't be one.
        assert client.post('/agent/usage', headers={**headers('fri'), 'Content-Type': 'application/json'},
                           content='{"guild_id": "guild", "user_id": "owner", "since": %s}' % raw).status_code in (400, 422)
    assert client.post('/agent/usage', headers=headers('fri'), json={**actor('member'), 'since': 0}).status_code == 403
    assert client.post('/agent/usage', headers=headers('runtime'), json={**actor(), 'since': 0}).status_code == 401


def test_the_window_is_advertised_and_optional():
    advertised = contract()
    assert 'usage-window' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != 'usage-window']})
