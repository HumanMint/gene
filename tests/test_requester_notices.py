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
    assert job['kind'] == 'feature' and job['approved_by'] == 'owner' and job['intake_approved_ref'] is None
    notice = store.claim_review_notice()  # The owner hears it shipped, so the DM can offer the reply.
    assert notice['job']['id'] == job['id'] and notice['phase'] == 'status' and notice['job']['state'] == 'deployed'
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
    owner = {'guild_id': '123', 'user_id': 'owner'}
    assert client.post(route+'/finish', headers=headers('fri'),
                       json={**owner, 'lease': lease, 'state': 'posted'}).status_code == 422
    assert client.post(route+'/finish', headers=headers('fri'),
                       json={**owner, 'lease': lease, 'state': 'sent', 'message_id': '42'}).json() == {'ok': True}
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


def test_a_feature_labelled_like_a_repair_does_not_qualify(store):
    job = store.request('123', '20', 'repair:123456', 'https://discord.com/channels/123/1/123456',
                        'Add a brand new command please')
    from tests.conftest import request_reference
    store.review_decision(job['id'], 'intake', request_reference(job), 'approve', 'owner')
    built = store.claim()
    store.finish_build(built['id'], built['lease'], {'sha': 'b'*40, 'base_sha': built['base_sha'],
                       'branch': 'codex/new', 'tests_passed': True, 'summary': 'New command'})
    store.approve(job['id'], 'b'*40, 'owner')
    deploying = store.claim_deploy()
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Release checks passed.')
    assert store.get(job['id'])['state'] == 'deployed'
    with pytest.raises(Conflict, match='Only a shipped repair'):
        claim(store, job['id'], 'owner')  # Repair intake never recorded it.


def test_an_erasure_after_an_old_rollback_does_not_block_a_newer_repair(store):
    feature = proposed(store, sha='c'*40, source='message-9')
    store.approve(feature['id'], 'c'*40, 'owner')
    deploying = store.claim_deploy()
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Release checks passed.')
    rollback = store.rollback('123', 'owner')
    deploying = store.claim_deploy()
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Rolled back.')
    assert store.get(rollback['id'])['state'] == 'deployed'
    job = shipped(store)
    store.erase_user('owner')  # Touches the old rollback's updated time.
    assert store.get(rollback['id'])['updated'] > store.get(job['id'])['updated']
    assert claim(store, job['id'], 'owner')['job']['id'] == job['id']


def test_a_newer_release_before_the_tap_stops_the_reply(store):
    job = shipped(store)
    feature = proposed(store, sha='c'*40, source='message-9')
    store.approve(feature['id'], 'c'*40, 'owner')
    deploying = store.claim_deploy()
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Release checks passed.')
    assert store.status()['active_sha'] == 'c'*40
    with pytest.raises(Conflict, match='newer release'):
        claim(store, job['id'], 'owner')  # "Fixed now" may not hold for what's running.
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM requester_notices').fetchone()[0] == 0


def test_recording_the_reply_is_the_owners_on_this_server(tmp_path):
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
    owner = {'guild_id': '123', 'user_id': 'owner'}
    lease = client.post(route, headers=headers('fri'), json=owner).json()['lease']
    for scope in ({'guild_id': '999', 'user_id': '20'}, {'guild_id': '999', 'user_id': 'owner'},
                  {'guild_id': '123', 'user_id': '20'}, {'guild_id': '123'}, {'user_id': 'owner'}):
        refused = client.post(route+'/finish', headers=headers('fri'), json={**scope, 'lease': lease, 'state': 'failed'})
        assert refused.status_code == 403
    again = client.post(route, headers=headers('fri'), json=owner)
    assert again.status_code == 409 and 'may already' in again.json()['detail']  # Still the first claim's.
    assert client.post(route+'/finish', headers=headers('fri'),
                       json={**owner, 'lease': lease, 'state': 'failed'}).json() == {'ok': True}
    # A Fri from before the owner-scoped finish (a Fri-only rollback) sends just the lease, and its
    # outcome still counts: a refusal it saw can be tried again.
    lease = client.post(route, headers=headers('fri'), json=owner).json()['lease']
    assert client.post(route+'/finish', headers=headers('fri'), json={'lease': lease, 'state': 'failed'}).json() == {'ok': True}
    lease = client.post(route, headers=headers('fri'), json=owner).json()['lease']
    assert client.post(route+'/finish', headers=headers('fri'),
                       json={'lease': lease, 'state': 'sent', 'message_id': '42'}).json() == {'ok': True}
    told = client.post(route, headers=headers('fri'), json=owner)
    assert told.status_code == 409 and 'already told' in told.json()['detail']


@pytest.mark.parametrize('next_release', ['rollback', 'feature'])
def test_no_reply_while_another_release_is_going_out(store, next_release):
    job = shipped(store)
    if next_release == 'rollback':
        store.rollback('123', 'owner')
    else:
        feature = proposed(store, sha='c'*40, source='message-9')
        store.approve(feature['id'], 'c'*40, 'owner')
    deploying = store.claim_deploy()
    assert store.status()['active_sha'] == 'b'*40  # The runtime may already run it; the controller hasn't recorded it.
    with pytest.raises(Conflict, match='going out'):
        claim(store, job['id'], 'owner')
    store.finish_deploy(deploying['id'], deploying['lease'], False, 'Health check failed.')
    assert store.status()['active_sha'] == 'b'*40
    assert claim(store, job['id'], 'owner')['job']['id'] == job['id']  # It never landed, so the repair is still live.


def test_no_reply_while_an_approved_rollback_waits_for_the_deployer(store):
    job = shipped(store)
    rollback = store.rollback('123', 'owner')  # Goes out on the deployer's next pass, or once development resumes.
    with pytest.raises(Conflict, match='rollback is waiting'):
        claim(store, job['id'], 'owner')
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM requester_notices').fetchone()[0] == 0
    deploying = store.claim_deploy()
    assert deploying['id'] == rollback['id']
    store.finish_deploy(deploying['id'], deploying['lease'], False, 'Health check failed.')
    assert claim(store, job['id'], 'owner')['job']['id'] == job['id']  # It never landed, so the repair is still live.


def test_a_stale_approved_rollback_does_not_hold_the_reply(store):
    job = shipped(store)
    rollback = store.rollback('123', 'owner')
    with store.connection() as db:  # Approved against an older release: the deployer fails it rather than run it.
        db.execute('UPDATE jobs SET proposal=json_set(proposal,"$.base_sha",?) WHERE id=?', ('c'*40, rollback['id']))
    assert claim(store, job['id'], 'owner')['job']['id'] == job['id']
