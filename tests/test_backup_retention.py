import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('retention', Path(__file__).parents[1]/'ops/maintainer/retain-backups.py')
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


def test_retention_keeps_count_age_and_rollback_and_refuses_bad_metadata(tmp_path, monkeypatch, capsys):
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
