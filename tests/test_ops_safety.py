"""Portable checks of the release script's trust checks; full mocks run on Linux."""
import io
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1]/'ops/maintainer/batch-release.sh'


@pytest.mark.parametrize('commit', ['a'*40, 'b'*40, None])
def test_embedded_archive_identity(tmp_path, commit):
    body = SCRIPT.read_text().split("<<'PYARCHIVE'\n")[1].split('\nPYARCHIVE')[0]
    archive = tmp_path/'source.tar'
    with tarfile.open(archive, 'w', format=tarfile.PAX_FORMAT,
                      pax_headers={'comment': commit} if commit else {}) as tar:
        item = tarfile.TarInfo('synthetic')
        item.size = 1
        tar.addfile(item, io.BytesIO(b'x'))
    result = subprocess.run(['python3', '-c', body, str(archive), 'a'*40], capture_output=True)
    assert (result.returncode == 0) == (commit == 'a'*40)


def test_masked_services_never_start_on_resume():
    text = SCRIPT.read_text()
    functions = text[text.index('development_masked()'):text.index('write_result()')]
    # Exercise the masked branch without any service, Docker or database access.
    harness = '''
log() { :; }
die() { exit 90; }
systemctl() {
  case $1 in
    is-enabled) echo masked; return 1 ;;
    is-active) return 3 ;;
    *) echo unexpected-start; exit 91 ;;
  esac
}
'''
    result = subprocess.run(['bash', '-c', harness+functions+'\nresume_services\ndevelopment_ready'],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'unexpected-start' not in result.stdout
