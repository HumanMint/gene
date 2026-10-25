import pytest

from gene.fri_protocol import compatible, contract
from gene.repair_policy import ACTOR
from gene.requester_notices import claim, finish
from gene.store import Conflict
from tests.conftest import proposed
from tests.test_api import headers
from tests.test_repairs import payload, proposal, queued


def shipped(store, verified=True):
    job = queued(store)
    candidate = proposal(job)
    if not verified:
        candidate['repair_verification']['review']['decision'] = 'manual'
    store.finish_build(job['id'], job['lease'], candidate)
    if not verified:
        store.approve(job['id'], 'b'*40, 'owner')
    deploying = store.claim_deploy()
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Repair release checks passed.')
    return store.get(job['id'])


def test_a_shipped_member_repair_replies_once(store):
    job = shipped(store)
    assert job['approved_by'] == ACTOR
    first = claim(store, job['id'], 'owner')
    assert first['job'] == {'id': job['id'], 'guild_id': '123', 'user_id': '20',
                            'source_url': job['source_url'], 'approved_sha': 'b'*40}
    with pytest.raises(Conflict, match='may already'):
        claim(store, job['id'], 'owner')  # Still sending: a second tap never races the first.
    finish(store, job['id'], first['lease'], 'sent', '555')
    with pytest.raises(Conflict, match='already told'):
        claim(store, job['id'], 'owner')
    with pytest.raises(Conflict):
        finish(store, job['id'], first['lease'], 'sent', '555')
    with store.connection() as db:
        row = db.execute('SELECT * FROM requester_notices WHERE job_id=?', (job['id'],)).fetchone()
        assert (row['state'], row['message_id'], row['lease']) == ('sent', '555', None)
        assert db.execute('SELECT count(*) FROM audit WHERE action="requester_notice" AND actor="owner"').fetchone()[0] == 1


def test_a_refused_reply_can_be_tried_again_but_an_unconfirmed_one_cannot(store):
    job = shipped(store)
    first = claim(store, job['id'], 'owner')
    finish(store, job['id'], first['lease'], 'failed', '999')
    second = claim(store, job['id'], 'owner')
    with pytest.raises(Conflict):
        finish(store, job['id'], first['lease'], 'sent')  # The old lease is gone.
    finish(store, job['id'], second['lease'], 'uncertain')
    with pytest.raises(Conflict, match='may already'):
        claim(store, job['id'], 'owner')
    with store.connection() as db:
        assert db.execute('SELECT message_id FROM requester_notices').fetchone()[0] == ''


def test_a_repair_the_owner_finished_after_its_checks_still_qualifies(store):
    job = shipped(store, verified=False)
    assert job['kind'] == 'feature' and job['approved_by'] == 'owner'
    assert claim(store, job['id'], 'owner')['job']['user_id'] == '20'


def test_only_shipped_repairs_with_their_report_on_record_qualify(store):
    job = queued(store)
    with pytest.raises(Conflict, match='Only a shipped repair'):
        claim(store, job['id'], 'owner')  # Still building.
    store.finish_build(job['id'], job['lease'], proposal(job))
    with pytest.raises(Conflict, match='Only a shipped repair'):
        claim(store, job['id'], 'owner')  # Approved, not deployed.
    feature = proposed(store, sha='c'*40, source='message-2')
    store.approve(feature['id'], 'c'*40, 'owner')
    with pytest.raises(Conflict, match='Only a shipped repair'):
        claim(store, feature['id'], 'owner')  # A member's feature request has no report to reply to.
    with pytest.raises(KeyError):
        claim(store, 'ffffffffffff', 'owner')


def test_an_erased_reporter_leaves_nothing_to_reply_to(store):
    job = shipped(store)
    store.erase_user('20')
    assert store.get(job['id'])['state'] == 'deployed' and store.get(job['id'])['source_url'] == ''
    with pytest.raises(Conflict, match='Only a shipped repair'):
        claim(store, job['id'], 'owner')


def test_a_later_rollback_stops_the_reply(store):
    job = shipped(store)
    rollback = store.rollback('123', 'owner')
    deploying = store.claim_deploy()
    assert deploying['id'] == rollback['id']
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Rolled back.')
    with pytest.raises(Conflict, match='rollback'):
        claim(store, job['id'], 'owner')


def test_routes_are_owner_only_and_the_capability_is_optional(tmp_path):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from gene.api import create_app
    from gene.config import Settings
    from gene.store import Store
    settings = Settings(Path(tmp_path/'api.sqlite3'), '123', 'owner', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='1', fri_token='fri-'+'x'*32)
    client = TestClient(create_app(settings))
    store = Store(settings.db)
    store.bootstrap('a'*40)
    store.set_paused(False, 'owner')
    store.update_models([{'id': 'gpt-6-astra', 'name': 'GPT-6 Astra', 'efforts': ['high'], 'default_effort': 'high'}])
    store.select_model('gpt-6-astra', 'high', 'owner')
    job = shipped(store)
    route = f"/requests/{job['id']}/requester-notice"
    assert client.post(route, headers=headers('fri'), json={'guild_id': '123', 'user_id': '20'}).status_code == 403
    assert client.post(route, headers=headers('fri'), json={'guild_id': '9', 'user_id': 'owner'}).status_code == 403
    assert client.post(route, headers=headers('runtime'), json={'guild_id': '123', 'user_id': 'owner'}).status_code == 401
    assert client.post('/requests/ffffffffffff/requester-notice', headers=headers('fri'),
                       json={'guild_id': '123', 'user_id': 'owner'}).status_code == 404
    claimed = client.post(route, headers=headers('fri'), json={'guild_id': '123', 'user_id': 'owner'})
    assert claimed.status_code == 200 and claimed.json()['job']['user_id'] == '20'
    lease = claimed.json()['lease']
    assert client.post(route+'/finish', headers=headers('fri'),
                       json={'lease': lease, 'state': 'posted'}).status_code == 422
    assert client.post(route+'/finish', headers=headers('fri'),
                       json={'lease': lease, 'state': 'sent', 'message_id': '42'}).json() == {'ok': True}
    again = client.post(route, headers=headers('fri'), json={'guild_id': '123', 'user_id': 'owner'})
    assert again.status_code == 409 and 'already told' in again.json()['detail']
    advertised = contract()
    assert 'requester-notices' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != 'requester-notices']})


def test_reports_from_member_dms_keep_their_dm_link(store):
    from gene.repair_store import request
    from tests.test_repairs import enable
    enable(store)
    body = payload()
    body['conversation_id'] = 'private:77'
    body['channel_id'] = '1'
    body['source']['channel_id'] = '77'
    body['source']['guild_id'] = None
    assert request(store, body)['accepted']
    job = store.claim()
    assert job['source_url'].startswith('https://discord.com/channels/@me/77/')
