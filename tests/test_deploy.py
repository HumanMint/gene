from unittest.mock import Mock

import pytest

from gene.deploy import Deployer
from gene.store import Store


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
