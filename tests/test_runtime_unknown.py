"""gene #48: a deployment whose restore also failed leaves the runtime unknown, not the old release live."""
import json
import re
import sqlite3
import time
from contextlib import closing
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.deploy import Deployer
from gene.requester_notices import claim
from gene.store import Conflict, Store
from tests.conftest import proposed
from tests.test_requester_notices import shipped

UNKNOWN = 'Release and automatic rollback both failed. Operator recovery required'


def deployer(root, healthy, api=None):
    result = Deployer.__new__(Deployer)
    result.root, result.api = root, api or Mock()
    result.prepare, result.compose, result.activate = Mock(), Mock(), Mock()
    result.healthy = Mock(side_effect=healthy)
    return result


@pytest.mark.parametrize('healthy,unknown', [((True,), False), ((False, True), False), ((False, False), True)])
def test_only_a_restore_that_never_proved_healthy_leaves_the_runtime_unknown(tmp_path, healthy, unknown):
    Store(tmp_path/'data/controller/gene.sqlite3').bootstrap('a'*40)
    (tmp_path/'data/red').mkdir()
    worker = deployer(tmp_path, healthy)
    worker.api.post.side_effect = [{'id': 'f'*12, 'lease': 'e'*32, 'kind': 'feature',
                                    'proposal': {'sha': 'b'*40, 'base_sha': 'a'*40}}, {'ok': True}]
    assert worker.tick()
    sent = worker.api.post.call_args_list[-1].args
    assert sent[0] == '/deploy/'+'f'*12+'/finish'
    assert sent[1].get('runtime_unknown', False) is unknown
    assert sent[1]['success'] is (healthy == (True,))


def test_a_restore_that_cannot_even_start_leaves_the_runtime_unknown(tmp_path):
    Store(tmp_path/'data/controller/gene.sqlite3').bootstrap('a'*40)
    (tmp_path/'data/red').mkdir()
    worker = deployer(tmp_path, (False,))
    worker.activate.side_effect = [None, OSError('compose failed')]
    worker.api.post.side_effect = [{'id': 'f'*12, 'lease': 'e'*32, 'kind': 'feature',
                                    'proposal': {'sha': 'b'*40, 'base_sha': 'a'*40}}, {'ok': True}]
    assert worker.tick()
    assert worker.api.post.call_args_list[-1].args[1]['runtime_unknown'] is True


def test_a_failure_before_red_stops_changes_nothing_it_has_to_report(tmp_path):
    worker = deployer(tmp_path, ())
    worker.prepare.side_effect = RuntimeError('The previous approved release is missing')
    worker.api.post.side_effect = [{'id': 'f'*12, 'lease': 'e'*32, 'kind': 'rollback',
                                    'proposal': {'sha': 'b'*40, 'base_sha': 'a'*40}}, {'ok': True}]
    assert worker.tick()
    worker.compose.assert_not_called()
    assert 'runtime_unknown' not in worker.api.post.call_args_list[-1].args[1]


def test_the_unknown_runtime_survives_a_lost_acknowledgement_and_a_restart(tmp_path):
    worker = deployer(tmp_path, ())
    worker.pending_result = {'job_id': 'a'*12, 'lease': 'b'*32, 'success': False, 'detail': UNKNOWN}
    worker.pending_unknown = True
    worker.api.post.side_effect = httpx.ConnectError('Controller unavailable')
    with pytest.raises(httpx.HTTPError):
        worker.tick()
    restarted = deployer(tmp_path, ())
    restarted.api.post.return_value = {'ok': True}
    assert restarted.tick()
    restarted.api.post.assert_called_once_with('/deploy/'+'a'*12+'/finish', {
        'lease': 'b'*32, 'success': False, 'detail': UNKNOWN, 'runtime_unknown': True})
    assert not (tmp_path/'deploy-result.json').exists() and not (tmp_path/'deploy-runtime.json').exists()


def an_older_deployer_accepts(receipt):
    """The receipt check of every deployer before gene #48 (4c51f3c, src/gene/deploy.py), verbatim. One it refuses
    stops it before the controller on every tick, so it never claims again, owner rollbacks included."""
    return not (not isinstance(receipt, dict) or set(receipt) != {'job_id', 'lease', 'success', 'detail'}
                or not re.fullmatch(r'[a-f0-9]{12}', str(receipt['job_id']))
                or not re.fullmatch(r'[a-f0-9]{32}', str(receipt['lease']))
                or type(receipt['success']) is not bool or not isinstance(receipt['detail'], str))


@pytest.mark.parametrize('healthy', [(True,), (False, True), (False, False)])
def test_a_pending_receipt_stays_one_an_older_deployer_can_finish(tmp_path, healthy):
    Store(tmp_path/'data/controller/gene.sqlite3').bootstrap('a'*40)
    (tmp_path/'data/red').mkdir()
    worker = deployer(tmp_path, healthy)
    worker.api.post.side_effect = [{'id': 'f'*12, 'lease': 'e'*32, 'kind': 'feature',
                                    'proposal': {'sha': 'b'*40, 'base_sha': 'a'*40}},
                                   httpx.ConnectError('Acknowledgement lost')]
    with pytest.raises(httpx.HTTPError):
        worker.tick()
    # Downgraded now, the deployer replays this file before anything else, every tick.
    receipt = json.loads((tmp_path/'deploy-result.json').read_text())
    assert an_older_deployer_accepts(receipt)
    assert receipt == {'job_id': 'f'*12, 'lease': 'e'*32, 'success': healthy == (True,), 'detail': receipt['detail']}
    # Kept on, it still reports what it saw.
    restarted = deployer(tmp_path, ())
    restarted.api.post.return_value = {'ok': True}
    assert restarted.tick()
    assert restarted.api.post.call_args.args[1].get('runtime_unknown', False) is (healthy == (False, False))
    assert not (tmp_path/'deploy-result.json').exists() and not (tmp_path/'deploy-runtime.json').exists()


def test_evidence_an_older_deployer_left_behind_speaks_for_no_other_receipt(tmp_path):
    # An older deployer finished the receipt this belonged to without reading it, then the deployer came back.
    (tmp_path/'deploy-runtime.json').write_text(json.dumps({'job_id': 'c'*12, 'lease': 'd'*32,
                                                            'runtime_unknown': True}))
    (tmp_path/'deploy-result.json').write_text(json.dumps({'job_id': 'a'*12, 'lease': 'b'*32, 'success': False,
                                                           'detail': 'The previous approved release is missing'}))
    worker = deployer(tmp_path, ())
    worker.api.post.return_value = {'ok': True}
    assert worker.tick()
    worker.api.post.assert_called_once_with('/deploy/'+'a'*12+'/finish', {
        'lease': 'b'*32, 'success': False, 'detail': 'The previous approved release is missing'})
    assert not (tmp_path/'deploy-runtime.json').exists()


@pytest.mark.parametrize('receipt,evidence', [
    ({'success': True}, {'runtime_unknown': True}),  # A healthy release is a known one.
    ({'success': False}, {'runtime_unknown': False}),  # Written only when it holds.
    ({'success': False}, {'runtime_unknown': 'yes'}),
    ({'success': False}, {'runtime_unknown': True, 'detail': 'x'}),
    ({'success': False, 'runtime_unknown': True}, None)])  # Beside the receipt, never in it.
def test_contradictory_unknown_runtime_evidence_needs_an_operator(tmp_path, receipt, evidence):
    (tmp_path/'deploy-result.json').write_text(json.dumps(
        {'job_id': 'a'*12, 'lease': 'b'*32, 'detail': 'x', **receipt}))
    if evidence is not None:
        (tmp_path/'deploy-runtime.json').write_text(json.dumps({'job_id': 'a'*12, 'lease': 'b'*32, **evidence}))
    worker = deployer(tmp_path, ())
    with pytest.raises(ValueError, match='operator reconciliation'):
        worker.tick()
    worker.api.post.assert_not_called()


def unknown_after_failed_rollback(store):
    """The issue's repro: ship repair B, roll back to A, A fails health and restoring B fails too."""
    job = shipped(store)
    rollback = store.rollback('123', 'owner')
    deploying = store.claim_deploy()
    assert deploying['id'] == rollback['id']
    store.finish_deploy(deploying['id'], deploying['lease'], False, UNKNOWN, runtime_unknown=True)
    return job, rollback


def test_a_failed_restore_marks_the_runtime_unknown_and_the_reporter_hears_nothing(store):
    job, rollback = unknown_after_failed_rollback(store)
    status = store.status()
    assert status['active_sha'] == 'b'*40  # Still the last release a health check proved.
    assert status['runtime_unknown'] == {'job_id': rollback['id'], 'kind': 'rollback', 'sha': 'a'*40,
                                         'restore_sha': 'b'*40, 'since': pytest.approx(time.time(), abs=60)}
    with pytest.raises(Conflict, match='unknown'):
        claim(store, job['id'], 'owner')
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM audit WHERE action="runtime_unknown" AND target=?',
                          (rollback['id'],)).fetchone()[0] == 1
        assert not db.execute('SELECT 1 FROM requester_notices').fetchone()


def test_an_unknown_runtime_holds_automatic_releases_but_not_a_rollback(store):
    feature = proposed(store)
    store.approve(feature['id'], 'b'*40, 'owner')
    first = store.claim_deploy()
    store.finish_deploy(first['id'], first['lease'], False, UNKNOWN, runtime_unknown=True)
    queued = proposed(store, sha='c'*40, source='message-2')
    store.approve(queued['id'], 'c'*40, 'owner')
    assert store.claim_deploy() is None
    assert store.get(queued['id'])['state'] == 'approved'  # Untouched: it waits, like outside the window.
    with store.connection() as db:  # An earlier release to go back to.
        db.execute('UPDATE settings SET value=? WHERE key="previous_sha"', ('d'*40,))
    rollback = store.rollback('guild', 'owner')
    assert store.claim_deploy()['id'] == rollback['id']


def test_a_failure_that_changed_nothing_keeps_the_runtime_unknown(store):
    feature = proposed(store)
    store.approve(feature['id'], 'b'*40, 'owner')
    first = store.claim_deploy()
    store.finish_deploy(first['id'], first['lease'], False, UNKNOWN, runtime_unknown=True)
    with store.connection() as db:
        db.execute('UPDATE settings SET value=? WHERE key="previous_sha"', ('d'*40,))
    rollback = store.rollback('guild', 'owner')
    claimed = store.claim_deploy()
    store.finish_deploy(claimed['id'], claimed['lease'], False, 'The previous approved release is missing')
    assert store.status()['runtime_unknown']['job_id'] == first['id']
    assert store.get(rollback['id'])['state'] == 'failed'


def test_a_healthy_deployment_makes_the_runtime_known_again(store):
    job, _ = unknown_after_failed_rollback(store)
    again = store.rollback('123', 'owner')
    claimed = store.claim_deploy()
    assert claimed['id'] == again['id']
    store.finish_deploy(claimed['id'], claimed['lease'], True, 'Deployed approved commit; healthy.')
    assert store.status()['runtime_unknown'] is None
    assert store.status()['active_sha'] == 'a'*40
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM audit WHERE action="runtime_known" AND target=?',
                          (again['id'],)).fetchone()[0] == 1
    with pytest.raises(Conflict, match='rollback'):
        claim(store, job['id'], 'owner')  # The repair is no longer live: the existing rule still applies.


def test_an_unknown_runtime_receipt_is_idempotent_and_cannot_change(store):
    feature = proposed(store)
    store.approve(feature['id'], 'b'*40, 'owner')
    first = store.claim_deploy()
    store.finish_deploy(first['id'], first['lease'], False, UNKNOWN, runtime_unknown=True)
    store.finish_deploy(first['id'], first['lease'], False, UNKNOWN, runtime_unknown=True)
    with pytest.raises(Conflict, match='differs'):
        store.finish_deploy(first['id'], first['lease'], False, 'Release failed health checks')
    with pytest.raises(ValueError):
        store.finish_deploy('f'*12, 'e'*32, True, 'Healthy', runtime_unknown=True)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM audit WHERE action="runtime_unknown"').fetchone()[0] == 1


def api_client(tmp_path):
    settings = Settings(tmp_path/'data/controller/gene.sqlite3', 'guild', 'owner',
                        'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32, fri_token='fri-'+'x'*32)
    return TestClient(create_app(settings))


def deploy_post(client):
    def post(path, data=None):
        response = client.post(path, json=data or {}, headers={'Authorization': 'Bearer deploy-'+'x'*32})
        response.raise_for_status()
        return response.json()
    return post


def test_the_deployer_reports_it_and_the_owner_sees_it_in_status(tmp_path):
    with api_client(tmp_path) as client:
        store = client.app.state.store
        store.bootstrap('a'*40)
        store.update_models([{'id': 'gpt-6.1-sol', 'name': 'Sol', 'efforts': ['high'], 'default_effort': 'high'}])
        store.set_paused(False, 'owner')
        job = proposed(store)
        store.approve(job['id'], 'b'*40, 'owner')
        (tmp_path/'data/red').mkdir(parents=True)

        worker = deployer(tmp_path, (False, False), api=Mock(post=deploy_post(client)))
        assert worker.tick()
        status = client.get('/status', headers={'Authorization': 'Bearer fri-'+'x'*32}).json()
        assert status['active_sha'] == 'a'*40
        assert status['runtime_unknown']['job_id'] == job['id']
        assert status['runtime_unknown']['restore_sha'] == 'a'*40
        assert store.get(job['id'])['state'] == 'failed'
        # A healthy release is a known one, so a deployer can't report both.
        assert client.post('/deploy/'+'f'*12+'/finish', headers={'Authorization': 'Bearer deploy-'+'x'*32},
                           json={'lease': 'e'*32, 'success': True, 'detail': 'x', 'runtime_unknown': True}
                           ).status_code == 422


def health(path, age=0.0, **changes):
    path.write_text(json.dumps({'sha': 'b'*40, 'guild_connected': True, 'controls_running': True,
                                'at': time.time()-age, **changes}))


def test_a_maintainer_clears_it_once_the_recorded_release_is_running_and_healthy(store, tmp_path, capsys):
    from gene.runtime_state import main
    job, rollback = unknown_after_failed_rollback(store)
    db, beat = str(store.path), tmp_path/'gene-health.json'
    common = ['--actor', 'maintainer:claude', '--reason', 'Restored B by hand; heartbeat checked']
    assert main(['status', db]) == 0
    assert json.loads(capsys.readouterr().out)['runtime_unknown']['job_id'] == rollback['id']
    for sha, beat_changes, expected in (
            ('a'*40, {'sha': 'a'*40}, 'records'),  # Healthy, but not the release the controller records as live.
            ('b'*40, {'sha': 'a'*40}, 'heartbeat'),
            ('b'*40, {'guild_connected': False}, 'heartbeat'),
            ('b'*40, {'controls_running': False}, 'heartbeat'),
            ('b'*40, {'age': 120}, 'heartbeat')):
        age = beat_changes.pop('age', 0.0)
        health(beat, age, **beat_changes)
        assert main(['clear', db, '--sha', sha, '--health', str(beat), *common]) != 0
        assert expected in capsys.readouterr().err
        assert store.status()['runtime_unknown'] is not None
    health(beat)
    assert main(['clear', db, '--sha', 'b'*40, '--health', str(beat), '--actor', 'someone',
                 '--reason', 'x']) != 0
    assert store.status()['runtime_unknown'] is not None
    log = tmp_path/'production-actions.jsonl'
    assert main(['clear', db, '--sha', 'b'*40, '--health', str(beat), '--log', str(log), *common]) == 0
    assert store.status()['runtime_unknown'] is None
    with store.connection() as conn:
        row = conn.execute('SELECT actor,target,detail FROM audit WHERE action="runtime_reconciled"').fetchone()
    assert (row['actor'], row['target']) == ('maintainer:claude', 'b'*40)
    assert 'heartbeat checked' in row['detail'] and rollback['id'] in row['detail']
    logged = json.loads(log.read_text())
    assert (logged['actor'], logged['outcome'], logged['after']) == ('maintainer:claude', 'ok', {'gene': 'b'*40})
    assert main(['clear', db, '--sha', 'b'*40, '--health', str(beat), *common]) != 0  # Nothing left to clear.
    assert claim(store, job['id'], 'owner')['job']['id'] == job['id']


def test_clearing_waits_for_a_deployment_in_flight(store, tmp_path):
    from gene.runtime_state import reconcile
    unknown_after_failed_rollback(store)
    store.rollback('123', 'owner')
    store.claim_deploy()
    with pytest.raises(Conflict, match='going out'), store.connection() as db:
        reconcile(db, 'b'*40, 'maintainer:claude', 'checked', tmp_path/'gene-health.json')


def an_older_controller_finishes(store, job, detail):
    """What a controller from before gene #48 commits for a failed deployment (4c51f3c, Store.finish_deploy): it
    ignores runtime_unknown, so it records no state and its receipt holds only success and detail."""
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE jobs SET state="failed",result=?,lease=NULL,updated=? WHERE id=?',
                   (detail, time.time(), job['id']))
        db.execute('UPDATE deployments SET state="failed" WHERE job_id=?', (job['id'],))
        db.execute('INSERT INTO deployment_receipts VALUES (?,?,?)',
                   (job['id'], job['lease'], json.dumps({'success': False, 'detail': detail}, sort_keys=True)))
        Store._audit(db, 'deployer', 'failed', job['id'], detail)


def deploying(store, sha='b'*40, source='message-1'):
    job = proposed(store, sha=sha, source=source)
    store.approve(job['id'], sha, 'owner')
    return store.claim_deploy()


def test_receipts_are_recorded_the_way_every_controller_compares_them(store):
    job = deploying(store)
    store.finish_deploy(job['id'], job['lease'], False, UNKNOWN, runtime_unknown=True)
    with store.connection() as db:  # A Gene rollback below gene #48 compares a replay with exactly this.
        result = db.execute('SELECT result FROM deployment_receipts WHERE job_id=?', (job['id'],)).fetchone()[0]
    assert result == json.dumps({'success': False, 'detail': UNKNOWN}, sort_keys=True)
    # So a deployer downgraded below it, replaying without the evidence, is acknowledged and changes nothing.
    store.finish_deploy(job['id'], job['lease'], False, UNKNOWN)
    assert store.status()['runtime_unknown']['job_id'] == job['id']


def test_a_receipt_an_older_controller_committed_replays_after_the_upgrade(tmp_path):
    with api_client(tmp_path) as client:
        store = client.app.state.store
        store.bootstrap('a'*40)
        store.update_models([{'id': 'gpt-6.1-sol', 'name': 'Sol', 'efforts': ['high'], 'default_effort': 'high'}])
        store.set_paused(False, 'owner')
        job = deploying(store)
        # The older controller committed the failure, ignoring runtime_unknown; its acknowledgement was lost.
        an_older_controller_finishes(store, job, UNKNOWN)
        (tmp_path/'deploy-result.json').write_text(json.dumps({'job_id': job['id'], 'lease': job['lease'],
                                                               'success': False, 'detail': UNKNOWN}))
        (tmp_path/'deploy-runtime.json').write_text(json.dumps({'job_id': job['id'], 'lease': job['lease'],
                                                                'runtime_unknown': True}))
        worker = deployer(tmp_path, (), api=Mock(post=deploy_post(client)))
        assert worker.tick()
        assert not (tmp_path/'deploy-result.json').exists()  # Acknowledged, so the deployer claims again.
        state = store.status()['runtime_unknown']  # The evidence the older controller dropped still counts.
        assert (state['job_id'], state['restore_sha']) == (job['id'], 'a'*40)
        assert store.get(job['id'])['state'] == 'failed'
        store.finish_deploy(job['id'], job['lease'], False, UNKNOWN, runtime_unknown=True)  # Lost again.
        with store.connection() as db:
            assert db.execute('SELECT count(*) FROM audit WHERE action="runtime_unknown"').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM audit WHERE action="failed"').fetchone()[0] == 1
        for lease, detail in (('0'*32, UNKNOWN), (job['lease'], 'Something else')):
            with pytest.raises(Conflict, match='differs'):
                store.finish_deploy(job['id'], lease, False, detail, runtime_unknown=True)


def test_late_evidence_never_rewinds_what_has_happened_since(store, tmp_path):
    from gene.runtime_state import main
    # A later deployment went out healthy.
    job = deploying(store)
    an_older_controller_finishes(store, job, UNKNOWN)
    later = deploying(store, source='message-2')
    store.finish_deploy(later['id'], later['lease'], True, 'Deployed approved commit; healthy.')
    store.finish_deploy(job['id'], job['lease'], False, UNKNOWN, runtime_unknown=True)
    assert store.status()['runtime_unknown'] is None
    # A later deployment was claimed, even one whose own restore proved healthy.
    job = deploying(store, sha='c'*40, source='message-5')
    an_older_controller_finishes(store, job, UNKNOWN)
    later = deploying(store, sha='c'*40, source='message-6')
    store.finish_deploy(later['id'], later['lease'], False, 'Release failed health checks; restored')
    assert store.status()['active_sha'] == job['proposal']['base_sha']
    store.finish_deploy(job['id'], job['lease'], False, UNKNOWN, runtime_unknown=True)
    assert store.status()['runtime_unknown'] is None
    # A maintainer release recorded another one as live.
    job = deploying(store, sha='c'*40, source='message-3')
    an_older_controller_finishes(store, job, UNKNOWN)
    with store.connection() as db:
        db.execute('UPDATE settings SET value=? WHERE key="active_sha"', ('d'*40,))
    store.finish_deploy(job['id'], job['lease'], False, UNKNOWN, runtime_unknown=True)
    assert store.status()['runtime_unknown'] is None
    # A maintainer found the recorded release running and healthy.
    first = deploying(store, sha='e'*40, source='message-4')
    store.finish_deploy(first['id'], first['lease'], False, UNKNOWN, runtime_unknown=True)
    rollback = store.rollback('123', 'owner')
    job = store.claim_deploy()
    assert job['id'] == rollback['id']
    an_older_controller_finishes(store, job, UNKNOWN)
    health(tmp_path/'gene-health.json', sha='d'*40)
    assert main(['clear', str(store.path), '--sha', 'd'*40, '--health', str(tmp_path/'gene-health.json'),
                 '--actor', 'maintainer:claude', '--reason', 'Checked d by hand']) == 0
    store.finish_deploy(job['id'], job['lease'], False, UNKNOWN, runtime_unknown=True)
    assert store.status()['runtime_unknown'] is None


CLEAR = ['--actor', 'maintainer:claude', '--reason', 'Heartbeat checked']


def test_a_heartbeat_from_before_the_runtime_became_unknown_clears_nothing(store, tmp_path):
    from gene.runtime_state import main
    beat = tmp_path/'gene-health.json'
    health(beat)  # B, healthy, the moment before the rollback stopped Red. Both releases then failed at once.
    unknown_after_failed_rollback(store)
    assert time.time()-json.loads(beat.read_text())['at'] < 25  # Still fresh by age alone.
    assert main(['clear', str(store.path), '--sha', 'b'*40, '--health', str(beat), *CLEAR]) != 0
    assert store.status()['runtime_unknown'] is not None


def test_a_heartbeat_from_before_a_newer_failure_clears_nothing(store, tmp_path):
    from gene.runtime_state import main
    first = deploying(store)
    store.finish_deploy(first['id'], first['lease'], False, UNKNOWN, runtime_unknown=True)
    beat = tmp_path/'gene-health.json'
    health(beat, sha='a'*40)  # A proved running again after the first failure...
    with store.connection() as db:
        db.execute('UPDATE settings SET value=? WHERE key="previous_sha"', ('d'*40,))
    rollback = store.rollback('guild', 'owner')
    claimed = store.claim_deploy()
    store.finish_deploy(claimed['id'], claimed['lease'], False, UNKNOWN, runtime_unknown=True)  # ...then this one.
    assert main(['clear', str(store.path), '--sha', 'a'*40, '--health', str(beat), *CLEAR]) != 0
    assert store.status()['runtime_unknown']['job_id'] == rollback['id']


def test_the_heartbeat_is_read_under_the_lock_that_clears_it(store, tmp_path, monkeypatch):
    from gene import runtime_state
    unknown_after_failed_rollback(store)
    beat = tmp_path/'gene-health.json'
    health(beat)
    read = runtime_state.heartbeat

    def contended(*args):
        # A deployment failing now can't mark the runtime unknown again between this read and the clear.
        with closing(sqlite3.connect(store.path, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
        return read(*args)
    monkeypatch.setattr(runtime_state, 'heartbeat', contended)
    assert runtime_state.main(['clear', str(store.path), '--sha', 'b'*40, '--health', str(beat), *CLEAR]) == 0
    assert store.status()['runtime_unknown'] is None
