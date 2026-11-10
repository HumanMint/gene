"""Both release entrypoints use one protected, optional calling overlay."""

import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from gene.fri_release import FriRelease


def launcher(tmp_path):
    release = FriRelease.__new__(FriRelease)
    release.root = tmp_path / 'fri'
    release.root.mkdir()
    release.env = release.root / 'release.env'
    release.compose_file = tmp_path / 'base.yml'
    release.compose_file.touch()
    release.run = Mock(return_value='ok')
    return release


def root_owned(monkeypatch, path, uid=0, mode=None):
    original = Path.lstat

    def lstat(candidate, *args, **kwargs):
        value = original(candidate, *args, **kwargs)
        if candidate == path:
            fields = list(value)
            fields[4] = uid
            if mode is not None:
                fields[0] = (fields[0] & ~0o7777) | mode
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, 'lstat', lstat)


def test_no_overlay_keeps_the_exact_command(tmp_path):
    release = launcher(tmp_path)
    (release.root / 'other.yml').write_text('not an accepted overlay')
    assert release.compose('stop', 'runtime') == 'ok'
    release.run.assert_called_once_with([
        'docker', 'compose', '--project-name', 'fri', '--env-file', str(release.env),
        '-f', str(release.compose_file), 'stop', 'runtime'])


@pytest.mark.parametrize('mode', [0o600, 0o644])
def test_protected_overlay_is_added_after_base(tmp_path, monkeypatch, mode):
    release = launcher(tmp_path)
    overlay = release.root / 'compose.calling.yml'
    overlay.write_text('services: {}')
    overlay.chmod(mode)
    root_owned(monkeypatch, overlay)
    release.compose('up', '-d', '--no-deps', '--force-recreate', 'runtime')
    args = release.run.call_args.args[0]
    assert args[args.index('-f'):] == [
        '-f', str(release.compose_file), '-f', str(overlay),
        'up', '-d', '--no-deps', '--force-recreate', 'runtime']


@pytest.mark.parametrize('damage', ['directory', 'symlink', 'dangling', 'owner', 'writable', 'readonly', 'special'])
def test_invalid_overlay_refuses_before_docker(tmp_path, monkeypatch, damage):
    release = launcher(tmp_path)
    overlay = release.root / 'compose.calling.yml'
    if damage == 'directory':
        overlay.mkdir()
    elif damage in ('symlink', 'dangling'):
        target = tmp_path / 'target'
        if damage == 'symlink':
            target.touch()
        overlay.symlink_to(target)
    else:
        overlay.write_text('services: {}')
        overlay.chmod({'writable': 0o666, 'readonly': 0o400, 'special': 0o4600}.get(damage, 0o600))
    root_owned(monkeypatch, overlay, uid=1 if damage == 'owner' else 0,
               mode=0o4600 if damage == 'special' else None)
    with pytest.raises((ValueError, RuntimeError), match='overlay'):
        release.compose('up', '-d', 'runtime')
    release.run.assert_not_called()


def test_inspection_error_is_not_treated_as_absence(tmp_path, monkeypatch):
    release = launcher(tmp_path)

    def denied(*args, **kwargs):
        raise PermissionError('synthetic denial')

    monkeypatch.setattr(Path, 'lstat', denied)
    with pytest.raises((OSError, ValueError, RuntimeError)):
        release.compose('stop', 'runtime')
    release.run.assert_not_called()


def test_overlay_is_rechecked_for_each_launch(tmp_path, monkeypatch):
    release = launcher(tmp_path)
    overlay = release.root / 'compose.calling.yml'
    overlay.write_text('services: {}')
    overlay.chmod(0o600)
    root_owned(monkeypatch, overlay)
    release.compose('stop', 'runtime')
    overlay.chmod(0o666)
    with pytest.raises((ValueError, RuntimeError), match='overlay'):
        release.compose('up', '-d', 'runtime')
    assert release.run.call_count == 1


def test_failed_release_keeps_overlay_on_every_rollback_command(tmp_path, monkeypatch):
    from tests.test_fri_split import installation

    release, _, calls = installation(tmp_path)
    overlay = release.root / 'compose.calling.yml'
    overlay.write_text('services: {}')
    overlay.chmod(0o600)
    root_owned(monkeypatch, overlay)
    release.healthy = lambda sha, *args: sha == 'b' * 40
    with pytest.raises(RuntimeError, match='health'):
        release.deploy('c' * 40)
    commands = [args for args in calls if args[:2] == ['docker', 'compose']]
    assert len(commands) == 4
    assert all(args[args.index('-f') + 2:][:2] == ['-f', str(overlay)] for args in commands)
    assert overlay.is_file()


@pytest.mark.parametrize('kind', ['absent', 'symlink', 'directory', 'writable', 'valid'])
def test_batch_compose_function_checks_overlay_before_docker(tmp_path, kind):
    if kind == 'valid' and os.geteuid() != 0:
        pytest.skip('real root ownership is exercised by the Linux release mocks')
    release = launcher(tmp_path)
    overlay = release.root / 'compose.calling.yml'
    if kind == 'directory':
        overlay.mkdir()
    elif kind == 'symlink':
        overlay.symlink_to(tmp_path / 'missing')
    elif kind != 'absent':
        overlay.write_text('services: {}')
        overlay.chmod(0o600 if kind == 'valid' else 0o666)
    script = Path('ops/maintainer/batch-release.sh').read_text()
    function = script[script.index('fri_compose() {'):].split('\n}\n', 1)[0] + '\n}\n'
    capture = tmp_path / 'docker-args'
    # Only the real function is executed; no release, container, or network starts.
    wrapper = '''set -euo pipefail
die() { echo "$*" >&2; exit 82; }
backup_dir() { printf '%s' "$ROOT"; }
docker() { printf '%s\\n' "$@" > "$CAPTURE"; }
'''
    (tmp_path / 'source/ops').mkdir(parents=True)
    (tmp_path / 'source/ops/fri-compose.yml').touch()
    result = subprocess.run(['bash', '-c', wrapper + function + '\nfri_compose up -d runtime'],
                            env={'PATH': os.environ['PATH'], 'ROOT': str(tmp_path),
                                 'FRI_ROOT': str(release.root), 'CAPTURE': str(capture)},
                            capture_output=True, text=True)
    if kind in ('absent', 'valid'):
        assert result.returncode == 0, result.stderr
        args = capture.read_text().splitlines()
        expected = ['compose', '--project-name', 'fri', '--env-file', str(release.env),
                    '-f', str(tmp_path / 'source/ops/fri-compose.yml')]
        if kind == 'valid':
            expected += ['-f', str(overlay)]
        assert args == expected + ['up', '-d', 'runtime']
    else:
        assert result.returncode != 0
        assert not capture.exists()
