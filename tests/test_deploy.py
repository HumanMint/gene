from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.deploy import Deployer
from gene.store import Store
from tests.conftest import proposed, request


@pytest.mark.parametrize('candidate_healthy,rollback_healthy', [(True, True), (False, True), (False, False)])
def test_release_health_and_automatic_code_recovery(tmp_path, monkeypatch, candidate_healthy, rollback_healthy):
    previous, candidate = 'a' * 40, 'b' * 40
    Store(tmp_path / 'data/controller/gene.sqlite3').bootstrap(previous)
    red = tmp_path / 'data/red'
    red.mkdir()
    (red / 'settings.json').write_text('{"persisted": true}')
    deployer = Deployer.__new__(Deployer)
    deployer.root = tmp_path
    deployer.prepare = Mock()
    deployer.compose = Mock()
    deployer.activate = Mock()
    deployer.healthy = Mock(side_effect=[candidate_healthy, rollback_healthy])
    job = {'id': 'job', 'proposal': {'sha': candidate, 'base_sha': previous}}

    if candidate_healthy:
        deployer.process(job)
        assert deployer.activate.call_args_list == [((candidate,),)]
    else:
        expected = 'previous code was restored' if rollback_healthy else 'Operator recovery required'
        with pytest.raises(RuntimeError, match=expected):
            deployer.process(job)
        assert [call.args[0] for call in deployer.activate.call_args_list] == [candidate, previous]
    deployer.compose.assert_called_once_with('stop', 'red')
    assert (tmp_path / 'backups/job/controller.sqlite3').is_file()
    assert (tmp_path / 'backups/job/red.tar.gz').is_file()
    assert (red / 'settings.json').read_text() == '{"persisted": true}'


def test_feature_activation_and_rollback_preserve_the_trusted_runtime_image(tmp_path):
    config = tmp_path / 'release.env'
    config.write_text('GENE_RELEASE_SHA=' + 'a' * 40 + '\nGENE_RUNTIME_IMAGE=gene-runtime:review-v1\n')
    deployer = Deployer.__new__(Deployer)
    deployer.root = tmp_path
    deployer.compose = Mock()
    deployer.activate('b' * 40)
    assert 'GENE_RUNTIME_IMAGE=gene-runtime:review-v1\n' in config.read_text()
    deployer.activate('a' * 40)
    assert 'GENE_RUNTIME_IMAGE=gene-runtime:review-v1\n' in config.read_text()


@pytest.mark.parametrize('lost_after_commit', [False, True])
@pytest.mark.parametrize('success', [False, True])
def test_completion_survives_connection_loss_and_deployer_restart(tmp_path, success, lost_after_commit):
    settings = Settings(tmp_path/'controller.sqlite3', 'guild', 'owner',
                        'r'*32, 'w'*32, 'd'*32, fri_token='f'*32)
    with TestClient(create_app(settings)) as client:
        store = client.app.state.store
        store.bootstrap('a'*40)
        store.update_models([{'id': 'gpt-6.1-sol', 'name': 'Sol',
                              'efforts': ['high'], 'default_effort': 'high'}])
        store.set_paused(False, 'owner')
        job = proposed(store)
        store.approve(job['id'], 'b'*40, 'owner')
        request(store, source='next-job')
        failed_once, paths = False, []

        def post(path, data=None):
            nonlocal failed_once
            paths.append(path)
            lose = path.endswith('/finish') and not failed_once
            if lose:
                failed_once = True
            if lose and not lost_after_commit:
                raise httpx.ConnectError('Controller unavailable before receipt arrived')
            response = client.post(path, json=data or {}, headers={'Authorization': 'Bearer '+'d'*32})
            response.raise_for_status()
            if lose:
                raise httpx.ReadError('Controller committed but its acknowledgement was lost')
            return response.json()

        deployer = Deployer.__new__(Deployer)
        deployer.root = tmp_path
        deployer.api = Mock(post=post)
        deployer.process = Mock(side_effect=None if success else RuntimeError('Candidate failed; previous code restored'))
        with pytest.raises(httpx.HTTPError):
            deployer.tick()
        deployer.process.assert_called_once()
        assert (tmp_path/'deploy-result.json').stat().st_mode & 0o777 == 0o600
        if not lost_after_commit:
            assert store.get(job['id'])['state'] == 'deploying'
            assert store.claim() is None

        # New process, same durable result. No Docker activation or new claim may occur.
        restarted = Deployer.__new__(Deployer)
        restarted.root, restarted.api = tmp_path, deployer.api
        restarted.process = Mock(side_effect=AssertionError('Never repeat a completed deployment'))
        assert restarted.tick()
        restarted.process.assert_not_called()
        assert paths == ['/deploy/claim', f"/deploy/{job['id']}/finish", f"/deploy/{job['id']}/finish"]
        assert not (tmp_path/'deploy-result.json').exists()
        assert store.get(job['id'])['state'] == ('deployed' if success else 'failed')
        assert store.status()['active_sha'] == ('b'*40 if success else 'a'*40)
        assert store.claim() is not None
        with store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM audit WHERE target=? AND action=?',
                              (job['id'], 'deployed' if success else 'failed')).fetchone()[0] == 1


def test_disk_failure_keeps_known_result_in_memory_before_any_delivery(tmp_path):
    deployer = Deployer.__new__(Deployer)
    deployer.root = tmp_path
    deployer.pending_result = {'job_id': 'a'*12, 'lease': 'b'*32, 'success': True, 'detail': 'Healthy'}
    deployer.api = Mock()
    deployer.api.post.return_value = {'ok': True}
    with patch('gene.deploy.private_json', side_effect=OSError('Disk unavailable')):
        with pytest.raises(OSError):
            deployer.tick()
    deployer.api.post.assert_not_called()
    assert deployer.tick()
    deployer.api.post.assert_called_once_with('/deploy/'+'a'*12+'/finish',
                                             {'lease': 'b'*32, 'success': True, 'detail': 'Healthy'})


def test_invalid_receipt_never_claims_or_guesses_a_result(tmp_path):
    deployer = Deployer.__new__(Deployer)
    deployer.root, deployer.api = tmp_path, Mock()
    (tmp_path/'deploy-result.json').write_text('{"success": true}')
    with pytest.raises(ValueError, match='operator reconciliation'):
        deployer.tick()
    deployer.api.post.assert_not_called()
    assert (tmp_path/'deploy-result.json').exists()
