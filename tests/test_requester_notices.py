import pytest

from gene import requester_notices as notices
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
    # Its answer lost, the same finish hears what was recorded (see test_recording_again_after_a_lost_answer...).
    assert finish(store, job['id'], first['lease'], 'failed') == {'state': 'sent', 'reason': None}
    with store.connection() as db:
        row = db.execute('SELECT * FROM requester_notices WHERE job_id=?', (job['id'],)).fetchone()
        assert (row['state'], row['message_id'], row['lease']) == ('sent', '555', first['lease'])
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
                       json={**owner, 'lease': lease, 'state': 'sent', 'message_id': '42'}).json() == {'ok': True, 'state': 'sent', 'reason': None}
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
                       json={**owner, 'lease': lease, 'state': 'failed'}).json() == {'ok': True, 'state': 'failed', 'reason': None}
    # A Fri from before the owner-scoped finish (a Fri-only rollback) sends just the lease, and its
    # outcome still counts: a refusal it saw can be tried again.
    lease = client.post(route, headers=headers('fri'), json=owner).json()['lease']
    assert client.post(route+'/finish', headers=headers('fri'), json={'lease': lease, 'state': 'failed'}).json() == {'ok': True, 'state': 'failed', 'reason': None}
    lease = client.post(route, headers=headers('fri'), json=owner).json()['lease']
    assert client.post(route+'/finish', headers=headers('fri'),
                       json={'lease': lease, 'state': 'sent', 'message_id': '42'}).json() == {'ok': True, 'state': 'sent', 'reason': None}
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


def erased(store):
    store.erase_user('20')


def rolled_back(store):
    rollback = store.rollback('123', 'owner')
    deploying = store.claim_deploy()
    assert deploying['id'] == rollback['id']
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Rolled back.')


def newer_release(store):
    feature = proposed(store, sha='c'*40, source='message-9')
    store.approve(feature['id'], 'c'*40, 'owner')
    deploying = store.claim_deploy()
    store.finish_deploy(deploying['id'], deploying['lease'], True, 'Release checks passed.')


def going_out(store):
    feature = proposed(store, sha='c'*40, source='message-9')
    store.approve(feature['id'], 'c'*40, 'owner')
    store.claim_deploy()


def rollback_waiting(store):
    store.rollback('123', 'owner')


def runtime_unknown(store):  # A rollback that failed, and putting the fix back never proved healthy either.
    rollback = store.rollback('123', 'owner')
    deploying = store.claim_deploy()
    store.finish_deploy(deploying['id'], deploying['lease'], False, 'Red did not come back.', runtime_unknown=True)
    assert store.status()['active_sha'] == 'b'*40 and rollback['id'] == deploying['id']


@pytest.mark.parametrize('change,reason', [
    (erased, 'no longer on record'), (rolled_back, 'rollback ran'), (newer_release, 'newer release'),
    (going_out, 'started going out'), (rollback_waiting, 'rollback is waiting'), (runtime_unknown, 'runtime is unknown')])
def test_a_reply_sent_after_its_claim_stopped_holding_is_not_recorded_as_sent(store, change, reason):
    """gene #47: the claim's checks run again when the reply is recorded, so one that landed while the claim was
    on its way to Fri, or while Fri sent, is caught. The reply may be up, so it is never tried again."""
    job = shipped(store)
    lease = claim(store, job['id'], 'owner')['lease']
    change(store)
    result = finish(store, job['id'], lease, 'sent', '555')
    assert result['state'] == 'invalidated' and reason in result['reason'] and 'tap again' not in result['reason']
    with store.connection() as db:
        row = db.execute('SELECT * FROM requester_notices WHERE job_id=?', (job['id'],)).fetchone()
        assert (row['state'], row['message_id'], row['lease']) == ('invalidated', '', lease)  # No link to the reply kept.
        audit = db.execute('SELECT actor,detail FROM audit WHERE action="requester_notice_invalidated" AND target=?',
                           (job['id'],)).fetchone()
        assert audit['actor'] == 'controller' and reason in audit['detail']
    with pytest.raises(Conflict):
        claim(store, job['id'], 'owner')
    assert finish(store, job['id'], lease, 'sent', '555') == result  # Heard again, changing nothing.


def test_a_reply_whose_claim_still_holds_is_recorded_as_sent(store):
    job = shipped(store)
    lease = claim(store, job['id'], 'owner')['lease']
    assert finish(store, job['id'], lease, 'sent', '555') == {'state': 'sent', 'reason': None}


@pytest.mark.parametrize('outcome', ['failed', 'uncertain'])
def test_an_unsent_reply_keeps_its_outcome_whatever_landed(store, outcome):
    """Only "sent" claims the reply holds; a refusal or an unconfirmed send is recorded as Fri saw it."""
    job = shipped(store)
    lease = claim(store, job['id'], 'owner')['lease']
    rolled_back(store)
    assert finish(store, job['id'], lease, outcome) == {'state': outcome, 'reason': None}


def test_fri_hears_when_a_sent_reply_was_not_recorded_as_sent(tmp_path):
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
    erased(store)
    answer = client.post(route+'/finish', headers=headers('fri'), json={**owner, 'lease': lease, 'state': 'sent',
                                                                        'message_id': '42'})
    assert answer.status_code == 200
    assert answer.json()['ok'] is True and answer.json()['state'] == 'invalidated' and answer.json()['reason']
    advertised = contract()
    assert 'requester-notice-recheck' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities']
                                                      if c != 'requester-notice-recheck']})


def notice_row(store, job_id):
    with store.connection() as db:
        return dict(db.execute('SELECT * FROM requester_notices WHERE job_id=?', (job_id,)).fetchone())


def aged(store, job_id, seconds):
    with store.connection() as db:
        db.execute('UPDATE requester_notices SET at=at-? WHERE job_id=?', (seconds, job_id))


def test_a_prepared_claim_is_sent_only_once_authorized(store):
    """fri #214: a Fri that asks for the send right before it claims only to prepare, so the claim's checks run
    again after its last Discord read, with nothing awaited between them and the send."""
    job = shipped(store)
    first = notices.claim(store, job['id'], 'owner', prepare=True)
    assert notice_row(store, job['id'])['state'] == 'preparing' and 'reconcile' not in first
    with pytest.raises(Conflict, match='Another tap'):
        notices.claim(store, job['id'], 'owner', prepare=True)  # A second tap never races the first.
    with pytest.raises(Conflict, match='no longer holds'):
        notices.authorize(store, job['id'], 'f'*32)
    notices.authorize(store, job['id'], first['lease'])
    assert notice_row(store, job['id'])['state'] == 'authorized'
    with pytest.raises(Conflict, match='no longer holds'):
        notices.authorize(store, job['id'], first['lease'])  # Once: the send it cleared may have posted.
    with pytest.raises(Conflict, match='may already'):
        notices.claim(store, job['id'], 'owner', prepare=True)
    assert finish(store, job['id'], first['lease'], 'sent', '555') == {'state': 'sent', 'reason': None}
    with pytest.raises(Conflict, match='already told'):
        notices.claim(store, job['id'], 'owner', prepare=True)


@pytest.mark.parametrize('change,reason', [
    (erased, 'Only a shipped repair'), (rolled_back, 'rollback ran'), (newer_release, 'newer release'),
    (going_out, 'going out'), (rollback_waiting, 'rollback is waiting'), (runtime_unknown, 'runtime is unknown')])
def test_a_change_before_the_send_refuses_it(store, change, reason):
    """The reporter's erasure, or a release that landed, while Fri read Discord: nothing posts, and the claim
    is recorded as refused, so once the fix holds again a later tap may try."""
    job = shipped(store)
    lease = notices.claim(store, job['id'], 'owner', prepare=True)['lease']
    change(store)
    with pytest.raises(Conflict, match=reason):
        notices.authorize(store, job['id'], lease)
    assert notice_row(store, job['id'])['state'] == 'preparing'
    assert finish(store, job['id'], lease, 'failed') == {'state': 'failed', 'reason': None}


def test_a_claim_whose_fri_stopped_before_the_send_is_taken_over(store):
    """gene #139: preparing never posts, so a claim left by a Fri that stopped is tried again once it's old,
    and the stopped one can never send."""
    job = shipped(store)
    stopped = notices.claim(store, job['id'], 'owner', prepare=True)['lease']
    aged(store, job['id'], notices.PREPARE_SECONDS+1)
    lease = notices.claim(store, job['id'], 'owner', prepare=True)['lease']
    with pytest.raises(Conflict, match='no longer holds'):
        notices.authorize(store, job['id'], stopped)
    with pytest.raises(Conflict, match='already been recorded'):
        finish(store, job['id'], stopped, 'failed')
    notices.authorize(store, job['id'], lease)
    assert finish(store, job['id'], lease, 'sent', '555')['state'] == 'sent'


def test_an_older_fri_can_take_over_a_stopped_preparation_but_never_a_send(store):
    job = shipped(store)
    notices.claim(store, job['id'], 'owner', prepare=True)
    with pytest.raises(Conflict, match='Another tap'):
        claim(store, job['id'], 'owner')
    aged(store, job['id'], notices.PREPARE_SECONDS+1)
    lease = claim(store, job['id'], 'owner')['lease']  # An older Fri: its claim clears the send, as before.
    assert notice_row(store, job['id'])['state'] == 'sending'
    aged(store, job['id'], notices.SETTLE_SECONDS+1)
    with pytest.raises(Conflict, match='may already'):
        claim(store, job['id'], 'owner')  # It can't look in Discord for what may have posted.
    assert finish(store, job['id'], lease, 'uncertain')['state'] == 'uncertain'


@pytest.mark.parametrize('prepared', [True, False])
def test_a_send_never_recorded_is_looked_for_before_it_is_tried_again(store, prepared):
    """gene #139: an authorized send whose Fri stopped may have posted. Once it has had time to settle, the next
    tap has Fri look for it in Discord first; until then, and for an older Fri, it's never tried again."""
    job = shipped(store)
    stopped = notices.claim(store, job['id'], 'owner', prepare=prepared)['lease']
    if prepared:
        notices.authorize(store, job['id'], stopped)
    sent_at = notice_row(store, job['id'])['at']
    with pytest.raises(Conflict, match='may already'):
        notices.claim(store, job['id'], 'owner', prepare=True)  # Fri may still be sending it.
    aged(store, job['id'], notices.SETTLE_SECONDS+1)
    with pytest.raises(Conflict, match='may already'):
        claim(store, job['id'], 'owner')
    looking = notices.claim(store, job['id'], 'owner', prepare=True)
    assert looking['reconcile'] == {'since': sent_at-notices.SETTLE_SECONDS-1}
    assert notice_row(store, job['id'])['state'] == 'reconciling'
    with pytest.raises(Conflict, match='already been recorded'):
        finish(store, job['id'], stopped, 'sent', '444')  # The stopped Fri's claim is over.
    again = notices.claim(store, job['id'], 'owner', prepare=True)  # A tap that stopped while looking: look again.
    assert again['reconcile'] == looking['reconcile']
    with pytest.raises(Conflict, match='no longer holds'):
        notices.authorize(store, job['id'], looking['lease'])
    notices.authorize(store, job['id'], again['lease'])  # Nothing found: this one may send.
    assert notice_row(store, job['id'])['state'] == 'authorized' and notice_row(store, job['id'])['at'] > sent_at
    assert finish(store, job['id'], again['lease'], 'sent', '555')['state'] == 'sent'


def test_a_send_found_in_discord_is_recorded_with_the_usual_checks(store):
    job = shipped(store)
    stopped = notices.claim(store, job['id'], 'owner', prepare=True)['lease']
    notices.authorize(store, job['id'], stopped)
    aged(store, job['id'], notices.SETTLE_SECONDS+1)
    lease = notices.claim(store, job['id'], 'owner', prepare=True)['lease']
    rolled_back(store)
    result = finish(store, job['id'], lease, 'sent', '444')  # Fri found the stopped tap's reply.
    assert result['state'] == 'invalidated' and 'rollback ran' in result['reason']
    assert notice_row(store, job['id'])['message_id'] == ''


@pytest.mark.parametrize('outcome', ['sent', 'failed', 'uncertain'])
def test_recording_again_after_a_lost_answer_hears_what_was_recorded(store, outcome):
    """fri #214: Fri retries a finish whose answer it lost, and an invalidated reply it never heard about
    still gets taken back. The record doesn't change, and another claim's lease still hears nothing."""
    job = shipped(store)
    lease = notices.claim(store, job['id'], 'owner', prepare=True)['lease']
    notices.authorize(store, job['id'], lease)
    erased(store)
    first = finish(store, job['id'], lease, outcome, '555' if outcome == 'sent' else '')
    assert first['state'] == ('invalidated' if outcome == 'sent' else outcome)
    before = notice_row(store, job['id'])
    assert finish(store, job['id'], lease, outcome, '555' if outcome == 'sent' else '') == first
    assert notice_row(store, job['id']) == before
    with pytest.raises(Conflict, match='already been recorded'):
        finish(store, job['id'], 'f'*32, outcome)


def test_preparing_and_authorizing_need_no_schema_change(tmp_path):
    """New states are values in the same text column, so an older controller opens the table as it is. How an
    older one treats them (it refuses a claim while any send is on its way) was checked against Gene main."""
    from gene.store import Store
    store = Store(tmp_path/'gene.sqlite3')
    store.bootstrap('a'*40)
    store.set_paused(False, 'owner')
    store.update_models([{'id': 'gpt-6-astra', 'name': 'GPT-6 Astra', 'efforts': ['high'], 'default_effort': 'high'}])
    store.select_model('gpt-6-astra', 'high', 'owner')
    job = shipped(store)
    lease = notices.claim(store, job['id'], 'owner', prepare=True)['lease']
    with store.connection() as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        columns = [row['name'] for row in db.execute('PRAGMA table_info(requester_notices)')]
    assert columns == ['job_id', 'lease', 'state', 'message_id', 'at']  # No schema change.
    notices.authorize(store, job['id'], lease)
    assert finish(store, job['id'], lease, 'sent', '555')['state'] == 'sent'


def served(tmp_path):
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
    return client, store


def test_a_tap_after_fri_stopped_before_sending_is_tried_again(tmp_path):
    """gene #139: Fri claimed, then stopped before it sent. Every later tap was refused for good."""
    client, store = served(tmp_path)
    job = shipped(store)
    route = f"/requests/{job['id']}/requester-notice"
    owner = {'guild_id': '123', 'user_id': 'owner'}
    assert client.post(route, headers=headers('fri'), json={**owner, 'prepare': True}).status_code == 200
    aged(store, job['id'], 3600)
    retried = client.post(route, headers=headers('fri'), json={**owner, 'prepare': True})
    assert retried.status_code == 200, retried.json()


def test_fri_can_ask_for_the_send_on_the_owners_behalf_only(tmp_path):
    client, store = served(tmp_path)
    job = shipped(store)
    route = f"/requests/{job['id']}/requester-notice"
    owner = {'guild_id': '123', 'user_id': 'owner'}
    claimed = client.post(route, headers=headers('fri'), json={**owner, 'prepare': True})
    assert claimed.status_code == 200 and 'reconcile' not in claimed.json()
    lease = claimed.json()['lease']
    assert notice_row(store, job['id'])['state'] == 'preparing'
    for scope in ({'guild_id': '999', 'user_id': 'owner'}, {'guild_id': '123', 'user_id': '20'}, {'lease': lease}):
        refused = client.post(route+'/authorize', headers=headers('fri'), json={**scope, 'lease': lease})
        assert refused.status_code in (403, 422)
    assert client.post(route+'/authorize', headers=headers('runtime'), json={**owner, 'lease': lease}).status_code == 401
    assert client.post(route+'/authorize', headers=headers('fri'), json={**owner, 'lease': 'x'}).status_code == 409
    assert client.post(route+'/authorize', headers=headers('fri'), json={**owner, 'lease': lease}).json() == {'ok': True}
    sent = client.post(route+'/finish', headers=headers('fri'), json={**owner, 'lease': lease, 'state': 'sent',
                                                                      'message_id': '42'})
    assert sent.json() == {'ok': True, 'state': 'sent', 'reason': None}
    again = client.post(route+'/finish', headers=headers('fri'), json={**owner, 'lease': lease, 'state': 'sent',
                                                                       'message_id': '42'})
    assert again.json() == sent.json()
    advertised = contract()
    assert 'requester-notice-authorize' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities']
                                                      if c != 'requester-notice-authorize']})
