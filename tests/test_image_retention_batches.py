"""Image retention against batch-release.sh's records (gene #76): synthetic paths, no Docker."""
import copy
import json
import os
import shutil
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import gene_image_retention as retention
from tests.test_image_retention import NOW, iid

REAL_CLOCK = retention.time.time  # bound before any fixture pins time.time; the script runs on the real clock

DAY = 24 * 60 * 60
RUNTIME, WORKER, FRI = 100, 110, 120  # image IDs: iid(base + release number)
AUDIO = 'a' * 40
LIVE = ['baseline', 'staged', 'rehearsed', 'intent:pause', 'drained', 'gene_attempted', 'intent:cogs',
        'intent:red_stop', 'intent:source_swap', 'intent:env', 'intent:worker', 'intent:start_new',
        'intent:record', 'gene_recorded', 'intent:fri', 'fri_switched', 'evals_started', 'evals_ran',
        'evaluated', 'verified_off', 'intent:services', 'done:unpause', 'resumed']


def gene(n):
    return f'{0xa00 + n:040x}'


def fri(n):
    return f'{0xf00 + n:040x}'


def release_images(n):
    return {'runtime': ('gene-runtime:' + gene(n), iid(RUNTIME + n)),
            'worker': ('gene-worker:' + gene(n), iid(WORKER + n)),
            'fri': ('fri-runtime:' + fri(n), iid(FRI + n))}


def chain(layers, size, parent=''):
    return {'parent': parent, 'size': size, 'layers': ['sha256:' + layer for layer in layers]}


def write_batch(root, new, old, at, marks=LIVE, legacy_format=False, backup=True):
    """A batch the way batch-release.sh leaves it: manifest, journal, switch backup and old cogs."""
    stage = root / 'staging' / ('batch-' + gene(new) + ('' if legacy_format else '-' + fri(new)))
    stage.mkdir(parents=True)
    manifest = {'gene': gene(new), 'fri': fri(new),
                'old': {role: {'tag': t, 'id': i} for role, (t, i) in release_images(old).items()},
                'new': {role: {'tag': t, 'id': i} for role, (t, i) in release_images(new).items()},
                'archives': {'gene': 'b' * 64, 'fri': 'c' * 64}, 'cogs': 'd' * 64, 'downgrades': ''}
    if not legacy_format:  # batch-release.sh before 1ad1493 recorded no old pair
        manifest |= {'old_gene': gene(old), 'old_fri': fri(old)}
    if backup:
        folder = root / 'backups' / f'maintainer-batch-{gene(new)}-{int(at)}'
        (folder / 'db').mkdir(parents=True)
        (folder / 'installed-source').mkdir()
        (folder / 'release.env').write_text(f'GENE_RELEASE_SHA={gene(old)}\nGENE_ROOT={root}\n'
                                            f'GENE_RUNTIME_IMAGE=gene-runtime:{gene(old)}\n')
        (folder / 'worker.env').write_text(f'GENE_WORKER_IMAGE=gene-worker:{gene(old)}\nGENE_WORKER_TOKEN=tok-3f9a-secret\n')
        (folder / 'fri-release.env').write_text(f'FRI_RELEASE_SHA={fri(old)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(old)}\n')
        (folder / 'fri-release.json').write_text(json.dumps({'sha': fri(old), 'previous': fri(old - 1)}))
        for name in ('db/controller.sqlite3', 'red.tar.gz'):
            (folder / name).touch()
        # The old source tree, marked with its release, with the compose files a rollback starts it from.
        (folder / 'installed-source/ops').mkdir()
        (folder / 'installed-source/.release-sha').write_text(gene(old) + '\n')
        for name in ('compose.yml', 'fri-compose.yml'):
            (folder / 'installed-source/ops' / name).write_text('services: {}\n')
        manifest['backup'] = str(folder)
    (root / 'releases' / gene(old) / 'cogs').mkdir(parents=True, exist_ok=True)
    (stage / 'manifest.json').write_text(json.dumps(manifest, indent=1))
    # baseline.json is batch-release.sh's state snapshot before the batch: what rollback and resume-old check.
    (stage / 'baseline.json').write_text(json.dumps({
        'settings': {'active_sha': gene(old), 'previous_sha': gene(old - 1), 'paused': 'false',
                     'fri_active_sha': fri(old)},
        'source': gene(old), 'busy_jobs': 0,
        'gene': {'sha': gene(old), 'guild_id': '123', 'presence': 'online', 'guild_connected': True},
        'fri': {'sha': fri(old), 'guild_id': '123', 'presence': 'online', 'guild_connected': True}}))
    journal = stage / 'journal'
    journal.write_text(''.join(mark + '\n' for mark in marks))
    os.utime(journal, (at, at))
    return stage


@pytest.fixture
def batch_host(tmp_path, monkeypatch):
    """Live Gene 4 / Fri 4 after four batches (1 and 2 in the old format) and no deployments row."""
    root, fri_root = tmp_path / 'gene', tmp_path / 'fri'
    for path in ('maintenance', 'secrets', 'data/controller', 'releases', 'backups', 'staging'):
        (root / path).mkdir(parents=True)
    fri_root.mkdir()
    (root / 'release.env').write_text(f'GENE_RELEASE_SHA={gene(4)}\nGENE_ROOT={root}\n'
                                      f'GENE_RUNTIME_IMAGE=gene-runtime:{gene(4)}\n')
    (root / 'source').mkdir()
    (root / 'source/.release-sha').write_text(gene(4) + '\n')
    (root / 'secrets/worker.env').write_text(f'GENE_WORKER_IMAGE=gene-worker:{gene(4)}\nGENE_WORKER_TOKEN=tok-3f9a-secret\n')
    (root / 'audio-release.env').write_text(f'GENE_AUDIO_SHA={AUDIO}\n')
    (fri_root / 'release.env').write_text(f'FRI_RELEASE_SHA={fri(4)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(4)}\n')
    (fri_root / 'release.json').write_text(json.dumps({'sha': fri(4), 'previous': fri(3), 'at': 1}))
    images = {}
    for n in range(5):
        for tag, image in release_images(n).values():
            images[image] = {'tags': [tag]}
    images[iid(201)] = {'tags': ['gene-audio:' + AUDIO]}
    images[iid(202)] = {'tags': ['gene-audio-resolver:' + AUDIO]}
    images[iid(203)] = {'tags': ['wordpress:latest']}
    images[iid(300)] = {'tags': []}  # an unnamed object nothing recorded
    images[iid(204)] = {'tags': ['python:3.11-slim']}  # the base every Gene image is built on
    for n, old in [(1, 0), (2, 1), (3, 2), (4, 3)]:
        write_batch(root, n, old, NOW - (5 - n) * DAY, legacy_format=n < 3)
    db = sqlite3.connect(root / 'data/controller/gene.sqlite3')
    db.executescript('CREATE TABLE settings (key,value); CREATE TABLE jobs(id,kind,state,proposal,approved_sha);'
                     'CREATE TABLE deployments(job_id,sha,previous_sha,at,state);')
    db.executemany('INSERT INTO settings VALUES (?,?)', [('active_sha', gene(4)), ('previous_sha', gene(3)),
                                                         ('fri_active_sha', fri(4))])
    db.commit()
    policy = {'version': 1, 'keep_images': [], 'retired_images': {}, 'apply_enabled': True}
    retention.save_json(root / 'maintenance/image-retention-policy.json', policy)
    containers = {'controller': iid(RUNTIME + 4), 'red': iid(RUNTIME + 4), 'fri': iid(FRI + 4)}
    # Classic builder: each worker is a child of its runtime image.
    metadata = {image: {'id': image, 'parent': '', 'tags': row['tags']} for image, row in images.items()}
    for n in range(5):
        metadata[iid(RUNTIME + n)]['parent'] = iid(204)
        metadata[iid(WORKER + n)]['parent'] = iid(RUNTIME + n)
    monkeypatch.setattr(retention, 'docker_inventory', lambda: (copy.deepcopy(images), dict(containers)))
    monkeypatch.setattr(retention, 'process_refs', lambda: [])
    monkeypatch.setattr(retention, 'image_metadata', lambda ref: copy.deepcopy(metadata[ref]))
    monkeypatch.setattr(retention, 'live_images', lambda: {key: containers[key] for key in ('controller', 'red', 'fri')},
                        raising=False)
    # Layer chains as the classic builder leaves them: one base under every Gene image, a runtime layer,
    # then the worker's layer on top of its runtime.
    details = {iid(201): chain(['audio'], 900), iid(202): chain(['resolver'], 800),
               iid(203): chain(['wordpress'], 1000), iid(300): chain(['orphan'], 70),
               iid(204): chain(['base'], 500)}
    for n in range(5):
        details[iid(RUNTIME + n)] = chain(['base', f'runtime-{n}'], 600, iid(204))
        details[iid(WORKER + n)] = chain(['base', f'runtime-{n}', f'worker-{n}'], 650, iid(RUNTIME + n))
        details[iid(FRI + n)] = chain(['fri-base', f'fri-{n}'], 350)
    monkeypatch.setattr(retention, 'image_details', lambda ids: {i: copy.deepcopy(details[i]) for i in ids}, raising=False)
    monkeypatch.setattr(retention, 'FRI_ROOT', fri_root, raising=False)
    monkeypatch.setattr(retention.time, 'time', lambda: NOW)
    return SimpleNamespace(root=root, fri_root=fri_root, images=images, policy=policy, db=db,
                           containers=containers, metadata=metadata, details=details)


def legacy_level(root, db, sha, previous, at, complete):
    """A controller deployments row with a maintainer backup, as installs before batch-release.sh left them."""
    folder = root / 'backups' / ('maintainer-' + sha)
    folder.mkdir()
    (folder / 'release.env').write_text(f'GENE_RELEASE_SHA={previous}\nGENE_RUNTIME_IMAGE=gene-runtime:{previous}\n')
    (folder / 'worker.env').write_text(f'GENE_WORKER_IMAGE=gene-worker:{previous}\n')
    (folder / 'controller.sqlite3').touch()
    (folder / 'red.tar.gz').touch()
    if complete:
        (folder / 'installed-source').mkdir()
        (folder / 'fri.tar.gz').touch()
    (root / 'releases' / previous / 'cogs').mkdir(parents=True, exist_ok=True)
    db.execute('INSERT INTO deployments VALUES (?,?,?,?,?)', ('legacy-' + sha[-4:], sha, previous, at, 'deployed'))
    db.commit()


def test_batch_release_history_without_a_deployments_row_yields_a_safe_plan(batch_host):
    root = batch_host.root
    result = retention.run(root)
    # Release 0 is behind the two rollback levels and the three batches cleanup keeps.
    assert [row['tags'] for row in result['delete']] == [['gene-worker:' + gene(0)], ['gene-runtime:' + gene(0)]]
    assert all(row['evidence'] for row in result['delete'])
    for n in (1, 2, 3, 4):
        for _, image in release_images(n).values():
            assert image in result['keep']
    assert iid(FRI + 0) in result['keep']  # Fri release tags belong to batch cleanup.
    for image in (iid(201), iid(202), iid(203), iid(300)):
        assert image in result['keep']
    assert retention.read_json(root / 'maintenance/image-retention-plan.json')['delete'] == result['delete']


@pytest.mark.parametrize('covered', [True, False])
def test_an_incomplete_legacy_level_blocks_only_inside_the_rollback_depth(batch_host, covered):
    root, db = batch_host.root, batch_host.db
    if covered:  # The host's history: batches above the last legacy install, whose backup is incomplete.
        legacy_level(root, db, gene(0), gene(8), 100.0, complete=False)
        assert [row['id'] for row in retention.run(root)['delete']] == [iid(WORKER + 0), iid(RUNTIME + 0)]
        return
    for n in (1, 2, 3):
        for path in (root / 'staging').glob('batch-' + gene(n) + '*'):
            (path / 'journal').unlink()
    legacy_level(root, db, gene(3), gene(2), 100.0, complete=False)
    with pytest.raises(ValueError, match=f'maintainer-{gene(3)} for {gene(3)[:7]} is incomplete: '
                                         'missing installed-source, fri.tar.gz'):
        retention.run(root)
    assert not (root / 'maintenance/image-retention-plan.json').exists()


def stage_of(root, n):
    """Batch n's own record, whichever format; never a Fri-only batch on the same Gene."""
    return next(path for path in (root / 'staging' / f'batch-{gene(n)}-{fri(n)}', root / 'staging' / f'batch-{gene(n)}')
                if path.exists())


def set_marks(stage, marks):
    at = (stage / 'journal').stat().st_mtime
    (stage / 'journal').write_text(''.join(mark + '\n' for mark in marks))
    os.utime(stage / 'journal', (at, at))


def edit_manifest(stage, change):
    path = stage / 'manifest.json'
    manifest = json.loads(path.read_text())
    change(manifest)
    path.write_text(json.dumps(manifest))


def backup_of(stage):
    return retention.Path(json.loads((stage / 'manifest.json').read_text())['backup'])


def test_the_plan_lists_exact_removals_reasons_for_every_kept_image_and_a_byte_estimate(batch_host):
    root = batch_host.root
    result = retention.run(root)
    plan = retention.read_json(root / 'maintenance/image-retention-plan.json')
    assert plan == json.loads(json.dumps(result))
    assert [(row['id'], row['tags']) for row in plan['delete']] == [
        (iid(WORKER + 0), ['gene-worker:' + gene(0)]), (iid(RUNTIME + 0), ['gene-runtime:' + gene(0)])]
    assert plan['delete'][0]['evidence'] == f'Superseded in batch release history ({stage_of(root, 1).name})'
    assert plan['delete_parents'] == []
    # Every image object Docker lists is either planned for removal or kept with a reason.
    assert set(plan['keep']) | {row['id'] for row in plan['delete']} == set(batch_host.images)
    assert all(row['reason'] for row in plan['keep'].values())
    reasons = {image: row['reason'] for image, row in plan['keep'].items()}
    assert reasons[iid(RUNTIME + 4)].startswith(retention.CONTAINER + '; ' + retention.CURRENT)
    assert reasons[iid(WORKER + 4)].startswith(retention.CURRENT)
    assert reasons[iid(RUNTIME + 3)].startswith(f'{retention.ROLLBACK} (batch {stage_of(root, 4).name})')
    assert reasons[iid(WORKER + 2)].startswith(f'{retention.ROLLBACK} (batch {stage_of(root, 3).name})')
    assert reasons[iid(WORKER + 1)] == f'{retention.RECENT} ({stage_of(root, 2).name})'
    assert reasons[iid(FRI + 2)].startswith(retention.ROLLBACK) and reasons[iid(FRI + 1)].startswith(retention.RECENT)
    assert reasons[iid(FRI + 0)] == retention.FRI_OWNED
    assert reasons[iid(FRI + 4)].startswith(retention.CONTAINER)
    assert reasons[iid(201)].startswith(retention.AUDIO) and reasons[iid(203)] == retention.OUTSIDE
    assert reasons[iid(300)] == retention.UNCLASSIFIED
    assert plan['keep_summary'][retention.FRI_OWNED] == 1 and sum(plan['keep_summary'].values()) == len(plan['keep'])
    # The worker's and runtime's own layers; the shared base stays with the kept images.
    assert plan['bytes']['lower_bytes'] == plan['bytes']['upper_bytes'] == 150 and plan['bytes']['exact']
    # With no image ending at the shared base, its size is bounded only by the runtimes above it (600).
    del batch_host.details[iid(204)]
    batch_host.images.pop(iid(204))
    bounds = retention.run(root)['bytes']
    assert (bounds['lower_bytes'], bounds['upper_bytes'], bounds['exact']) == (50, 650, False)
    assert plan['live'] == {'gene': gene(4), 'fri': fri(4)}
    assert [(step['sha'], step['previous'], step['previous_fri']) for step in plan['lineage']] == [
        (gene(4), gene(3), fri(3)), (gene(3), gene(2), fri(2))]
    assert plan['deletion'] == 'plan only' and plan['version'] == 2
    assert 'tok-3f9a-secret' not in (root / 'maintenance/image-retention-plan.json').read_text()
    assert not (root / 'maintenance/image-retention-state.json').exists()


def test_apply_removes_only_the_planned_tags_with_no_force_or_prune(batch_host, monkeypatch):
    root, images = batch_host.root, batch_host.images
    calls = []

    def docker(*args):
        calls.append(args)
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'Id': next(i for i, row in images.items() if args[3] in row['tags'])}])
        assert args[:4] == ('docker', 'image', 'rm', '--no-prune')
        images.pop(next(i for i, row in images.items() if args[4] in row['tags']))
        return ''

    monkeypatch.setattr(retention, 'command', docker)
    result = retention.run(root, apply=True)
    assert result['removed_tags'] == ['gene-worker:' + gene(0), 'gene-runtime:' + gene(0)]
    assert [c for c in calls if c[2] == 'rm'] == [('docker', 'image', 'rm', '--no-prune', 'gene-worker:' + gene(0)),
                                                 ('docker', 'image', 'rm', '--no-prune', 'gene-runtime:' + gene(0))]
    assert iid(FRI + 0) in images and result['deletion'] == 'enabled'


def test_apply_waits_for_the_policy_to_enable_deletion(batch_host, monkeypatch):
    root, policy = batch_host.root, batch_host.policy
    policy['apply_enabled'] = False
    retention.save_json(root / 'maintenance/image-retention-policy.json', policy)
    monkeypatch.setattr(retention, 'command', lambda *args: pytest.fail(f'unexpected {args}'))
    result = retention.run(root, apply=True)
    assert result['deletion'] == 'disabled in the policy' and result['removed_tags'] == []
    assert len(result['delete']) == 2
    assert not (root / 'maintenance/image-retention-state.json').exists()
    policy['apply_enabled'] = 'yes'
    retention.save_json(root / 'maintenance/image-retention-policy.json', policy)
    with pytest.raises(ValueError, match='Unsupported retention policy'):
        retention.run(root, apply=True)


def test_removals_stop_when_the_window_closes(batch_host, monkeypatch):
    root, images = batch_host.root, batch_host.images
    clock = iter([NOW] * 6 + [NOW + 3600] * 100)  # the plan and first removal at 04:10, then 05:10
    monkeypatch.setattr(retention.time, 'time', lambda: next(clock))

    def docker(*args):
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'Id': next(i for i, row in images.items() if args[3] in row['tags'])}])
        images.pop(next(i for i, row in images.items() if args[4] in row['tags']))
        return ''

    monkeypatch.setattr(retention, 'command', docker)
    result = retention.run(root, apply=True)
    assert result['removed_tags'] == ['gene-worker:' + gene(0)]
    assert 'window closed' in result['deferred']
    assert iid(RUNTIME + 0) in images


def remove_journal_marks(root, n, *marks):
    stage = stage_of(root, n)
    set_marks(stage, [m for m in (stage / 'journal').read_text().split() if m not in marks])


@pytest.mark.parametrize('mutation, message', [
    ('rolled_back', 'Missing successful rollback lineage for ' + gene(4)),
    ('unrecorded', 'Missing successful rollback lineage for ' + gene(4)),
    ('different_release', 'Malformed batch record .*different release'),
    ('unreadable_manifest', 'Malformed batch record .*unreadable manifest'),
    ('symlinked_manifest', 'unreadable manifest'),
    ('invalid_image', 'Malformed batch record .*invalid old runtime image'),
    ('missing_backup_file', 'is missing db/controller.sqlite3'),
    ('missing_source', 'is missing installed-source'),
    ('backup_outside', 'switch backup is missing or outside backups/'),
    ('backup_disagrees', r'its rollback-gene would refuse \(worker.env\)'),
    ('old_image_changed', 'old runtime image gene-runtime:.* is missing or changed'),
    ('new_fri_missing', 'new fri image fri-runtime:.* is missing or changed'),
    ('missing_cogs', 'rollback cogs releases/' + gene(2) + ' are missing'),
    ('pointer', 'Controller rollback pointer disagrees with batch'),
    ('live_pins', 'Live Gene image pins disagree with batch'),
    ('live_fri', 'Live Fri is not the release batch'),
    ('ambiguous', 'Ambiguous rollback lineage for ' + gene(4)),
    ('unknown_dir', 'Unrecognized batch record'),
])
def test_malformed_unfinished_rolled_back_or_ambiguous_history_refuses(batch_host, mutation, message):
    root, images, db = batch_host.root, batch_host.images, batch_host.db
    live, behind = stage_of(root, 4), stage_of(root, 3)
    if mutation == 'rolled_back':
        set_marks(live, LIVE + ['intent:rollback', 'fri_restored', 'done:downgrade', 'gene_rolled_back',
                                'intent:services_old', 'resumed_old'])
    elif mutation == 'unrecorded':
        remove_journal_marks(root, 4, 'gene_recorded')
    elif mutation == 'different_release':
        edit_manifest(live, lambda m: m.update(gene=gene(7)))
    elif mutation == 'unreadable_manifest':
        (live / 'manifest.json').write_text('{"gene": ')
    elif mutation == 'symlinked_manifest':
        (live / 'manifest.json').rename(live / 'real.json')
        (live / 'manifest.json').symlink_to(live / 'real.json')
    elif mutation == 'invalid_image':
        edit_manifest(live, lambda m: m['old']['runtime'].update(id='sha256:short'))
    elif mutation == 'missing_backup_file':
        (backup_of(behind) / 'db/controller.sqlite3').unlink()
    elif mutation == 'missing_source':
        shutil.rmtree(backup_of(behind) / 'installed-source')
    elif mutation == 'backup_outside':
        edit_manifest(live, lambda m: m.update(backup=str(root / 'backups' / '..' / 'staging')))
    elif mutation == 'backup_disagrees':
        (backup_of(live) / 'worker.env').write_text('GENE_WORKER_IMAGE=gene-worker:' + gene(1) + '\n')
    elif mutation == 'old_image_changed':
        images[iid(RUNTIME + 2)]['tags'] = []
        images[iid(400)] = {'tags': ['gene-runtime:' + gene(2)]}
    elif mutation == 'new_fri_missing':
        images[iid(FRI + 4)]['tags'] = []
    elif mutation == 'missing_cogs':
        (root / 'releases' / gene(2) / 'cogs').rmdir()
    elif mutation == 'pointer':
        db.execute("UPDATE settings SET value=? WHERE key='previous_sha'", (gene(2),))
        db.commit()
    elif mutation == 'live_pins':
        (root / 'release.env').write_text(f'GENE_RELEASE_SHA={gene(4)}\nGENE_RUNTIME_IMAGE=gene-runtime:{gene(3)}\n')
    elif mutation == 'live_fri':
        (batch_host.fri_root / 'release.env').write_text(
            f'FRI_RELEASE_SHA={fri(3)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(3)}\n')
    elif mutation == 'ambiguous':
        copy_dir = root / 'staging' / (live.name + '-2')
        copy_dir.mkdir()
        (copy_dir / 'manifest.json').write_text((live / 'manifest.json').read_text())
        (copy_dir / 'journal').write_text((live / 'journal').read_text())
        at = (live / 'journal').stat().st_mtime
        os.utime(copy_dir / 'journal', (at, at))
    elif mutation == 'unknown_dir':
        (root / 'staging' / 'batch-notes').mkdir()
        (root / 'staging' / 'batch-notes' / 'journal').write_text('baseline\n')
    with pytest.raises(ValueError, match=message):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-plan.json').exists()
    assert not (root / 'maintenance/image-retention-state.json').exists()


@pytest.mark.parametrize('marks', [
    LIVE[:5],  # drained, then stopped before the switch
    LIVE[:15],  # switching
    LIVE + ['intent:rollback', 'fri_restored'],  # a rollback after resume, not finished
    ['baseline', 'staged', 'rehearsed', 'intent:pause'],  # a drain that paused and timed out
])
def test_a_batch_in_flight_holds_retention_until_it_resumes(batch_host, marks):
    root = batch_host.root
    write_batch(root, 5, 4, NOW - 60, marks=marks)
    with pytest.raises(retention.Busy, match='mid-release or mid-rollback'):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-plan.json').exists()


def test_a_staged_batch_keeps_every_image_it_names_without_blocking(batch_host):
    root, images = batch_host.root, batch_host.images
    for tag, image in release_images(5).values():
        images[image] = {'tags': [tag]}
    write_batch(root, 5, 4, NOW - 60, marks=['baseline', 'staged', 'rehearsed'], backup=False)
    result = retention.run(root)
    for _, image in release_images(5).values():
        assert result['keep'][image]['reason'].startswith(retention.UNFINISHED)
    assert [row['id'] for row in result['delete']] == [iid(WORKER + 0), iid(RUNTIME + 0)]


def test_a_rolled_back_batch_is_not_lineage_and_its_images_need_evidence(batch_host):
    root, images = batch_host.root, batch_host.images
    for tag, image in release_images(5).values():
        images[image] = {'tags': [tag]}
    write_batch(root, 5, 4, NOW - 60, marks=LIVE + ['intent:rollback', 'gene_rolled_back', 'resumed_old'])
    result = retention.run(root)
    assert [step['sha'] for step in result['lineage']] == [gene(4), gene(3)]
    # Its new images went into service and back out, with no supersession evidence of their own.
    assert result['keep'][iid(RUNTIME + 5)]['reason'].startswith(retention.RECENT)
    # cleanup's recent window counts any batch that resumed, so release 1 leaves it, as cleanup would.
    assert [row['id'] for row in result['delete']] == [iid(WORKER + 0), iid(WORKER + 1), iid(RUNTIME + 0),
                                                      iid(RUNTIME + 1)]
    assert result['keep'][iid(RUNTIME + 2)]['reason'].startswith(retention.ROLLBACK)


def test_cleanup_recent_releases_are_kept_and_older_batch_evidence_waits_out_the_grace(batch_host):
    root = batch_host.root
    result = retention.run(root)
    assert result['keep'][iid(RUNTIME + 1)]['reason'] == f'{retention.RECENT} ({stage_of(root, 2).name})'
    assert result['keep'][iid(RUNTIME + 2)]['reason'].startswith(retention.ROLLBACK)
    journal = stage_of(root, 1) / 'journal'
    os.utime(journal, (NOW - 60, NOW - 60))
    for n in (2, 3, 4):  # keep the order: every later batch resumed later still
        os.utime(stage_of(root, n) / 'journal', (NOW - 50 + n, NOW - 50 + n))
    result = retention.run(root)
    assert result['delete'] == []
    assert result['keep'][iid(RUNTIME + 0)]['reason'] == '24-hour supersession grace period'


def cog_on_top(batch_host, kind):
    """A deployer release above batch 4: a feature job (cog c on Gene 4), or the owner's rollback to Gene 3."""
    root, db = batch_host.root, batch_host.db
    sha, base = ('c' * 40, gene(4)) if kind == 'feature' else (gene(3), gene(4))
    (root / 'release.env').write_text(f'GENE_RELEASE_SHA={sha}\nGENE_ROOT={root}\nGENE_RUNTIME_IMAGE=gene-runtime:{gene(4)}\n')
    (root / 'releases' / base / 'cogs/gene').mkdir(parents=True)
    (root / 'releases' / base / 'cogs/gene/__init__.py').touch()
    (root / 'backups/cog-job').mkdir()
    for name in ('controller.sqlite3', 'red.tar.gz'):
        (root / 'backups/cog-job' / name).touch()
    db.execute("UPDATE settings SET value=? WHERE key='active_sha'", (sha,))
    db.execute("UPDATE settings SET value=? WHERE key='previous_sha'", (base,))
    db.execute('INSERT INTO jobs VALUES (?,?,?,?,?)',
               ('cog-job', kind, 'deployed', json.dumps({'sha': sha, 'base_sha': base}), sha))
    db.execute('INSERT INTO deployments VALUES (?,?,?,?,?)', ('cog-job', sha, base, NOW - 3600, 'deployed'))
    db.commit()


def test_a_deployer_release_above_batches(batch_host):
    root = batch_host.root
    cog_on_top(batch_host, 'rollback')
    result = retention.run(root)  # undoing the owner's rollback restores the pair batch 4's rollback expects
    assert [step['record'].split()[0] for step in result['lineage']] == ['cog', 'batch', 'batch']


def test_a_feature_deploy_above_a_batch_leaves_it_unrecoverable_by_the_release_script(batch_host):
    cog_on_top(batch_host, 'feature')
    # Undoing the feature through the deployer leaves previous_sha at the feature, which pair_check refuses.
    with pytest.raises(ValueError, match=r'rollback-gene would refuse \(release settings\)'):
        retention.run(batch_host.root)


def test_a_batch_above_a_complete_legacy_install(batch_host):
    root, db = batch_host.root, batch_host.db
    for n in (1, 2, 3):
        (stage_of(root, n) / 'journal').unlink()
    legacy_level(root, db, gene(3), gene(2), NOW - 3 * DAY, complete=True)
    result = retention.run(root)
    assert [step['record'].split()[0] for step in result['lineage']] == ['batch', 'deployment']
    assert result['keep'][iid(RUNTIME + 2)]['reason'].startswith(retention.ROLLBACK + ' (deployment')


def test_a_pending_deployment_receipt_skips_the_run(batch_host):
    root = batch_host.root
    (root / 'deploy-result.json').write_text('{}')
    with pytest.raises(retention.Busy, match='receipt'):
        retention.run(root, apply=True)


@pytest.mark.parametrize('case', ['exact', 'unknown_junction', 'shared_removed', 'branch', 'kept_child',
                                  'inconsistent'])
def test_byte_estimate_never_double_counts_shared_layers(case):
    def image(layers, size):
        return {'parent': '', 'size': size, 'layers': ['sha256:' + layer for layer in layers]}
    kept, removed = {}, {}
    if case == 'exact':
        kept['k'], removed['r'] = image(['a'], 60), image(['a', 'b'], 100)
        expected = (40, 40)
    elif case == 'unknown_junction':  # no image ends at the shared layer: its size is only bounded
        kept['k'], removed['r'] = image(['a', 'c'], 80), image(['a', 'b'], 100)
        expected = (20, 100)
    elif case == 'shared_removed':  # one removed layer under two removed images counts once
        kept['k'] = image(['a'], 60)
        removed |= {'r1': image(['a', 'b'], 100), 'r2': image(['a', 'b', 'd'], 130)}
        expected = (70, 70)
    elif case == 'branch':
        kept['k'] = image(['a'], 60)
        removed |= {'r1': image(['a', 'b', 'e'], 120), 'r2': image(['a', 'b', 'f'], 110)}
        expected = (60, 110)
    elif case == 'kept_child':  # a kept image built on the removed one frees nothing
        kept['k'], removed['r'] = image(['a', 'b'], 100), image(['a'], 60)
        expected = (0, 0)
    else:
        kept['k'], removed['r'] = image(['a'], 60), image(['a'], 70)
        expected = (0, 70)
    details = kept | removed
    result = retention.estimate_bytes(details, set(removed))
    assert (result['lower_bytes'], result['upper_bytes']) == expected
    assert result['exact'] is (expected[0] == expected[1] and case != 'inconsistent')
    if case == 'inconsistent':
        assert result['method'].startswith('coarse')


def test_the_plan_says_when_docker_will_keep_a_planned_image_and_when_sizes_are_unavailable(batch_host, monkeypatch):
    root, details = batch_host.root, batch_host.details
    batch_host.images[iid(301)] = {'tags': ['gene-tool:special']}
    details[iid(301)] = chain(['base', 'runtime-0', 'tool'], 700, iid(RUNTIME + 0))
    result = retention.run(root)
    planned = {row['id']: row for row in result['delete']}
    assert 'docker_keeps' in planned[iid(RUNTIME + 0)] and 'docker_keeps' not in planned[iid(WORKER + 0)]
    assert result['bytes']['lower_bytes'] == result['bytes']['upper_bytes'] == 50  # only the worker's layer
    monkeypatch.setattr(retention, 'image_details', lambda ids: (_ for _ in ()).throw(
        retention.subprocess.CalledProcessError(1, 'docker')))
    result = retention.run(root)
    assert result['bytes']['lower_bytes'] is None and result['bytes']['method'].startswith('unavailable')
    assert result['keep'][iid(300)]['reason'] == retention.UNCLASSIFIED


def test_an_untagged_parent_of_a_kept_image_is_explained(batch_host):
    root, details = batch_host.root, batch_host.details
    batch_host.images[iid(302)] = {'tags': []}
    details[iid(302)] = chain(['base'], 500)
    details[iid(RUNTIME + 4)]['parent'] = iid(302)
    result = retention.run(root)
    assert result['keep'][iid(302)]['reason'] == retention.KEPT_PARENT


# The window: the run as the timer starts it.

def actions(root):
    path = root / 'production-actions.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_outside_the_window_a_run_takes_no_lock_and_writes_only_its_log_line(batch_host, monkeypatch):
    root = batch_host.root
    monkeypatch.setattr(retention.time, 'time', lambda: NOW + 3600)  # 05:10
    assert retention.execute(root, apply=True) == (retention.REFUSED, None)
    assert sorted(p.name for p in (root / 'maintenance').iterdir()) == ['image-retention-policy.json']
    [line] = actions(root)
    assert line['action'] == 'image-retention apply' and line['outcome'].startswith('refused: outside the maintenance')
    assert line['tz'] == 'America/Los_Angeles' and line['local_date'] == '2026-10-28'
    monkeypatch.setattr(retention.time, 'time', lambda: float('nan'))
    assert retention.execute(root)[0] == retention.REFUSED


def test_an_owner_exception_runs_outside_the_window_and_is_logged(batch_host, monkeypatch):
    root = batch_host.root
    monkeypatch.setattr(retention.time, 'time', lambda: NOW + 6 * 3600)
    code, result = retention.execute(root, exception='owner, 2026-10-28 10:10, disk at 95%')
    assert code == 0 and result['deletion'] == 'plan only'
    assert actions(root)[-1]['exception'] == 'owner, 2026-10-28 10:10, disk at 95%'


def test_each_run_is_logged_with_exact_images_and_no_secrets(batch_host, monkeypatch):
    root, images = batch_host.root, batch_host.images
    monkeypatch.setenv('RETENTION_TRIGGER', 'gene-image-retention.timer')
    monkeypatch.setenv('RETENTION_ACTOR', 'gene-image-retention.service')
    monkeypatch.delenv('SUDO_USER', raising=False)

    def docker(*args):
        if args[:3] == ('docker', 'image', 'inspect'):
            return json.dumps([{'Id': next(i for i, row in images.items() if args[3] in row['tags'])}])
        images.pop(next(i for i, row in images.items() if args[4] in row['tags']))
        return ''

    monkeypatch.setattr(retention, 'command', docker)
    assert retention.execute(root)[0] == 0
    assert retention.execute(root, apply=True)[0] == 0
    plan, apply = actions(root)
    assert plan['action'] == 'image-retention plan' and plan['outcome'] == 'ok'
    assert plan['images']['delete'] == {iid(WORKER + 0): ['gene-worker:' + gene(0)],
                                        iid(RUNTIME + 0): ['gene-runtime:' + gene(0)]}
    assert apply['images']['removed_tags'] == ['gene-worker:' + gene(0), 'gene-runtime:' + gene(0)]
    assert apply['before'] == apply['after'] == {'gene': gene(4), 'fri': fri(4)}
    assert apply['actor'] == 'gene-image-retention.service' and apply['trigger'] == 'gene-image-retention.timer'
    assert {'at', 'local_date', 'tz', 'actor', 'trigger', 'action', 'reason', 'before', 'after', 'duration_s',
            'restart_s', 'outcome', 'exception'} <= apply.keys()
    for name in ('production-actions.jsonl', 'maintenance/image-retention-status.json',
                 'maintenance/image-retention-plan.json', 'maintenance/image-retention-result.json'):
        assert 'tok-3f9a-secret' not in (root / name).read_text()


def test_status_records_success_failure_and_skips(batch_host, monkeypatch):
    root = batch_host.root
    path = root / 'maintenance/image-retention-status.json'
    assert retention.execute(root)[0] == 0
    first = retention.read_json(path)
    assert first['last_success']['mode'] == 'plan' and first['consecutive_failures'] == 0
    assert first['last_success']['delete'] == {iid(WORKER + 0): ['gene-worker:' + gene(0)],
                                               iid(RUNTIME + 0): ['gene-runtime:' + gene(0)]}
    (stage_of(root, 3) / 'manifest.json').write_text('[')
    for _ in range(2):
        with pytest.raises(ValueError):
            retention.execute(root)
    record = retention.read_json(path)
    assert record['consecutive_failures'] == 2 and record['last_run']['outcome'] == 'failed'
    assert 'Malformed batch record' in record['last_run']['detail']
    assert record['last_success'] == first['last_success']
    assert actions(root)[-1]['outcome'].startswith('failed: ValueError: Malformed batch record')
    (stage_of(root, 3) / 'manifest.json').write_text((stage_of(root, 4) / 'manifest.json').read_text())
    lock = (root / 'maintenance/image-retention.lock').open('a')
    retention.fcntl.flock(lock, retention.fcntl.LOCK_EX | retention.fcntl.LOCK_NB)
    try:
        assert retention.execute(root) == (retention.REFUSED, None)
    finally:
        lock.close()
    record = retention.read_json(path)
    assert record['last_run']['outcome'] == 'skipped' and record['consecutive_failures'] == 2
    assert actions(root)[-1]['outcome'] == 'skipped: Another image-retention run holds its lock'


def systemctl(timer='enabled', active='active', result='success', status='0'):
    def show(*args):
        assert args[:2] == ('systemctl', 'show')
        if args[2] == 'gene-image-retention.timer':
            return f'UnitFileState={timer}\nActiveState={active}\nLastTriggerUSec=x\nNextElapseUSecRealtime=y\n'
        return f'ActiveState=inactive\nResult={result}\nExecMainStatus={status}\nExecMainExitTimestamp=z\n'
    return show


@pytest.mark.parametrize('case, state', [('ok', 'ok'), ('service_failed', 'failing'), ('run_failed', 'failing'),
                                         ('stale', 'stale'), ('disabled', 'disabled'), ('never', 'never-run'),
                                         ('no_systemd', 'unknown')])
def test_status_reports_an_enabled_but_failing_timer_as_unhealthy(batch_host, monkeypatch, case, state):
    root = batch_host.root
    if case != 'never':
        assert retention.execute(root)[0] == 0
    monkeypatch.setattr(retention, 'command', systemctl(
        timer='disabled' if case == 'disabled' else 'enabled',
        result='exit-code' if case == 'service_failed' else 'success',
        status='1' if case == 'service_failed' else '0'))
    if case == 'run_failed':
        (stage_of(root, 4) / 'manifest.json').write_text('{}')
        with pytest.raises(ValueError):
            retention.execute(root)
    if case == 'no_systemd':
        monkeypatch.setattr(retention, 'command', lambda *args: (_ for _ in ()).throw(FileNotFoundError()))
    now = NOW + (retention.STALE + 60 if case == 'stale' else 60)
    report = retention.status(root, now)
    assert report['status'] == state and report['healthy'] is (state == 'ok')
    if case != 'never':
        assert report['last_success_at'].startswith('2026-10-28T04:10:00-07:00')
    if case == 'service_failed':
        assert report['reasons'] == ['the last service run ended with exit-code (exit status 1)']
    assert json.dumps(report)  # what the status command prints


def test_the_status_command_is_read_only(batch_host, monkeypatch, capsys):
    root = batch_host.root
    assert retention.execute(root)[0] == 0
    before = {p.name: p.stat().st_mtime_ns for p in (root / 'maintenance').iterdir()}
    monkeypatch.setattr(retention.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(retention, 'command', systemctl())
    monkeypatch.setattr(retention.time, 'time', lambda: NOW + 12 * 3600)  # any hour
    assert retention.main(['--status', '--root', str(root), '--fri-root', str(batch_host.fri_root)]) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'ok'
    assert {p.name: p.stat().st_mtime_ns for p in (root / 'maintenance').iterdir()} == before
    assert len(actions(root)) == 1


def test_the_window_matches_the_controllers_copy():
    from gene import maintenance
    day, hour = 24 * 3600, 3600
    moments = [retention.EARLIEST - 1, float('nan'), float('inf')]
    # The daylight-saving ends of 2026 and 2027 and an ordinary day, by the quarter hour and at the edges.
    for start in (1793145600, 1793664000, 1804982400):  # 2026-10-28, 2026-11-01, 2027-03-14 at 00:00Z
        moments += [start + step * 900 for step in range(4 * 24 * 2)]
        moments += [start + offset for h in range(0, 2 * day, hour) for offset in (h - 1, h, h + 1)]
    for moment in moments:
        assert retention.window_open(moment) is maintenance.is_open(moment), moment


def test_the_units_run_once_a_day_inside_the_window_and_skips_are_not_failures():
    from configparser import ConfigParser
    units = retention.Path(__file__).parents[1] / 'ops'
    timer, service = ConfigParser(strict=False), ConfigParser(strict=False)
    timer.read(units / 'gene-image-retention.timer')
    service.read(units / 'gene-image-retention.service')
    calendar = timer['Timer']['OnCalendar']
    assert calendar.endswith(' America/Los_Angeles')
    hour, minute, _ = calendar.split()[1].split(':')
    delay = int(timer['Timer']['RandomizedDelaySec'].removesuffix('min'))
    assert int(hour) == 4 and int(minute) + delay < 50  # starts with room before 05:00
    assert timer['Timer']['Persistent'] == 'false'
    assert service['Service']['SuccessExitStatus'] == str(retention.REFUSED)
    assert service['Service']['ExecStart'].endswith('gene_image_retention.py --apply')
    assert '-/srv/gene/production-actions.jsonl' in service['Service']['ReadWritePaths'].split()


def removing(images, calls=None, on_inspect=None):
    """Docker for an apply run: inspect names the tag's image, rm drops it from the inventory."""
    def docker(*args):
        if args[:3] == ('docker', 'image', 'inspect'):
            if on_inspect:
                on_inspect(args[3])
            return json.dumps([{'Id': next(i for i, row in images.items() if args[3] in row['tags'])}])
        assert args[:4] == ('docker', 'image', 'rm', '--no-prune')
        if calls is not None:
            calls.append(args[4])
        images.pop(args[4] if args[4] in images else next(i for i, row in images.items() if args[4] in row['tags']))
        return ''
    return docker


@pytest.mark.parametrize('change', ['manifest', 'manifest_and_backup', 'no_backup'])
def test_an_older_batch_that_does_not_check_out_supersedes_nothing(batch_host, monkeypatch, change):
    root, images = batch_host.root, batch_host.images
    image, tag = iid(999), 'gene-runtime:' + gene(9)
    images[image] = {'tags': [tag]}
    batch_host.metadata[image] = {'id': image, 'parent': '', 'tags': [tag]}
    batch_host.details[image] = chain(['unexpected'], 123)
    stage = stage_of(root, 1)
    if change == 'no_backup':  # pruned: its pins can no longer be checked
        shutil.rmtree(backup_of(stage))
    else:
        edit_manifest(stage, lambda m: m['old']['runtime'].update(tag=tag, id=image))
    if change == 'manifest_and_backup':
        (backup_of(stage) / 'release.env').write_text(f'GENE_RELEASE_SHA={gene(0)}\nGENE_RUNTIME_IMAGE={tag}\n')
    calls = []
    monkeypatch.setattr(retention, 'command', removing(images, calls))
    result = retention.run(root, apply=True)
    assert calls == [] and image in images
    if change != 'no_backup':
        assert result['keep'][image]['reason'] == 'no evidence that this image was superseded'
    assert result['keep'][iid(WORKER + 0)]['reason'] == 'no evidence that this image was superseded'


@pytest.mark.parametrize('change, message', [
    ('no_baseline', 'baseline.json is missing'),
    ('unreadable_baseline', 'baseline.json is unreadable'),
    ('baseline_release', 'baseline.json is not the old release'),
    ('baseline_presence', 'baseline.json has no gene guild and presence'),
    ('baseline_pointer', 'baseline.json previous_sha .* is not the next rollback level'),
    ('no_downgrades', 'its manifest records no downgrades'),
    ('no_attempt', 'its journal has no gene_attempted'),
    ('old_compose', 'is missing installed-source/ops/compose.yml'),
    ('source_marker', r'would stop restoring the source \(old source not in place\)'),
    ('fri_record', r'its rollback-gene would stop at verify old \(fri_record\)'),
    ('old_format', 'manifest names no old pair'),
])
def test_a_rollback_level_needs_everything_its_rollback_reads(batch_host, monkeypatch, change, message):
    root = batch_host.root
    latest, behind = stage_of(root, 4), stage_of(root, 3)
    if change == 'no_baseline':
        (latest / 'baseline.json').unlink()
    elif change == 'unreadable_baseline':
        (behind / 'baseline.json').write_text('{"settings": ')
    elif change == 'baseline_release':
        edit_baseline(behind, lambda b: b['settings'].update(active_sha=gene(7)))
    elif change == 'baseline_presence':
        edit_baseline(latest, lambda b: b['gene'].pop('presence'))
    elif change == 'baseline_pointer':
        edit_baseline(latest, lambda b: b['settings'].update(previous_sha=gene(1)))
    elif change == 'no_downgrades':
        edit_manifest(behind, lambda m: m.pop('downgrades'))
    elif change == 'no_attempt':
        remove_journal_marks(root, 4, 'gene_attempted')
    elif change == 'old_compose':
        (backup_of(behind) / 'installed-source/ops/compose.yml').unlink()
    elif change == 'source_marker':
        (backup_of(latest) / 'installed-source/.release-sha').write_text(gene(2) + '\n')
    elif change == 'fri_record':
        (backup_of(behind) / 'fri-release.json').write_text(json.dumps({'sha': fri(1), 'previous': fri(0)}))
    elif change == 'old_format':
        edit_manifest(behind, lambda m: [m.pop('old_gene'), m.pop('old_fri')])
    monkeypatch.setattr(retention, 'command', removing(batch_host.images))
    with pytest.raises(ValueError, match=message):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-plan.json').exists()


def edit_baseline(stage, change):
    path = stage / 'baseline.json'
    baseline = json.loads(path.read_text())
    change(baseline)
    path.write_text(json.dumps(baseline))


def batch_start(root):
    """A batch-release.sh command starting: exec 8>> batch-release.lock; flock -n 8."""
    handle = open(root / 'batch-release.lock', 'a')
    try:
        retention.fcntl.flock(handle, retention.fcntl.LOCK_EX | retention.fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def test_no_batch_command_can_start_while_retention_runs(batch_host, monkeypatch):
    root, images = batch_host.root, batch_host.images
    inventory, attempts = retention.docker_inventory, []

    def snapshot_inventory():
        # A batch starting now could stage a new release that names a planned image.
        handle = batch_start(root)
        attempts.append(handle is not None)
        if handle is not None:
            handle.close()
        return inventory()

    monkeypatch.setattr(retention, 'docker_inventory', snapshot_inventory)
    calls = []
    monkeypatch.setattr(retention, 'command', removing(images, calls))
    retention.run(root, apply=True)
    assert calls == ['gene-worker:' + gene(0), 'gene-runtime:' + gene(0)]
    assert len(attempts) == 5 and not any(attempts)
    handle = batch_start(root)  # and the lock is free again afterwards
    assert handle is not None
    handle.close()


def test_a_running_batch_command_skips_the_run_and_its_own_lock_check_is_untouched(batch_host, monkeypatch):
    root = batch_host.root
    handle = batch_start(root)
    try:
        monkeypatch.setattr(retention, 'command', lambda *args: pytest.fail(f'unexpected {args}'))
        assert retention.execute(root, apply=True) == (retention.REFUSED, None)
    finally:
        handle.close()
    assert actions(root)[-1]['outcome'] == 'skipped: A batch-release command holds its lock; retry in the next window'
    assert not (root / 'maintenance/image-retention-plan.json').exists()


@pytest.mark.parametrize('path', ['tag', 'ancestor'])
def test_the_window_is_checked_again_right_before_each_removal(batch_host, monkeypatch, path):
    root, images, metadata = batch_host.root, batch_host.images, batch_host.metadata
    clock = [NOW]
    monkeypatch.setattr(retention.time, 'time', lambda: clock[0])
    calls = []
    if path == 'tag':  # 05:00 passes while the first tag is inspected
        docker = removing(images, calls, on_inspect=lambda tag: clock.__setitem__(0, NOW + 3600))
    else:  # an unnamed parent of release 0's runtime, collected after the tags
        images[iid(500)] = {'tags': []}
        metadata[iid(500)] = {'id': iid(500), 'parent': iid(204), 'tags': []}
        metadata[iid(RUNTIME + 0)]['parent'] = iid(500)
        batch_host.details[iid(500)] = chain(['base', 'parent-0'], 550, iid(204))
        lookup = retention.image_metadata

        def inspect(ref):
            if ref == iid(500) and iid(RUNTIME + 0) not in images:
                clock[0] = NOW + 3600  # 05:00 passes while the ancestor is inspected
            return lookup(ref)
        monkeypatch.setattr(retention, 'image_metadata', inspect)
        docker = removing(images, calls)
    monkeypatch.setattr(retention, 'command', docker)
    result = retention.run(root, apply=True)
    assert 'window closed' in result['deferred']
    if path == 'tag':
        assert calls == []
        state = retention.read_json(root / 'maintenance/image-retention-state.json')
        assert state.get('removal_intents', {}) == {}
    else:
        assert calls == ['gene-worker:' + gene(0), 'gene-runtime:' + gene(0)] and iid(500) in images


def test_an_interrupted_removal_run_still_logs_what_it_removed(batch_host, monkeypatch):
    root, images = batch_host.root, batch_host.images
    calls = []

    def stall(tag):
        if calls:  # Docker stops answering after the first removal
            raise retention.subprocess.TimeoutExpired('docker', 120)

    monkeypatch.setattr(retention, 'command', removing(images, calls, on_inspect=stall))
    with pytest.raises(retention.subprocess.TimeoutExpired):
        retention.execute(root, apply=True)
    line = actions(root)[-1]
    assert line['outcome'].startswith('failed: TimeoutExpired')
    assert line['images']['removed_tags'] == calls == ['gene-worker:' + gene(0)]
    assert retention.read_json(root / 'maintenance/image-retention-result.json')['removed_tags'] == calls


def test_a_run_keeps_the_batch_lock_for_a_bounded_time(batch_host, monkeypatch):
    root, images = batch_host.root, batch_host.images
    clock = [1000.0]
    monkeypatch.setattr(retention, 'monotonic', lambda: clock[0])
    calls = []

    def slow(tag):  # still inside the window, but too little of the hold is left once the first tag is done
        if calls:
            clock[0] += retention.LOCK_BUDGET - retention.REMOVAL_MARGIN + 1

    monkeypatch.setattr(retention, 'command', removing(images, calls, on_inspect=slow))
    result = retention.run(root, apply=True)
    assert calls == ['gene-worker:' + gene(0)] and 'hold on batch-release.lock' in result['deferred']
    assert retention.window_open(retention.time.time())


def test_a_batch_record_written_during_discovery_stops_the_run(batch_host, monkeypatch):
    root, images = batch_host.root, batch_host.images
    inventory, count = retention.docker_inventory, [0]

    def snapshot_inventory():
        count[0] += 1
        if count[0] == 3:  # the last check before the first removal: staging gains a batch naming release 0
            write_batch(root, 5, 0, NOW - 60, marks=['baseline'], backup=False)
        return inventory()

    monkeypatch.setattr(retention, 'docker_inventory', snapshot_inventory)
    calls = []
    monkeypatch.setattr(retention, 'command', removing(images, calls))
    with pytest.raises(ValueError) as stopped:
        retention.run(root, apply=True)
    assert calls == []
    assert isinstance(stopped.value, retention.Busy) and 'Batch records changed during discovery' in str(stopped.value)


# Sol round 2 on gene #80.

FRI_ONLY = ['baseline', 'staged', 'rehearsed', 'intent:pause', 'drained', 'gene_recorded', 'fri_backup', 'intent:fri',
            'fri_switched', 'evals_started', 'evals_ran', 'evaluated', 'verified_off', 'intent:services',
            'done:unpause', 'resumed']


def write_fri_only(root, images, n, new, old, at, backup=True, baseline=True, marks=FRI_ONLY, record=None):
    """A Fri-only batch on Gene n, as batch-release.sh leaves it: its backup holds only Fri's pins and record."""
    images.setdefault(iid(FRI + new), {'tags': ['fri-runtime:' + fri(new)]})
    stage = root / 'staging' / f'batch-{gene(n)}-{fri(new)}'
    stage.mkdir(parents=True)
    gene_images = {role: {'tag': t, 'id': i} for role, (t, i) in release_images(n).items() if role != 'fri'}
    old_images = gene_images | {'fri': {'tag': 'fri-runtime:' + fri(old), 'id': iid(FRI + old)}}
    new_images = gene_images | {'fri': {'tag': 'fri-runtime:' + fri(new), 'id': iid(FRI + new)}}
    manifest = {'gene': gene(n), 'fri': fri(new), 'old_gene': gene(n), 'old_fri': fri(old), 'old': old_images,
                'new': new_images, 'archives': {'fri': 'c' * 64}, 'downgrades': ''}
    if backup:
        folder = root / 'backups' / f'maintainer-fri-{fri(new)}-{int(at)}'
        folder.mkdir()
        (folder / 'fri-release.env').write_text(f'FRI_RELEASE_SHA={fri(old)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(old)}\n')
        (folder / 'fri-release.json').write_text(json.dumps(record or {'sha': fri(old), 'previous': fri(old - 1)}))
        manifest['backup'] = str(folder)
    (stage / 'manifest.json').write_text(json.dumps(manifest))
    if baseline:
        (stage / 'baseline.json').write_text(json.dumps({
            'settings': {'active_sha': gene(n), 'previous_sha': gene(n - 1), 'fri_active_sha': fri(old)},
            'source': gene(n), 'gene': {'guild_id': '123', 'presence': 'online'},
            'fri': {'sha': fri(old), 'guild_id': '123', 'presence': 'online'}}))
    (stage / 'journal').write_text(''.join(mark + '\n' for mark in marks))
    os.utime(stage / 'journal', (at, at))
    return stage


def live_fri(batch_host, n, previous=None):
    """Fri n live, as gene.fri_release leaves it: pins, record, the controller's setting and the container."""
    (batch_host.fri_root / 'release.env').write_text(f'FRI_RELEASE_SHA={fri(n)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(n)}\n')
    (batch_host.fri_root / 'release.json').write_text(json.dumps({'sha': fri(n), 'previous': fri(previous or n - 1)}))
    batch_host.db.execute("UPDATE settings SET value=? WHERE key='fri_active_sha'", (fri(n),))
    batch_host.db.commit()
    batch_host.containers['fri'] = iid(FRI + n)


@pytest.mark.parametrize('case', ['complete', 'two_stacked', 'between_levels', 'no_backup', 'no_baseline', 'pointer',
                                  'source', 'no_fri_backup_mark', 'discontinuous', 'gene_moved',
                                  'between_levels_incomplete'])
def test_a_fri_only_release_above_a_gene_level_must_roll_back_first(batch_host, monkeypatch, case):
    root, images = batch_host.root, batch_host.images
    on_top = {'no_backup': {'backup': False}, 'no_baseline': {'baseline': False},
              'no_fri_backup_mark': {'marks': [m for m in FRI_ONLY if m != 'fri_backup']}}
    names = []
    if case.startswith('between_levels'):
        # Gene 3 / Fri 7 came between batches 3 and 4, so rolling back batch 4 lands on Fri 7, not Fri 3.
        images[iid(FRI + 7)] = {'tags': ['fri-runtime:' + fri(7)]}
        latest = stage_of(root, 4)
        edit_manifest(latest, lambda m: [m.update(old_fri=fri(7)),
                                         m['old']['fri'].update(tag='fri-runtime:' + fri(7), id=iid(FRI + 7))])
        (backup_of(latest) / 'fri-release.env').write_text(f'FRI_RELEASE_SHA={fri(7)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(7)}\n')
        (backup_of(latest) / 'fri-release.json').write_text(json.dumps({'sha': fri(7), 'previous': fri(3)}))
        middle = write_fri_only(root, images, 3, 7, 3, NOW - 1.5 * DAY, backup=case == 'between_levels')
        live_fri(batch_host, 4, previous=7)  # batch 4 released Fri 4 over Fri 7
        names = [latest.name, middle.name, stage_of(root, 3).name]
    else:
        top = write_fri_only(root, images, 4, 5, 4, NOW - 0.5 * DAY, **on_top.get(case, {}))
        live_fri(batch_host, 5)
        names = [top.name, stage_of(root, 4).name, stage_of(root, 3).name]
        if case == 'two_stacked':
            second = write_fri_only(root, images, 4, 6, 5, NOW - 0.25 * DAY)
            live_fri(batch_host, 6)
            names.insert(0, second.name)
        elif case == 'pointer':
            edit_baseline(top, lambda b: b['settings'].update(previous_sha=gene(1)))
        elif case == 'source':
            edit_baseline(top, lambda b: b.update(source='unmarked'))
        elif case == 'discontinuous':  # it says it replaced Fri 3, but batch 4 left Fri 4 running
            edit_manifest(top, lambda m: [m.update(old_fri=fri(3)),
                                          m['old']['fri'].update(tag='fri-runtime:' + fri(3), id=iid(FRI + 3))])
            (backup_of(top) / 'fri-release.env').write_text(f'FRI_RELEASE_SHA={fri(3)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(3)}\n')
            (backup_of(top) / 'fri-release.json').write_text(json.dumps({'sha': fri(3), 'previous': fri(2)}))
        elif case == 'gene_moved':  # the runtime it ran beside isn't the one batch 4 put in service
            runtime = {'tag': 'gene-runtime:' + gene(3), 'id': iid(RUNTIME + 3)}
            edit_manifest(top, lambda m: [m[kind].update(runtime=runtime) for kind in ('old', 'new')])
    monkeypatch.setattr(retention, 'command', removing(images))
    if case in ('complete', 'two_stacked', 'between_levels'):
        result = retention.run(root, apply=True)
        assert [step['record'] for step in result['lineage']] == ['batch ' + name for name in names]
        assert sum(1 for step in result['lineage'] if not step.get('fri_only')) == 2
        for step in result['lineage']:
            if step.get('fri_only'):
                assert result['keep'][iid(FRI + int(step['fri'][-1]))]['reason'].startswith(retention.CONTAINER) or \
                    result['keep'][iid(FRI + int(step['fri'][-1]))]['reason'].startswith(retention.ROLLBACK)
        return
    message = {'no_backup': 'switch backup is missing', 'no_baseline': 'baseline.json is missing',
               'pointer': 'previous_sha .* is not the Gene release it sits on',
               'source': 'baseline.json source is not the Gene release it sits on',
               'no_fri_backup_mark': 'its journal has no fri_backup', 'discontinuous': 'Fri .* does not continue',
               'gene_moved': 'its Gene images are not the ones', 'between_levels_incomplete': 'switch backup is missing'}
    with pytest.raises(ValueError, match=message[case]):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-plan.json').exists()


def test_the_lock_budget_covers_discovery_and_planning(batch_host, monkeypatch):
    root = batch_host.root
    clock = [1000.0]
    monkeypatch.setattr(retention, 'monotonic', lambda: clock[0], raising=False)
    details = retention.image_details

    def slow_inspection(ids):
        clock[0] += retention.LOCK_BUDGET + 1  # image inspection outlasts the lock budget
        return details(ids)

    monkeypatch.setattr(retention, 'image_details', slow_inspection)
    monkeypatch.setattr(retention, 'command', lambda *args: pytest.fail(f'unexpected {args}'))
    with pytest.raises(retention.Busy, match='batch-release.lock'):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-plan.json').exists()
    handle = batch_start(root)  # the lock went back with the skip
    assert handle is not None
    handle.close()


def test_every_command_is_bounded_by_the_time_left_on_the_lock(monkeypatch):
    clock, seen = [1000.0], []
    monkeypatch.setattr(retention, 'monotonic', lambda: clock[0], raising=False)
    monkeypatch.setattr(retention, 'DEADLINE', 1030.0, raising=False)

    def check_output(args, text, timeout):
        seen.append(timeout)
        if args[1] == 'slow':
            raise retention.subprocess.TimeoutExpired(args, timeout)
        return 'ok'

    monkeypatch.setattr(retention.subprocess, 'check_output', check_output)
    assert retention.command('docker', 'fast') == 'ok' and seen == [30.0]
    with pytest.raises(retention.Busy, match='batch-release.lock'):
        retention.command('docker', 'slow')
    clock[0] = 1030.0
    with pytest.raises(retention.Busy, match='batch-release.lock'):
        retention.command('docker', 'fast')
    assert len(seen) == 2  # nothing starts once the budget is spent
    monkeypatch.setattr(retention, 'DEADLINE', None)
    assert retention.command('systemctl', 'show') == 'ok' and seen[-1] == 120


def test_a_skip_never_clears_an_unresolved_failure(batch_host, monkeypatch):
    root = batch_host.root
    assert retention.execute(root)[0] == 0
    (stage_of(root, 4) / 'manifest.json').write_text('{}')
    with pytest.raises(ValueError):
        retention.execute(root)
    lock = (root / 'maintenance/image-retention.lock').open('a')
    retention.fcntl.flock(lock, retention.fcntl.LOCK_EX | retention.fcntl.LOCK_NB)
    try:
        assert retention.execute(root) == (retention.REFUSED, None)
    finally:
        lock.close()
    monkeypatch.setattr(retention, 'command', systemctl())  # exit 4 counts as a successful service run
    report = retention.status(root, NOW + 60)
    assert report['status'] == 'failing' and not report['healthy']
    assert any('Malformed batch record' in reason for reason in report['reasons'])


# Sol round 3 on gene #80: each rollback must pass the release script's own first checks on the state the
# rollbacks above it leave, saved Fri records included.

@pytest.mark.parametrize('saved_by, change', [('fri_only', 'missing'), ('fri_only', 'wrong'),
                                              ('gene', 'missing'), ('gene', 'wrong')])
def test_a_saved_fri_record_must_pass_the_next_rollbacks_check(batch_host, monkeypatch, saved_by, change):
    root, images = batch_host.root, batch_host.images
    previous = {} if change == 'missing' else {'previous': fri(1)}
    if saved_by == 'fri_only':  # rollback-fri restores this record, then batch 4's pair_check reads it
        write_fri_only(root, images, 4, 5, 4, NOW - 0.5 * DAY, record={'sha': fri(4)} | previous)
        live_fri(batch_host, 5)
    else:  # batch 4's rollback-gene restores this record, then batch 3's pair_check reads it
        (backup_of(stage_of(root, 4)) / 'fri-release.json').write_text(json.dumps({'sha': fri(3)} | previous))
    monkeypatch.setattr(retention, 'command', removing(images))
    with pytest.raises(ValueError, match=r'would refuse \(fri release.json\)'):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-plan.json').exists()


REPO = retention.Path(__file__).parents[1]


SCRIPT = REPO / 'ops/maintainer/batch-release.sh'
# Stand-ins only for what reaches outside the host's files: Docker, curl and the controller DB container.
STUBS = r"""
docker() { [[ $* == *State.Health.Status* ]] && echo healthy; }
curl() { echo 200; }
db_py() { cat > /dev/null; printf '%s\n' "$DB_JSON"; }
container_image() {
  case $1 in gene-controller-1) echo "$CONTROLLER_IMAGE" ;; gene-red-1) echo "$RED_IMAGE" ;; fri-runtime-1) echo "$FRI_IMAGE" ;; esac
}
container_running() { [[ ($1 == gene-red-1 && $RED_IMAGE != none) || ($1 == fri-runtime-1 && $FRI_IMAGE != none) ]]; }
"""


def script_function(name):
    """A shell function from batch-release.sh, exactly as written: up to its closing brace, past heredocs."""
    import re
    lines = SCRIPT.read_text().split('\n')
    start = next(i for i, line in enumerate(lines) if line.startswith(name + '() {'))
    if lines[start].rstrip().endswith('}'):
        return lines[start]
    end, terminator = start, None
    while True:
        end += 1
        if terminator:
            terminator = None if lines[end] == terminator else terminator
        elif match := re.search(r"<<'(\w+)'", lines[end]):
            terminator = match[1]
        elif lines[end] == '}':
            return '\n'.join(lines[start:end + 1])


def script_lines(function, first, last):
    """The lines of a script function from the one starting with first up to the one starting with last."""
    body = script_function(function).split('\n')
    begin = next(i for i, line in enumerate(body) if line.strip().startswith(first))
    return '\n'.join(body[begin:next(i for i in range(begin, len(body)) if body[i].strip().startswith(last))])


def stubbed(name):
    """A script function, with the controller DB read in state() replaced by the stubbed DB's answer. That read
    is a heredoc inside $( … ), which bash before 4 cannot parse; everything else runs as written."""
    import re
    text = script_function(name)
    if name == 'state':
        text, count = re.subn(r"  db=\$\(db_py <<'PY'\n.*?\nPY\n  \)\n", '  db=$DB_JSON\n', text, flags=re.S)
        assert count == 1, 'state() no longer reads the controller DB the way this harness stubs it'
    return text


def run_script(tmp, functions, body, env):
    """Run batch-release.sh's own functions under bash, with python3 being this interpreter."""
    import shutil
    import subprocess
    import sys
    tools = tmp / 'tools'
    if not tools.exists():
        tools.mkdir()
        (tools / 'python3').symlink_to(sys.executable)
    program = '\n'.join(['set -Eeuo pipefail', 'SWITCHING=0', 'CONTROLLER=gene-controller-1', 'RED=gene-red-1',
                          'FRI=fri-runtime-1', STUBS, *(stubbed(name) for name in functions), body])
    done = subprocess.run([shutil.which('bash'), '-c', program], capture_output=True, text=True,
                          env={'PATH': f'{tools}:/usr/bin:/bin', 'LC_ALL': 'C', **env})
    return done.returncode, done.stdout + done.stderr


def put_state(batch_host, state, health_dir, expect):
    """Make the host's files, source tree and stubs show a simulated state; expect is the release whose
    health the stubs report (rollback health is the live release's own, not under test)."""
    import shutil
    root, fri_root = batch_host.root, batch_host.fri_root
    for path, key in ((root / 'release.env', 'release.env'), (root / 'secrets/worker.env', 'worker.env'),
                      (fri_root / 'release.env', 'fri release.env'), (fri_root / 'release.json', 'fri release.json')):
        path.unlink(missing_ok=True)
        if state[key] is not None:
            path.write_bytes(state[key])
    source = root / 'source'
    if state['source_tree'] != source:  # a restored tree: its own entries, moved as rename(2) moves them
        if source.is_symlink() or source.is_file():
            source.unlink()
        elif source.exists():
            shutil.rmtree(source)
        if state['source_tree'] is not None:
            shutil.copytree(state['source_tree'], source, symlinks=True)
    health_dir.mkdir(exist_ok=True)
    # Fresh by the clock state() reads in the script's own process: written now, read a moment later.
    gene_sha, fri_sha, baseline = expect
    (health_dir / 'gene.json').write_text(json.dumps({
        'sha': gene_sha, 'guild_id': baseline['gene']['guild_id'], 'presence': baseline['gene']['presence'],
        'guild_connected': True, 'controls_running': True, 'at': REAL_CLOCK()}))
    (health_dir / 'fri.json').write_text(json.dumps({
        'sha': fri_sha, 'guild_id': baseline['fri']['guild_id'], 'presence': baseline['fri']['presence'],
        'guild_connected': True, 'agent_heartbeat': {'running': True, 'failed': []}, 'at': REAL_CLOCK()}))
    settings = {key: value for key, value in state['settings'].items() if value is not None}
    return {'ROOT': str(root), 'FRI_ROOT': str(fri_root), 'GENE_HEALTH': str(health_dir / 'gene.json'),
            'FRI_HEALTH': str(health_dir / 'fri.json'), 'DB_JSON': json.dumps({'settings': settings, 'busy_jobs': 0}),
            'CONTROLLER_IMAGE': state['controller'], 'RED_IMAGE': state['red'], 'FRI_IMAGE': state['fri']}


def batch_env(root, record):
    stage = root / 'staging' / record['name']
    return {'MANIFEST': str(stage / 'manifest.json'), 'BASELINE': str(stage / 'baseline.json'), 'G': record['gene'],
            'F': record['fri'], 'OLD_G': record['old_gene'], 'OLD_F': record['old_fri']}


def source_case(batch_host, case):
    """The source-marker cases: how the script reads and moves the marker, not how Python would."""
    root = batch_host.root
    saved = backup_of(stage_of(root, 4)) / 'installed-source'
    if case == 'source_crlf_live':
        (root / 'source/.release-sha').write_bytes(gene(4).encode() + b'\r\n')
    elif case == 'source_space_saved':
        (saved / '.release-sha').write_text(' ' + gene(3) + '\n')
    elif case == 'source_symlink_saved':  # fine where it is, dangling once installed-source moves to source/
        (saved / '.release-sha').unlink()
        (saved.parent / 'source-sha').write_text(gene(3) + '\n')
        (saved / '.release-sha').symlink_to('../source-sha')
    elif case == 'source_trailing_newlines_saved':  # $(cat …) strips every trailing newline
        (saved / '.release-sha').write_text(gene(3) + '\n\n\n')
    elif case == 'quoted_pins':  # verify old reads release.env with nothing stripped
        (backup_of(stage_of(root, 3)) / 'release.env').write_text(
            f'GENE_RELEASE_SHA="{gene(2)}"\nGENE_ROOT={root}\nGENE_RUNTIME_IMAGE=gene-runtime:{gene(2)}\n')
    elif case == 'worker_space':  # verify old's worker pin is grep -Fx: the whole line, exactly
        (backup_of(stage_of(root, 3)) / 'worker.env').write_text(
            f'GENE_WORKER_IMAGE=gene-worker:{gene(2)} \nGENE_WORKER_TOKEN=tok-3f9a-secret\n')


# db_py for the script's settings writers: their own program, run against a scratch controller DB.
WRITERS_DB = r"""
db_py() {
  python3 -c '
import os, sqlite3, sys
connect = sqlite3.connect
sqlite3.connect = lambda path, *a, **kw: connect(os.environ["TEST_DB"] if path == "/data/gene.sqlite3" else path, *a, **kw)
program = sys.stdin.read()
sys.argv = ["-", *sys.argv[1:]]
exec(compile(program, "<db_py>", "exec"))
' "$@"
}
"""


def script_settings(batch_host, scratch, kind, step, before):
    """The controller settings a rollback writes, by the script's own restore_old_fri decision, set_fri_active
    and (rollback-gene) settings_transition with prev=$(jget "$BASELINE" settings.previous_sha)."""
    import sqlite3
    scratch.mkdir()
    db = sqlite3.connect(scratch / 'gene.sqlite3')
    db.executescript('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT); '
                     'CREATE TABLE audit(at, actor, action, target, detail);')
    db.executemany('INSERT INTO settings VALUES (?,?)', [(k, v) for k, v in before['settings'].items() if v is not None])
    db.commit()
    record = step['record']
    env = put_state(batch_host, before, scratch / 'health', (record['gene'], record['fri'], step['baseline']))
    env |= batch_env(batch_host.root, record) | {'TEST_DB': str(scratch / 'gene.sqlite3')}
    body = WRITERS_DB + 'old_fri_pinned || set_fri_active "$OLD_F"\n'
    if kind == 'gene':
        body += 'prev=$(jget "$BASELINE" settings.previous_sha)\nsettings_transition "$G" "$OLD_G" "$OLD_G" "$prev" r\n'
    functions = ['jget', 'backup_dir', 'source_state', 'state', 'old_fri_pinned', 'set_fri_active', 'settings_transition']
    returncode, out = run_script(scratch, functions, body, env)
    assert returncode == 0, out[-800:]
    stored = dict(db.execute('SELECT key,value FROM settings'))
    return {key: stored.get(key) for key in ('active_sha', 'previous_sha', 'fri_active_sha')}


REFUSED = {'fri_only_missing_previous', 'gene_wrong_previous', 'feature', 'source_crlf_live', 'source_space_saved',
           'source_symlink_saved', 'quoted_pins', 'worker_space'}


@pytest.mark.parametrize('case', ['plain', 'fri_only', 'two_stacked', 'between_levels', 'owner_rollback',
                                  'source_trailing_newlines_saved', 'fri_only_missing_previous', 'gene_wrong_previous',
                                  'feature', 'source_crlf_live', 'source_space_saved', 'source_symlink_saved',
                                  'quoted_pins', 'worker_space'])
def test_retention_and_the_release_script_agree_on_every_rollback_check(batch_host, monkeypatch, tmp_path, case):
    """Each check retention simulates runs again through batch-release.sh's own functions, on the same files,
    trees and settings: state, pair_check, fri_pair_check and gene_unmoved, rollback-gene's source restore, and
    verify old. A change to either side that makes them disagree fails here."""
    root, images = batch_host.root, batch_host.images
    if case in ('fri_only', 'two_stacked', 'fri_only_missing_previous'):
        record = {'sha': fri(4)} if case == 'fri_only_missing_previous' else None
        write_fri_only(root, images, 4, 5, 4, NOW - 0.5 * DAY, record=record)
        live_fri(batch_host, 5)
        if case == 'two_stacked':
            write_fri_only(root, images, 4, 6, 5, NOW - 0.25 * DAY)
            live_fri(batch_host, 6)
    elif case == 'between_levels':
        images[iid(FRI + 7)] = {'tags': ['fri-runtime:' + fri(7)]}
        latest = stage_of(root, 4)
        edit_manifest(latest, lambda m: [m.update(old_fri=fri(7)),
                                         m['old']['fri'].update(tag='fri-runtime:' + fri(7), id=iid(FRI + 7))])
        edit_baseline(latest, lambda b: b['settings'].update(fri_active_sha=fri(7)))
        (backup_of(latest) / 'fri-release.env').write_text(f'FRI_RELEASE_SHA={fri(7)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(7)}\n')
        (backup_of(latest) / 'fri-release.json').write_text(json.dumps({'sha': fri(7), 'previous': fri(3)}))
        write_fri_only(root, images, 3, 7, 3, NOW - 1.5 * DAY)
        live_fri(batch_host, 4, previous=7)
    elif case == 'gene_wrong_previous':
        (backup_of(stage_of(root, 4)) / 'fri-release.json').write_text(json.dumps({'sha': fri(3), 'previous': fri(1)}))
    elif case in ('owner_rollback', 'feature'):
        cog_on_top(batch_host, 'rollback' if case == 'owner_rollback' else 'feature')
    else:
        source_case(batch_host, case)
    checked = []
    for name in ('pair_mirror', 'restore_source', 'verify_mirror'):
        original = getattr(retention, name)

        def recording(*args, _name=name, _original=original):
            outcome = _original(*args)
            checked.append((_name, copy.deepcopy(args), copy.deepcopy(outcome)))
            return outcome
        monkeypatch.setattr(retention, name, recording)
    monkeypatch.setattr(retention, 'command', removing(images))
    try:
        retention.run(root)
    except ValueError as error:
        assert case in REFUSED and 'Batch ' in str(error) or 'cog job' in str(error), str(error)
    else:
        assert case not in REFUSED
    failing = [bool(outcome[0] if name == 'restore_source' else outcome) for name, _, outcome in checked]
    assert checked and failing == [False] * (len(failing) - (case in REFUSED)) + [True] * (case in REFUSED)
    for number, (name, args, outcome) in enumerate(checked):
        if name == 'pair_mirror':
            kind, step, state, _ = args
            env = put_state(batch_host, state, tmp_path / 'health', (step['record']['gene'], step['record']['fri'],
                                                                     step['baseline']))
            env |= batch_env(root, step['record'])
            body = 'pair_check' if kind == 'gene' else 'fri_pair_check && gene_unmoved'
            functions = ['jget', 'backup_dir', 'source_state', 'state', 'pair_check', 'fri_pair_check', 'gene_unmoved']
            returncode, out = run_script(tmp_path, functions, body, env)
            assert (returncode == 0) == (not outcome), (number, name, outcome, out[-800:])
        elif name == 'restore_source':  # rollback-gene's own lines, on copies of the trees it moves
            import shutil
            step, state = args
            scratch = tmp_path / f'restore-{number}'
            (scratch / 'root').mkdir(parents=True)
            if state['source_tree'] is not None:
                shutil.copytree(state['source_tree'], scratch / 'root/source', symlinks=True)
            shutil.copytree(step['backup'], scratch / 'B', symlinks=True)
            body = ('restore() {\n' + script_lines('rollback_gene', "log 'rollback: source'",
                                                    "log 'rollback: old controller and Red'")
                    + '\n}\nrestore\nprintf "SOURCE<%s>" "$(source_state)"')
            returncode, out = run_script(tmp_path, ['log', 'die', 'jget', 'rename_tree', 'source_state'], body,
                                         {'ROOT': str(scratch / 'root'), 'B': str(scratch / 'B'),
                                          'BASELINE': batch_env(root, step['record'])['BASELINE'],
                                          'G': step['record']['gene']})
            failing_checks, source, _ = outcome
            assert (returncode == 0) == (not failing_checks), (number, name, outcome, out[-800:])
            if returncode == 0:
                assert out.endswith(f'SOURCE<{source}>'), (out, source)
        else:  # the settings the rollback's own writers leave, then verify old on that state
            step, state = args
            record = step['record']
            before = next(args for check, args, _ in reversed(checked[:number]) if check == 'pair_mirror')[2]
            kind = next(args for check, args, _ in reversed(checked[:number]) if check == 'pair_mirror')[0]
            settings = script_settings(batch_host, tmp_path / f'settings-{number}', kind, step, before)
            assert settings == state['settings'], (number, settings, state['settings'])
            env = put_state(batch_host, state | {'settings': settings}, tmp_path / 'health',
                            (record['old_gene'], record['old_fri'], step['baseline']))
            env |= batch_env(root, record)
            returncode, out = run_script(tmp_path, ['jget', 'source_state', 'state', 'verify'], 'verify old', env)
            assert (returncode == 0) == (not outcome), (number, name, outcome, out[-800:])


@pytest.mark.parametrize('case', ['source_crlf_live', 'source_space_saved', 'source_symlink_saved',
                                  'source_trailing_newlines_saved', 'quoted_pins', 'worker_space', 'journal_crlf',
                                  'unknown_downgrade'])
def test_recovery_reads_files_as_the_release_script_does(batch_host, monkeypatch, case):
    root = batch_host.root
    if case == 'journal_crlf':  # has() is grep -x: "resumed\r" is not resumed
        journal = stage_of(root, 4) / 'journal'
        at = journal.stat().st_mtime
        journal.write_bytes(journal.read_bytes().replace(b'\n', b'\r\n'))
        os.utime(journal, (at, at))
    elif case == 'unknown_downgrade':  # downgrade_step dies on a module it has no step for, mid-rollback
        edit_manifest(stage_of(root, 4), lambda m: m.update(downgrades='gene.not_a_step'))
    else:
        source_case(batch_host, case)
    monkeypatch.setattr(retention, 'command', removing(batch_host.images))
    if case == 'source_trailing_newlines_saved':
        assert [step['record'] for step in retention.run(root)['lineage']][:1] == ['batch ' + stage_of(root, 4).name]
        return
    with pytest.raises(ValueError):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-plan.json').exists()


def test_the_known_downgrades_are_the_release_scripts():
    import re
    text = SCRIPT.read_text()
    listed = set(re.search(r'^DOWNGRADE_MODULES=\(([^)]*)\)', text, re.M)[1].split())
    stepped = set(re.findall(r'^    (gene\.[a-z_]+)\)', script_function('downgrade_step'), re.M))
    assert retention.DOWNGRADE_STEPS == listed == stepped


# Sol round 5 on gene #80: persisted fields must have the types the script writes.

@pytest.mark.parametrize('case', ['previous_sha_number', 'gene_guild_number', 'fri_guild_number', 'presence_number',
                                  'fri_active_number', 'manifest_fri_number'])
def test_persisted_fields_need_the_types_the_script_writes(batch_host, monkeypatch, case):
    root = batch_host.root
    if case == 'previous_sha_number':  # jget prints it and settings_transition stores the text
        edit_baseline(stage_of(root, 3), lambda b: b['settings'].update(previous_sha=int('1' * 40)))
    elif case in ('gene_guild_number', 'fri_guild_number'):  # state() writes str(guild_id)
        edit_baseline(stage_of(root, 4), lambda b: b[case.split('_')[0]].update(guild_id=123))
    elif case == 'presence_number':  # both health files write str(status)
        edit_baseline(stage_of(root, 4), lambda b: b['gene'].update(presence=1))
    elif case == 'fri_active_number':  # a controller setting: text or absent
        edit_baseline(stage_of(root, 4), lambda b: b['settings'].update(fri_active_sha=5))
    else:  # the manifest's Fri SHA, which jget would print as digits
        edit_manifest(stage_of(root, 1), lambda m: m.update(fri=int('1' * 40)))
    monkeypatch.setattr(retention, 'command', removing(batch_host.images))
    with pytest.raises(ValueError):
        retention.run(root, apply=True)
    assert not (root / 'maintenance/image-retention-plan.json').exists()


@pytest.mark.parametrize('case', ['previous_sha', 'gene_guild', 'fri_guild'])
def test_the_mirror_and_the_script_both_refuse_numeric_baseline_values(batch_host, tmp_path, case):
    """Past the structural type checks, recovery itself fails on these values, in the mirror and in the script:
    settings_transition stores previous_sha as text, and state() compares str(guild_id)."""
    root = batch_host.root
    n, change = {'previous_sha': (3, lambda b: b['settings'].update(previous_sha=int('1' * 40))),
                 'gene_guild': (4, lambda b: b['gene'].update(guild_id=123)),
                 'fri_guild': (4, lambda b: b['fri'].update(guild_id=123))}[case]
    by_tag = {tag: image for image, row in batch_host.images.items() for tag in row['tags']}
    records = {r['name']: r for r in retention.batch_records(root)}
    settings, *_ = retention.controller_refs(root)
    state = retention.live_pair_state(root, settings)
    for level in (4, 3):
        step = retention.batch_step(root, records[stage_of(root, level).name], by_tag)  # before the change
        if level == n:
            edit_baseline(stage_of(root, n), change)
            step['baseline'] = json.loads((stage_of(root, n) / 'baseline.json').read_text())
            before = state
        why, state = retention.recover('gene', step, state, str(root))
        if level == n:
            break
        assert why is None
    assert why and 'verify old' in why
    # The script: its own settings writers, then verify old, on the state the rollback leaves.
    record = step['record']
    restored = dict(before, **{'release.env': step['saved']['release.env'], 'worker.env': step['saved']['worker.env'],
                              'fri release.env': step['saved']['fri-release.env'],
                              'fri release.json': step['saved']['fri-release.json'],
                              'controller': record['images']['old']['runtime'][1],
                              'red': record['images']['old']['runtime'][1], 'fri': record['images']['old']['fri'][1],
                              'source': step['baseline']['source'],
                              'source_tree': step['backup'] / 'installed-source'})
    settings = script_settings(batch_host, tmp_path / 'settings', 'gene', step, before)
    env = put_state(batch_host, restored | {'settings': settings}, tmp_path / 'health',
                    (record['old_gene'], record['old_fri'], step['baseline']))
    env |= batch_env(root, record)
    returncode, out = run_script(tmp_path, ['jget', 'source_state', 'state', 'verify'], 'verify old', env)
    assert returncode != 0, out[-800:]


# gene #71 and #92: a stop after a fence whose restore hasn't finished.

JOURNALS = {
    'live': LIVE,
    'aborted_restoring': LIVE + ['intent:abort'],  # rollback-fri found Gene moved, then its restore stopped
    'aborted_restored': LIVE + ['intent:abort', 'resumed_old'],
    'abort_again': ['baseline', 'intent:pause', 'drained', 'resumed_old', 'intent:abort'],
    'drain_aborting': ['baseline', 'staged', 'rehearsed', 'intent:pause', 'paused:7', 'intent:abort'],
    'owner_paused_first': ['baseline', 'staged', 'rehearsed', 'intent:pause', 'skipped:pause'],
    'crlf_abort': ['baseline', 'drained', 'intent:abort\r'],
}


@pytest.mark.parametrize('case', sorted(JOURNALS))
def test_aborting_and_touched_match_the_release_script(tmp_path, case):
    journal = tmp_path / 'journal'
    journal.write_bytes(b''.join(line.encode() + b'\n' for line in JOURNALS[case]))
    data = journal.read_bytes()
    lines = [line.decode('utf-8', 'surrogateescape') for line in data.split(b'\n')]
    record = {'marks': set(lines), 'lines': lines}
    _, out = run_script(tmp_path, ['has', 'aborting', 'touched'],
                        'if aborting; then echo aborting; fi; if touched; then echo touched; fi', {'JOURNAL': str(journal)})
    assert ('aborting' in out.split()) == retention.aborting(record)
    assert ('touched' in out.split()) == retention.touched(record)


def test_a_batch_stopped_after_its_fence_holds_retention_until_resume_old(batch_host):
    root = batch_host.root
    write_fri_only(root, batch_host.images, 4, 5, 4, NOW - 0.5 * DAY, marks=FRI_ONLY + ['intent:abort'])
    live_fri(batch_host, 5)
    with pytest.raises(retention.Busy, match='mid-release or mid-rollback'):
        retention.run(root, apply=True)
    stage = root / 'staging' / f'batch-{gene(4)}-{fri(5)}'
    set_marks(stage, FRI_ONLY + ['intent:abort', 'resumed_old'])  # resume-old finished it, as it is
    with pytest.raises(ValueError, match='Live Fri is not the release batch'):  # no longer in service
        retention.run(root, apply=True)

