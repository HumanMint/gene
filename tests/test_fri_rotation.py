"""Exercise durable member turns across channels, changing rosters and failed searches."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_agent_store import AgentStore
from tests.test_api import headers
from tests.test_fri_exploration import archive, finish, row
from tests.test_fri_learning import learning as learning


def members(*users):
    return [{'user_id': str(user), 'name': 'friend '+str(user), 'role_id': '90'} for user in users]


def test_target_and_scope_are_controller_bound_and_cannot_be_overridden_by_a_plan(learning):
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    claim = scheduler.claim()
    url = '/agent/learning/'+claim['id']+'/target'
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post(url, json={'members': members(11)}, headers=headers(role)).status_code == 401
    for invalid in ([], members(11, 11), [{'user_id': '11', 'role_id': '99', 'name': 'wrong role'}]):
        with pytest.raises(ValueError):
            scheduler.exploration.target(claim['id'], invalid)
    picked = client.post(url, json={'members': members(11, 12)}, headers=headers('fri'))
    assert picked.status_code == 200 and picked.json()['target']['user_id'] == '11'
    for user in ('', '12', '999'):
        with pytest.raises(ValueError, match='assigned'):
            scheduler.exploration.prepare(claim['id'], user, '', 'change member')
    with pytest.raises(ValueError):
        scheduler.exploration.target(claim['id'], members(12))
    store.personal_control('11', 'disable')
    with pytest.raises(ValueError):
        scheduler.exploration.prepare(claim['id'], '11', '', 'revive erased target')
    result = finish(scheduler, claim, [])
    assert result['error']
    with store.connection() as db:
        assert not db.execute('SELECT 1 FROM fri_explore_rotation WHERE user_id="11"').fetchone()


def test_daily_refreshes_eventually_find_new_messages_in_each_exhausted_channel(learning):
    clock, store, settings, scheduler = archive(learning)
    settings = replace(settings, agent_history_channels='1,2')
    scheduler = AgentStore(store, settings)
    history = {'1': [row(clock[0]-1000, channel='1')], '2': [row(clock[0]-900, channel='2')]}
    selected, added = [], None
    for index in range(7):
        claim = scheduler.exploration.target(scheduler.claim()['id'], members(11))
        plan = scheduler.exploration.prepare(claim['id'], '11', '', 'read broadly')
        selected.append(claim['channel_id'])
        page = [r for r in history[claim['channel_id']] if int(r['id']) < int(plan['before_id'])]
        finish(scheduler, claim, page)
        scheduler.delivered(claim['id'])
        clock[0] += 26*3600
        if index == 3:
            added = row(clock[0]-100, channel='2', text='watched a new film')
            history['2'].append(added)
    assert selected == ['1', '1', '2', '2', '1', '1', '2']
    with store.connection() as db:
        assert db.execute('SELECT 1 FROM fri_explore_seen WHERE message_id=?', (added['id'],)).fetchone()
