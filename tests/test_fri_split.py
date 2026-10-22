"""Independent runtime contract and maintainer release failure recovery."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from gene.deploy import Deployer
from gene.fri_protocol import PROTOCOL, contract
from gene.fri_release import FriRelease
from gene.store import Store
from tests.test_api import headers


def test_contract_requires_fri_identity_and_unknown_version_blocks_before_work(client):
    assert client.get('/fri/contract').status_code == 401
    for identity in ('runtime', 'worker', 'deploy'):
        assert client.get('/fri/contract', headers=headers(identity)).status_code == 401
    assert client.get('/fri/contract', headers=headers('fri')).json() == contract()
    assert client.get('/agent/state', headers={**headers('fri'), 'X-Fri-Contract': 'gene-fri/99'}).status_code == 409
    assert client.get('/agent/state', headers={**headers('fri'), 'X-Fri-Contract': PROTOCOL}).status_code == 200
    assert client.get('/agent/state', headers=headers('fri')).status_code == 200  # Existing companion rollback.


def installation(tmp_path, previous=True):
    root, gene = tmp_path/'fri', tmp_path/'gene'
    root.mkdir()
    (gene/'data/controller').mkdir(parents=True)
    store = Store(gene/'data/controller/gene.sqlite3')
    store.bootstrap('a'*40)
    data, auth, env = tmp_path/'data', tmp_path/'auth', tmp_path/'fri.env'
    data.mkdir()
    auth.mkdir()
    env.write_text('GENE_GUILD_ID=123\n')
    (data/'fri-health.json').write_text(json.dumps({'sha':'a'*40}))
    (data/'reminder-receipts').mkdir()
    (auth/'session.json').write_text('current rotating auth')
    (root/'install.json').write_text(json.dumps({'env_file':str(env), 'data_dir':str(data),
        'auth_dir':str(auth), 'network':'gene_default', 'controller_url':'http://controller:8787',
        'legacy_container':'gene-fri-1'}))
    release = FriRelease(root, gene)
    if previous:
        release.env.write_bytes(release.environment('b'*40))
        release.record.write_text(json.dumps({'sha':'b'*40}))
        with store.connection() as db:
            db.execute('INSERT INTO settings VALUES (?,?)',('fri_active_sha','b'*40))
    calls=[]
    def run(args):
        calls.append(args)
        if args[:3] == ['docker','image','inspect']:
            return 'c'*40
        return ''
    release.run=run
    release.healthy=Mock(return_value=True)
    return release,store,calls


def test_release_is_fri_only_and_records_separate_identity(tmp_path):
    release,store,calls=installation(tmp_path)
    result=release.deploy('c'*40)
    assert result['success'] and Path(result['backup'],'controller.sqlite3').is_file()
    assert store.status()['active_sha'] == 'a'*40
    with store.connection() as db:
        assert db.execute("SELECT value FROM settings WHERE key='fri_active_sha'").fetchone()[0] == 'c'*40
    assert (release.root.parent/'auth/session.json').read_text() == 'current rotating auth'
    assert all('gene-worker' not in command and 'gene-deployer' not in command for command in calls)
    actions=[command for command in calls if command[:2] == ['docker','compose']]
    assert all(command[command.index('--project-name')+1]=='fri' and command[-1]=='runtime' for command in actions)


def test_failure_rolls_code_back_without_resurrecting_erasure_or_replaying_receipts(tmp_path):
    release,store,calls=installation(tmp_path)
    original_env=release.env.read_bytes()
    def health(sha, after, *args):
        if sha == 'c'*40:
            # These happen after the snapshot and must survive code rollback.
            store.erase_user('member')
            (release.data/'reminder-receipts/sent.json').write_text('{"sent":true}')
            (release.root.parent/'auth/session.json').write_text('refreshed auth')
            return False
        return sha == 'b'*40
    release.healthy=health
    with pytest.raises(RuntimeError,match='health'):
        release.deploy('c'*40)
    assert release.env.read_bytes()==original_env
    assert (release.data/'reminder-receipts/sent.json').is_file()
    assert (release.root.parent/'auth/session.json').read_text()=='refreshed auth'
    assert json.loads(release.record.read_text())['sha']=='b'*40
    with store.connection() as db:
        assert db.execute("SELECT value FROM settings WHERE key='fri_active_sha'").fetchone()[0]=='b'*40
        assert db.execute("SELECT revision FROM chat_revisions WHERE user_id='member'").fetchone()[0]>0
    assert store.status()['active_sha']=='a'*40
    assert not any(command[0] in {'systemctl','tar'} for command in calls)


def test_initial_cutover_stops_legacy_before_starting_new_and_failure_restores_it(tmp_path):
    release,_,calls=installation(tmp_path,previous=False)
    release.healthy=Mock(side_effect=[False,True])
    with pytest.raises(RuntimeError,match='health'):
        release.deploy('c'*40)
    stop=calls.index(['docker','stop','gene-fri-1'])
    start=next(i for i,c in enumerate(calls) if 'up' in c)
    assert stop<start
    assert ['docker','update','--restart=no','gene-fri-1'] in calls
    assert ['docker','update','--restart=unless-stopped','gene-fri-1'] in calls
    assert ['docker','start','gene-fri-1'] in calls
    assert not release.env.exists() and not release.record.exists()


def test_wrong_image_or_partial_state_never_stops_live_bot(tmp_path):
    release,_,calls=installation(tmp_path)
    with pytest.raises(ValueError,match='match'):
        release.deploy('d'*40)
    assert len(calls)==1
    release.env.unlink()
    with pytest.raises(ValueError,match='Inconsistent'):
        release.deploy('c'*40)
    assert len(calls)==2


@pytest.mark.parametrize('interrupted_state', ['new_env', 'changed_mount', 'new_audit'])
def test_interrupted_release_or_config_drift_cannot_become_rollback_baseline(tmp_path, interrupted_state):
    release,store,calls=installation(tmp_path)
    if interrupted_state == 'new_env':
        release.env.write_bytes(release.environment('d'*40))
    elif interrupted_state == 'changed_mount':
        release.env.write_bytes(release.env.read_bytes().replace(b'gene_default', b'other_network'))
    else:
        with store.connection() as db:
            db.execute("UPDATE settings SET value=? WHERE key='fri_active_sha'", ('d'*40,))
    original_env=release.env.read_bytes()
    with pytest.raises(ValueError,match='Inconsistent'):
        release.deploy('c'*40)
    assert len(calls)==1 and calls[0][:3]==['docker','image','inspect']
    assert release.env.read_bytes()==original_env
    assert json.loads(release.record.read_text())['sha']=='b'*40
    release.healthy.assert_not_called()


def test_gene_cog_activation_cannot_change_fri_pin_or_restart_fri(tmp_path):
    release,_,_=installation(tmp_path)
    before=release.env.read_bytes()
    deployer=Deployer.__new__(Deployer)
    deployer.root=release.gene
    (deployer.root/'release.env').write_text('GENE_RUNTIME_IMAGE=gene-runtime:old\n')
    deployer.compose=Mock()
    deployer.activate('d'*40)
    deployer.compose.assert_called_once_with('up','-d','--no-deps','--force-recreate','red')
    assert release.env.read_bytes()==before
