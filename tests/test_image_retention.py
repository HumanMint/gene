import copy
import json
import sqlite3
import subprocess
from unittest.mock import Mock

import pytest

from scripts import gene_image_retention as retention

NOW = 200000.0
PAST = NOW - retention.GRACE - 1


def iid(number):
    return 'sha256:' + f'{number:064x}'


def tag(number, role='runtime'):
    return f'gene-{role}:' + f'{number:040x}'


def evidence(tags, at=PAST):
    return {'tags': tags, 'retired_at': at, 'evidence': 'Verified superseded commit'}


def view(images=None, protected=(), current=()):
    return {'images': images or {iid(1): {'tags': [tag(1)]}},
            'protected': set(protected), 'current': set(current),
            'current_tags': {key: [tag(1)] for key in current}, 'fingerprint': 'unchanged'}


@pytest.mark.parametrize('protected', [False, True])
def test_only_evidenced_past_versions_are_removed(protected):
    inventory = view(protected=[iid(1)] if protected else [])
    state = {'retired': {iid(1): evidence([tag(1)])}}
    result = retention.plan(inventory, state, NOW)
    assert bool(result['delete']) is not protected
    assert retention.plan(inventory, {'retired': {}}, NOW)['delete'] == []


@pytest.mark.parametrize('tags', [[], ['gene-runtime:bootstrap'], ['gene-worker:search-tool'],
                                ['gene-audio:' + 'a'*40], ['wordpress:latest'],
                                [tag(1), 'other-app:latest'], [tag(1), tag(2)]])
def test_unknown_functional_aliases_untagged_and_other_apps_stay(tags):
    inventory = view({iid(1): {'tags': tags}})
    assert retention.plan(inventory, {'retired': {iid(1): evidence([tag(1)])}}, NOW)['delete'] == []


def test_supersession_grace_and_reactivation_restart_the_clock():
    state = {'retired': {}, 'seen_current': {iid(1): [tag(1)]}, 'active_ids': [iid(1)]}
    retention.observe(state, view(current=[]), NOW)
    assert not retention.plan(view(), state, NOW)['delete']
    assert retention.plan(view(), state, NOW + retention.GRACE)['delete']
    retention.observe(state, view(current=[iid(1)]), NOW + 100)
    assert iid(1) not in state['retired']
    retention.observe(state, view(), NOW + 200)
    assert state['retired'][iid(1)]['retired_at'] == NOW + 200
    assert not retention.plan(view(), state, NOW + retention.GRACE)['delete']


def test_historical_reactivation_between_runs_renews_grace():
    inventory = view()
    inventory['past'] = {iid(1): evidence([tag(1)], NOW - 10)}
    state = {'retired': {iid(1): evidence([tag(1)])}}
    retention.observe(state, inventory, NOW)
    assert not retention.plan(inventory, state, NOW)['delete']


def test_successful_backup_history_catches_versions_between_timer_runs():
    inventory = view()
    inventory['past'] = {iid(1): evidence([tag(1)])}
    state = retention.observe({'retired': {}}, inventory, NOW)
    assert retention.plan(inventory, state, NOW)['delete']
    # A new alias that was not historically configured still prevents deletion.
    inventory['images'][iid(1)]['tags'].append(tag(2))
    assert not retention.plan(inventory, state, NOW)['delete']


@pytest.fixture
def host(tmp_path, monkeypatch):
    for p in ['maintenance', 'secrets', 'data/controller', 'releases', 'backups']:
        (tmp_path / p).mkdir(parents=True, exist_ok=True)
    shas = [f'{n:040x}' for n in (1, 2, 3)]
    (tmp_path / 'release.env').write_text(f'GENE_RELEASE_SHA={shas[0]}\nGENE_RUNTIME_IMAGE={tag(1)}\n')
    (tmp_path / 'secrets/worker.env').write_text(f'GENE_WORKER_IMAGE={tag(1, "worker")}\nSECRET=private\n')
    (tmp_path / 'audio-release.env').write_text('GENE_AUDIO_SHA=' + shas[0] + '\n')
    images = {}
    for n in (1, 2, 3):
        for role, offset in [('runtime', 0), ('worker', 10)]:
            images[iid(n+offset)] = {'tags': [tag(n, role)]}
    images[iid(21)] = {'tags': ['gene-audio:' + shas[0]]}
    images[iid(22)] = {'tags': ['gene-audio-resolver:' + shas[0]]}
    images[iid(4)] = {'tags': [tag(4)]}
    images[iid(5)] = {'tags': [tag(5)]}
    images[iid(99)] = {'tags': ['wordpress:latest']}
    for current, previous in [(1, 2), (2, 3)]:
        folder = tmp_path / 'backups' / ('maintainer-' + f'{current:040x}')
        folder.mkdir()
        (folder / 'release.env').write_text(f'GENE_RELEASE_SHA={previous:040x}\nGENE_RUNTIME_IMAGE={tag(previous)}\n')
        (folder / 'worker.env').write_text(f'GENE_WORKER_IMAGE={tag(previous, "worker")}\n')
        (folder / 'installed-source').mkdir()
        for name in ('controller.sqlite3', 'red.tar.gz', 'fri.tar.gz'):
            (folder / name).touch()
        (tmp_path / 'releases' / f'{previous:040x}' / 'cogs').mkdir(parents=True)
    db = sqlite3.connect(tmp_path / 'data/controller/gene.sqlite3')
    db.executescript('CREATE TABLE settings (key,value); CREATE TABLE jobs(id,kind,state,proposal,approved_sha);'
                     'CREATE TABLE deployments(job_id,sha,previous_sha,at,state);')
    db.executemany('INSERT INTO settings VALUES (?,?)', [('active_sha', shas[0]), ('previous_sha', shas[1])])
    db.executemany('INSERT INTO deployments VALUES (?,?,?,?,?)', [
        ('first', shas[0], shas[1], 200, 'deployed'), ('second', shas[1], shas[2], 100, 'deployed')])
    db.execute('INSERT INTO jobs(state,proposal,approved_sha) VALUES (?,?,?)',
               ('review', json.dumps({'sha': f'{4:040x}'}), None))
    db.commit()
    policy = {'version': 1, 'keep_images': [], 'retired_images': {
        k: evidence(v['tags']) for k, v in images.items() if k not in [iid(21), iid(22), iid(99)]}}
    retention.save_json(tmp_path / 'maintenance/image-retention-policy.json', policy)
    monkeypatch.setattr(retention, 'docker_inventory', lambda: (copy.deepcopy(images), {}))
    monkeypatch.setattr(retention, 'process_refs', lambda: [])
    monkeypatch.setattr(retention, 'image_metadata', lambda ref: {'id': ref, 'parent': '', 'tags': []})
    monkeypatch.setattr(retention.time, 'time', lambda: NOW)
    return tmp_path, images, policy, db


def test_current_inactive_worker_two_rollbacks_and_pending_are_protected(host):
    root, images, policy, _ = host
    snapshot = retention.snapshot(root, policy)
    assert {iid(n) for n in [1, 11, 2, 12, 3, 13, 4, 21, 22]} <= snapshot['protected']
    result = retention.run(root)
    assert [r['id'] for r in result['delete']] == [iid(5)]
    assert not (root / 'maintenance/image-retention-state.json').exists()
    assert images[iid(99)]['tags'] == ['wordpress:latest']


@pytest.mark.parametrize('change', ['missing_image', 'missing_backup', 'mismatched_release',
                                   'inflight', 'receipt', 'invalid_policy'])
def test_incomplete_or_changing_release_state_fails_closed(host, change):
    root, images, _, db = host
    if change == 'missing_image':
        del images[iid(13)]
    elif change == 'missing_backup':
        (root / 'backups' / ('maintainer-' + f'{2:040x}') / 'worker.env').unlink()
    elif change == 'mismatched_release':
        (root / 'release.env').write_text('GENE_RELEASE_SHA=' + 'a'*40 + '\n')
    elif change == 'inflight':
        db.execute("INSERT INTO jobs(state,proposal,approved_sha) VALUES ('building',NULL,NULL)")
        db.commit()
    elif change == 'receipt':
        (root / 'deploy-result.json').write_text('{}')
    else:
        (root / 'maintenance/image-retention-policy.json').write_text('{}')
    with pytest.raises((ValueError, KeyError)):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-result.json').exists()


def test_process_configured_and_explicitly_held_images_stay(host, monkeypatch):
    root, _, policy, _ = host
    monkeypatch.setattr(retention, 'process_refs', lambda: [tag(5)])
    assert iid(5) in retention.snapshot(root, policy)['protected']
    monkeypatch.setattr(retention, 'process_refs', lambda: [])
    policy['keep_images'] = [iid(5)]
    assert iid(5) in retention.snapshot(root, policy)['protected']


def test_no_force_no_prune_and_other_apps_are_never_targeted(host, monkeypatch):
    root, images, _, _ = host
    calls = []

    def docker(*args):
        calls.append(args)
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'Id': iid(5)}])
        assert args == ('docker', 'image', 'rm', '--no-prune', tag(5))
        del images[iid(5)]
        return ''

    monkeypatch.setattr(retention, 'command', docker)
    result = retention.run(root, apply=True)
    assert result['removed_tags'] == [tag(5)]
    assert iid(99) in images
    assert len(calls) == 2
    # Crash/restart or a repeated hourly run does not repeat completed removals.
    assert retention.run(root, apply=True)['removed_tags'] == []


@pytest.mark.parametrize('mutation', ['configuration', 'retag'])
def test_revalidate_before_removing_anything(host, monkeypatch, mutation):
    root, _, _, _ = host
    original = retention.snapshot
    count = 0

    def snapshot(*args):
        nonlocal count
        result = original(*args)
        count += 1
        if count > 1 and mutation == 'configuration':
            result['fingerprint'] = 'changed'
        return result

    monkeypatch.setattr(retention, 'snapshot', snapshot)
    command = Mock(return_value=json.dumps([{'Id': iid(123)}]))
    monkeypatch.setattr(retention, 'command', command)
    with pytest.raises(ValueError, match='changed|retagged'):
        retention.run(root, apply=True)
    assert not any(c.args[:3] == ('docker', 'image', 'rm') for c in command.call_args_list)


def test_failed_delete_keeps_retry_evidence_and_does_not_force(host, monkeypatch):
    root, _, _, _ = host
    command = Mock(side_effect=[json.dumps([{'Id': iid(5)}]), RuntimeError('in use')])
    monkeypatch.setattr(retention, 'command', command)
    with pytest.raises(RuntimeError, match='in use'):
        retention.run(root, apply=True)
    state = retention.read_json(root / 'maintenance/image-retention-state.json')
    assert iid(5) in state['retired']
    assert '--force' not in command.call_args.args


def test_normal_cog_release_keeps_image_retention_working(host):
    root, _, policy, db = host
    sha, previous = f'{6:040x}', f'{1:040x}'
    (root / 'release.env').write_text(f'GENE_RELEASE_SHA={sha}\nGENE_RUNTIME_IMAGE={tag(1)}\n')
    (root / 'releases' / previous / 'cogs').mkdir(parents=True)
    backup = root / 'backups/cog-job'
    backup.mkdir()
    (backup / 'controller.sqlite3').touch()
    (backup / 'red.tar.gz').touch()
    db.execute("UPDATE settings SET value=? WHERE key='active_sha'", (sha,))
    db.execute("UPDATE settings SET value=? WHERE key='previous_sha'", (previous,))
    db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
               ('cog-job', 'feature', 'deployed', json.dumps({'sha': sha, 'base_sha': previous}), sha))
    db.execute('INSERT INTO deployments VALUES (?,?,?,?,?)',
               ('cog-job', sha, previous, 300, 'deployed'))
    db.commit()
    inventory = retention.snapshot(root, policy)
    assert {iid(n) for n in (1, 11, 2, 12, 3, 13)} <= inventory['protected']


def test_cog_rollback_to_a_maintainer_sha_uses_its_own_event_backup(host):
    root, _, policy, db = host
    test_normal_cog_release_keeps_image_retention_working(host)
    sha, previous = f'{1:040x}', f'{6:040x}'
    (root / 'release.env').write_text(f'GENE_RELEASE_SHA={sha}\nGENE_RUNTIME_IMAGE={tag(1)}\n')
    (root / 'releases' / previous / 'cogs').mkdir(parents=True)
    backup = root / 'backups/rollback-job'
    backup.mkdir()
    (backup / 'controller.sqlite3').touch()
    (backup / 'red.tar.gz').touch()
    db.execute("UPDATE settings SET value=? WHERE key='active_sha'", (sha,))
    db.execute("UPDATE settings SET value=? WHERE key='previous_sha'", (previous,))
    db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
               ('rollback-job', 'rollback', 'deployed', json.dumps({'sha': sha, 'base_sha': previous}), sha))
    db.execute('INSERT INTO deployments VALUES (?,?,?,?,?)',
               ('rollback-job', sha, previous, 400, 'deployed'))
    db.commit()
    inventory = retention.snapshot(root, policy)
    assert {iid(n) for n in (1, 11, 2, 12, 3, 13)} <= inventory['protected']


@pytest.mark.parametrize('hold', [False, True])
def test_auxiliary_on_demand_or_hold_reactivation_renews_grace(host, hold):
    root, _, policy, _ = host
    path = root / 'secrets/ondemand.env'
    if hold:
        policy['keep_images'] = [tag(5)]
        retention.save_json(root / 'maintenance/image-retention-policy.json', policy)
    else:
        path.write_text('GENE_TOOL_IMAGE=' + tag(5) + '\n')
    assert retention.run(root, apply=True)['delete'] == []
    if hold:
        policy['keep_images'] = []
        retention.save_json(root / 'maintenance/image-retention-policy.json', policy)
    else:
        path.unlink()
    assert retention.run(root)['delete'] == []


def test_subsequent_explicit_retirement_policy_is_reconciled(host):
    root, _, policy, _ = host
    entry = policy['retired_images'].pop(iid(5))
    retention.save_json(root / 'maintenance/image-retention-policy.json', policy)
    assert retention.run(root, apply=True)['delete'] == []
    policy['retired_images'][iid(5)] = entry
    retention.save_json(root / 'maintenance/image-retention-policy.json', policy)
    assert [row['id'] for row in retention.run(root)['delete']] == [iid(5)]


@pytest.mark.parametrize('protection', ['none', 'configured', 'tagged', 'shared_child'])
def test_only_unnamed_retired_gene_ancestors_can_be_collected(host, monkeypatch, protection):
    root, images, _, _ = host
    images[iid(50)] = {'tags': []}
    metadata = {iid(5): {'id': iid(5), 'parent': iid(50), 'tags': [tag(5)]},
                iid(50): {'id': iid(50), 'parent': iid(99), 'tags': []},
                iid(99): {'id': iid(99), 'parent': '', 'tags': ['wordpress:latest']}}
    monkeypatch.setattr(retention, 'image_metadata', lambda ref: metadata[ref])
    if protection == 'configured':
        (root / 'secrets/ondemand.env').write_text('GENE_TOOL_IMAGE=' + iid(50) + '\n')
    calls = []

    def docker(*args):
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'Id': iid(5)}])
        calls.append(args)
        target = args[-1]
        if target == tag(5):
            del images[iid(5)]
            if protection == 'tagged':
                metadata[iid(50)]['tags'] = ['gene-worker:future-function']
        elif protection == 'shared_child':
            raise subprocess.CalledProcessError(1, args)
        else:
            assert target == iid(50)
            del images[iid(50)]
        return ''

    monkeypatch.setattr(retention, 'command', docker)
    result = retention.run(root, apply=True)
    assert result['removed_tags'] == [tag(5)]
    assert (iid(50) not in images) == (protection == 'none')
    assert iid(99) in images
    assert not any(c[-1] == iid(99) for c in calls)
    assert all(c[:4] == ('docker', 'image', 'rm', '--no-prune') for c in calls)


def test_dry_run_persists_a_reviewable_plan_without_mutating_retirement_state(host):
    root, _, _, _ = host
    retention.run(root)
    plan = retention.read_json(root / 'maintenance/image-retention-plan.json')
    assert plan['delete'][0]['tags'] == [tag(5)]
    assert plan['delete'][0]['evidence']
    assert not (root / 'maintenance/image-retention-state.json').exists()
