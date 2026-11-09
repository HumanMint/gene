import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('retention', Path(__file__).parents[1]/'ops/maintainer/retain-backups.py')
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


@pytest.fixture
def backups(tmp_path):
    import os

    root, fri = tmp_path/'gene', tmp_path/'fri'
    now = 2000000000
    for directory in (root, fri):
        (directory/'backups').mkdir(parents=True)
        for i in range(15):
            backup = directory/'backups'/str(i)
            backup.mkdir()
            (backup/'data').write_text('synthetic')
            os.utime(backup, (now-30*86400+i, now-30*86400+i))
        os.utime(directory/'backups/0', (now, now))  # Old name, recent backup stays.
    (root/'release.env').write_text('GENE_RELEASE_SHA='+'a'*40)
    (fri/'release.env').write_text('FRI_RELEASE_SHA='+'b'*40)
    stage = root/'staging/batch-live'
    stage.mkdir(parents=True)
    manifest = {'gene': 'a'*40, 'fri': 'b'*40, 'old_gene': 'c'*40, 'old_fri': 'd'*40,
                'backup': str(root/'backups/1')}
    (stage/'manifest.json').write_text(json.dumps(manifest))
    (stage/'journal').write_text('resumed\n')
    previous = root/'staging/batch-previous'
    previous.mkdir()
    (previous/'manifest.json').write_text(json.dumps(dict(manifest, gene='c'*40, fri='d'*40,
                                                         backup=str(root/'backups/2'))))
    (previous/'journal').write_text('resumed\n')
    (fri/'release.json').write_text(json.dumps({'backup': str(fri/'backups/1')}))
    (fri/'backups/1/before.json').write_text(json.dumps({'backup': str(fri/'backups/2')}))
    (root/'maintainer-live-result.json').write_text(json.dumps({
        'sha': 'a'*40, 'fri_sha': 'b'*40, 'backup': str(root/'backups/1')}))
    return root, fri, now


def test_retention_keeps_count_age_and_rollback_and_refuses_bad_metadata(backups, monkeypatch, capsys):
    root, fri, now = backups
    stage = root/'staging/batch-live'
    expected = [root/'backups/3', root/'backups/4', root/'backups/5',
                fri/'backups/3', fri/'backups/4', fri/'backups/5']
    assert retention.plan(root, fri, now) == sorted(expected)
    assert all(p.exists() for p in expected)  # Planning never removes anything.
    saved_manifest = (stage/'manifest.json').read_text()
    (stage/'manifest.json').write_text('{bad')
    with pytest.raises(ValueError):
        retention.plan(root, fri, now)
    assert all(p.exists() for p in expected)

    (stage/'manifest.json').write_text(saved_manifest)
    monkeypatch.setattr(retention.time, 'time', lambda: now)
    monkeypatch.setattr(retention.sys, 'argv', ['retain', str(root), str(fri), '--dry-run'])
    retention.main()
    dry = capsys.readouterr().out
    assert dry.splitlines() == [str(p) for p in sorted(expected)]
    assert all(p.exists() for p in expected)
    monkeypatch.setattr(retention.sys, 'argv', ['retain', str(root), str(fri), '--apply'])
    retention.main()
    assert capsys.readouterr().out == dry
    assert all(not p.exists() for p in expected)
    assert (root/'backups/1').is_dir() and (root/'backups/2').is_dir()


@pytest.mark.parametrize('which', ['gene', 'fri'])
def test_symlinked_backup_root_refuses_apply(backups, tmp_path, monkeypatch, which):
    root, fri, now = backups
    directory = (root if which == 'gene' else fri)/'backups'
    external = tmp_path/'external'
    directory.rename(external)
    directory.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(retention.time, 'time', lambda: now)
    monkeypatch.setattr(retention.sys, 'argv', ['retain', str(root), str(fri), '--apply'])
    with pytest.raises(ValueError):
        retention.main()
    assert (external/'3/data').read_text() == 'synthetic'
    assert (root/'backups/3').is_dir() and (fri/'backups/3').is_dir()


@pytest.mark.parametrize('metadata', ['missing', 'no backup', 'empty backup', 'outside backup'])
def test_incomplete_previous_fri_metadata_refuses_whole_apply(backups, tmp_path, monkeypatch, metadata):
    root, fri, now = backups
    before = fri/'backups/1/before.json'
    if metadata == 'missing':
        before.unlink()
    else:
        value = {'no backup': {}, 'empty backup': {'backup': ''},
                 'outside backup': {'backup': str(tmp_path/'outside')}}[metadata]
        before.write_text(json.dumps(value))
    monkeypatch.setattr(retention.time, 'time', lambda: now)
    monkeypatch.setattr(retention.sys, 'argv', ['retain', str(root), str(fri), '--apply'])
    with pytest.raises((ValueError, OSError)):
        retention.main()
    assert (root/'backups/3/data').read_text() == 'synthetic'
    assert (fri/'backups/3/data').read_text() == 'synthetic'


def test_null_previous_fri_record_is_valid(backups):
    root, fri, now = backups
    (fri/'backups/1/before.json').write_text('null')
    assert fri/'backups/2' in retention.plan(root, fri, now)


@pytest.mark.parametrize('which', ['root', 'entry'])
def test_apply_rechecks_paths_after_planning(backups, tmp_path, monkeypatch, which):
    root, fri, now = backups
    original = retention.plan
    external = tmp_path/'external'

    def changed(*args):
        candidates = original(*args)
        path = root/'backups' if which == 'root' else root/'backups/3'
        path.rename(external)
        path.symlink_to(external, target_is_directory=True)
        return candidates

    monkeypatch.setattr(retention, 'plan', changed)
    monkeypatch.setattr(retention.time, 'time', lambda: now)
    monkeypatch.setattr(retention.sys, 'argv', ['retain', str(root), str(fri), '--apply'])
    with pytest.raises(ValueError):
        retention.main()
    assert (root/'backups/3/data').read_text() == 'synthetic'
    assert (fri/'backups/3/data').read_text() == 'synthetic'
