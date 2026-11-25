"""Exercise the real shell recovery function with a simulated occupied call port."""
import os
import re
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize('problem', ['ok', 'missing', 'read_error', 'removed_after_stop'])
@pytest.mark.parametrize('conflict', [False, True])
def test_shell_old_fri_uses_previous_compose(tmp_path, conflict, problem):
    script = Path('ops/maintainer/batch-release.sh').read_text()
    functions = '\n'.join(re.findall(
        r'^(?:old_fri_compose_file|restore_old_fri)\(\) \{.*?^\}', script, re.M | re.S))
    current = tmp_path/'source/ops/fri-compose.yml'
    old = tmp_path/'backup/installed-source/ops/fri-compose.yml'
    for file in (current, old):
        file.parent.mkdir(parents=True)
    current.write_text('ports: ["127.0.0.1:18790:18790"]\n')
    old.write_text('services: {}\n')
    if problem == 'missing':
        old.unlink()
    harness = r'''
set -eu
ROOT=$CASE_ROOT
FRI_ROOT=$CASE_ROOT
FRI=fri-runtime-1
BASELINE=unused
STAGE=$CASE_ROOT
umask 077
cat() {
  if [[ $READ_ERROR == 1 && $1 == "$ROOT/backup/installed-source/ops/fri-compose.yml" ]]; then
    echo "injected read error" >&2; return 1
  fi
  command cat "$@"
}
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
    if [[ $arg == stop ]]; then
      touch "$ROOT/stopped"
      if [[ $REMOVE_AFTER_STOP == 1 ]]; then rm "$ROOT/backup/installed-source/ops/fri-compose.yml"; fi
    fi
    if [[ $arg == up ]]; then
      cat "$file" > /dev/null || return 1
      if [[ $CONFLICT == 1 ]] && grep -q 18790 "$file"; then
        echo 'bind: address already in use' >&2; return 1
      fi
      echo "$file" > "$ROOT/used-compose"
      touch "$ROOT/running"
    fi
  done
}
'''
    result = subprocess.run(['bash', '-c', harness+functions+'\nrestore_old_fri'],
                            env={**os.environ, 'CASE_ROOT': str(tmp_path), 'CONFLICT': str(int(conflict)),
                                 'READ_ERROR': str(int(problem == 'read_error')),
                                 'REMOVE_AFTER_STOP': str(int(problem == 'removed_after_stop'))},
                            capture_output=True, text=True)
    if problem in {'missing', 'read_error'}:
        assert result.returncode != 0
        assert 'restore installed-source/ops/fri-compose.yml' in result.stderr
        assert not (tmp_path/'stopped').exists()
        return
    assert result.returncode == 0, result.stderr
    assert (tmp_path/'running').exists()
    used = Path((tmp_path/'used-compose').read_text().strip())
    assert used not in {old, current}
    assert used.stat().st_mode & 0o777 == 0o600
    assert used.read_bytes() == b'services: {}\n'
