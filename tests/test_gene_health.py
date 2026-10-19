import time

import pytest

from tests.test_api import headers
from tests.test_gene_controls import control_body
from tests.test_gene_controls import controls_api as controls_api


def heartbeat(**changes):
    return {'guild_id': '123', 'sha': 'a'*40, 'started': time.time()-100,
            'guild_connected': True, 'controls_running': True, **changes}


def scope(**changes):
    return {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1', **changes}


def test_health_is_independent_scoped_and_does_not_grant_execution(controls_api):
    client, controls = controls_api
    for role in ('fri', 'worker', 'deploy'):
        assert client.post('/runtime/health', headers=headers(role), json=heartbeat()).status_code == 401
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/controls/health', headers=headers(role), json=scope()).status_code == 401
    for changes in ({'guild_id': '777', 'channel_id': '7'}, {'channel_id': 'unknown'}):
        assert client.post('/controls/health', headers=headers('fri'), json=scope(**changes)).status_code == 403
    assert client.post('/runtime/health', headers=headers('runtime'), json=heartbeat(guild_id='other')).status_code == 403
    assert client.post('/runtime/health', headers=headers('runtime'), json={**heartbeat(), 'logs': 'private'}).status_code == 422
    assert not controls.health()['responsive']
    assert client.post('/runtime/health', headers=headers('runtime'), json=heartbeat()).status_code == 200
    report = client.post('/controls/health', headers=headers('fri'), json=scope()).json()
    assert report['responsive'] and report['heartbeat_age_seconds'] == 0
    with controls.store.connection() as db:
        assert not db.execute('SELECT 1 FROM gene_controls').fetchone()
        assert not db.execute('SELECT 1 FROM jobs').fetchone()
        db.execute('UPDATE gene_runtime_health SET at=?', (time.time()-45,))
    report = controls.health()
    assert not report['responsive'] and report['heartbeat_age_seconds'] >= 45
    # A bot process can keep heartbeating while its command bridge is occupied/stalled.
    controls.heartbeat(heartbeat())
    with controls.store.connection() as db:
        db.execute('UPDATE gene_control_catalog SET at=?', (time.time()-30,))
    assert not controls.health()['responsive']
    assert controls.store.status()['paused']


@pytest.mark.parametrize('state', ['claimed', 'running', 'finished'])
def test_restart_unblocks_wait_without_replay_or_overwriting_confirmed_receipt(controls_api, state):
    _, controls = controls_api
    body = control_body()
    identifier = controls.enqueue(body)['id']
    claim = controls.poll(['play'])
    if state != 'claimed':
        controls.start(identifier, claim['lease'])
    if state == 'finished':
        controls.finish(identifier, claim['lease'], {'output': 'Track Enqueued'})
    controls.heartbeat(heartbeat(started=time.time()))
    result = controls.result(identifier, '20', '123', 'public:1')
    assert result['done']
    if state == 'finished':
        assert result['output'] == 'Track Enqueued'
    else:
        assert 'restarted' in result['uncertain'] and 'Do not retry automatically' in result['uncertain']
    assert controls.poll(['play']) is None
    controls.store.clear_chats('20')
    assert controls.recent('20', '123', 'public:1') == []
