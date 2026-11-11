"""Exercise the real shell recovery function with a simulated occupied call port."""
import os
import re
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('missing', [False, True])
@pytest.mark.parametrize('conflict', [False, True])
def test_shell_old_fri_uses_previous_compose(tmp_path, conflict, missing):
    script = Path('ops/maintainer/batch-release.sh').read_text()
    functions = '\n'.join(re.findall(
        r'^(?:old_fri_compose_file|restore_old_fri)\(\) \{.*?^\}', script, re.M | re.S))
    current = tmp_path/'source/ops/fri-compose.yml'
    old = tmp_path/'backup/installed-source/ops/fri-compose.yml'
    for file in (current, old):
        file.parent.mkdir(parents=True)
    current.write_text('ports: ["127.0.0.1:18790:18790"]\n')
    old.write_text('services: {}\n')
    if missing:
        old.unlink()
    harness = r'''
set -eu
ROOT=$CASE_ROOT
FRI_ROOT=$CASE_ROOT
FRI=fri-runtime-1
BASELINE=unused
OLD_F=old
backup_dir() { echo "$ROOT/backup"; }
gene_changes() { return 0; }
source_state() { echo new; }
jget() { echo old; }
die() { echo "$*" >&2; exit 1; }
log() { :; }
mark() { :; }
restore_file() { :; }
set_fri_active() { :; }
container_running() { return 1; }
old_fri_pinned() { test -f "$ROOT/running"; }
fri_compose() { docker compose -f "$ROOT/source/ops/fri-compose.yml" "$@"; }
docker() {
  local file= arg
  while (( $# )); do
    arg=$1; shift
    if [[ $arg == -f ]]; then file=$1; shift; fi
    if [[ $arg == stop ]]; then touch "$ROOT/stopped"; fi
    if [[ $arg == up ]]; then
      if [[ $CONFLICT == 1 ]] && grep -q 18790 "$file"; then
        echo 'bind: address already in use' >&2; return 1
      fi
      touch "$ROOT/running"
    fi
  done
}
'''
    result = subprocess.run(['bash', '-c', harness+functions+'\nrestore_old_fri'],
                            env={**os.environ, 'CASE_ROOT': str(tmp_path), 'CONFLICT': str(int(conflict))},
                            capture_output=True, text=True)
    if missing:
        assert result.returncode != 0
        assert 'restore installed-source/ops/fri-compose.yml' in result.stderr
        assert not (tmp_path/'stopped').exists()
        return
    assert result.returncode == 0, result.stderr
    assert (tmp_path/'running').exists()
