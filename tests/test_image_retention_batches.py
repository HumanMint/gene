"""Image retention against batch-release.sh's records (gene #76): synthetic paths, no Docker."""
import copy
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest

from scripts import gene_image_retention as retention
from tests.test_image_retention import NOW, iid

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
        (folder / 'release.env').write_text(f'GENE_RELEASE_SHA={gene(old)}\nGENE_ROOT=/srv/gene\n'
                                            f'GENE_RUNTIME_IMAGE=gene-runtime:{gene(old)}\n')
        (folder / 'worker.env').write_text(f'GENE_WORKER_IMAGE=gene-worker:{gene(old)}\nGENE_WORKER_TOKEN=private\n')
        (folder / 'fri-release.env').write_text(f'FRI_RELEASE_SHA={fri(old)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(old)}\n')
        (folder / 'fri-release.json').write_text(json.dumps({'sha': fri(old)}))
        for name in ('db/controller.sqlite3', 'red.tar.gz'):
            (folder / name).touch()
        manifest['backup'] = str(folder)
    (root / 'releases' / gene(old) / 'cogs').mkdir(parents=True, exist_ok=True)
    (stage / 'manifest.json').write_text(json.dumps(manifest, indent=1))
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
    (root / 'release.env').write_text(f'GENE_RELEASE_SHA={gene(4)}\nGENE_ROOT=/srv/gene\n'
                                      f'GENE_RUNTIME_IMAGE=gene-runtime:{gene(4)}\n')
    (root / 'secrets/worker.env').write_text(f'GENE_WORKER_IMAGE=gene-worker:{gene(4)}\nGENE_WORKER_TOKEN=private\n')
    (root / 'audio-release.env').write_text(f'GENE_AUDIO_SHA={AUDIO}\n')
    (fri_root / 'release.env').write_text(f'FRI_RELEASE_SHA={fri(4)}\nFRI_RUNTIME_IMAGE=fri-runtime:{fri(4)}\n')
    images = {}
    for n in range(5):
        for tag, image in release_images(n).values():
            images[image] = {'tags': [tag]}
    images[iid(201)] = {'tags': ['gene-audio:' + AUDIO]}
    images[iid(202)] = {'tags': ['gene-audio-resolver:' + AUDIO]}
    images[iid(203)] = {'tags': ['wordpress:latest']}
    images[iid(300)] = {'tags': []}  # an unnamed object nothing recorded
    for n, old in [(1, 0), (2, 1), (3, 2), (4, 3)]:
        write_batch(root, n, old, NOW - (5 - n) * DAY, legacy_format=n < 3)
    db = sqlite3.connect(root / 'data/controller/gene.sqlite3')
    db.executescript('CREATE TABLE settings (key,value); CREATE TABLE jobs(id,kind,state,proposal,approved_sha);'
                     'CREATE TABLE deployments(job_id,sha,previous_sha,at,state);')
    db.executemany('INSERT INTO settings VALUES (?,?)', [('active_sha', gene(4)), ('previous_sha', gene(3))])
    db.commit()
    policy = {'version': 1, 'keep_images': [], 'retired_images': {}, 'apply_enabled': True}
    retention.save_json(root / 'maintenance/image-retention-policy.json', policy)
    containers = {'controller': iid(RUNTIME + 4), 'red': iid(RUNTIME + 4), 'fri': iid(FRI + 4)}
    # Classic builder: each worker is a child of its runtime image.
    metadata = {image: {'id': image, 'parent': '', 'tags': row['tags']} for image, row in images.items()}
    for n in range(5):
        metadata[iid(WORKER + n)]['parent'] = iid(RUNTIME + n)
    monkeypatch.setattr(retention, 'docker_inventory', lambda: (copy.deepcopy(images), dict(containers)))
    monkeypatch.setattr(retention, 'process_refs', lambda: [])
    monkeypatch.setattr(retention, 'image_metadata', lambda ref: copy.deepcopy(metadata[ref]))
    monkeypatch.setattr(retention, 'lock_held', lambda path: False, raising=False)
    monkeypatch.setattr(retention, 'FRI_ROOT', fri_root, raising=False)
    monkeypatch.setattr(retention.time, 'time', lambda: NOW)
    return SimpleNamespace(root=root, fri_root=fri_root, images=images, policy=policy, db=db,
                           containers=containers, metadata=metadata)


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

