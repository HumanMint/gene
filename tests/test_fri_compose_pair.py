"""Run the real stage admission check before it can record or build anything."""
import io
import os
import re
import subprocess
import tarfile
from pathlib import Path

import pytest


@pytest.mark.parametrize('allow_ops', ['0', '1'])
@pytest.mark.parametrize('fri_switches', [False, True])
@pytest.mark.parametrize('compose_changes', [False, True])
def test_fri_compose_change_requires_fri_switch(tmp_path, allow_ops, fri_switches, compose_changes):
    script = Path('ops/maintainer/batch-release.sh').read_text()
    stage = re.search(r'^stage\(\) \{.*?^\}', script, re.M | re.S).group()
    installed = tmp_path/'source/ops/fri-compose.yml'
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b'old compose\n')
    with tarfile.open(tmp_path/'gene.tar', 'w', format=tarfile.PAX_FORMAT,
                      pax_headers={'comment': 'g'*40}) as archive:
        content = b'new compose\n' if compose_changes else installed.read_bytes()
        member = tarfile.TarInfo('ops/fri-compose.yml')
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    (tmp_path/'gene.tar.sha256').touch()
    harness = r'''
set -eu
ROOT=$CASE_ROOT
STAGE=$CASE_ROOT
G=gggggggggggggggggggggggggggggggggggggggg
F=old
OLD_F=old
[[ $FRI_SWITCHES == 0 ]] || F=new
need() { :; }
check_manifest() { :; }
has() { return 1; }
gene_changes() { return 0; }
fri_changes() { [[ $F != "$OLD_F" ]]; }
die() { echo "$*" >&2; exit 1; }
sha256sum() { :; }
# Reaching the first manifest write means admission succeeded; don't run later phases.
mset() { touch "$ROOT/admitted"; exit 0; }
'''
    result = subprocess.run(['bash', '-c', harness+stage+'\nstage'], capture_output=True,
                            text=True, env={**os.environ, 'CASE_ROOT': str(tmp_path),
                                            'FRI_SWITCHES': str(int(fri_switches)),
                                            'ALLOW_OPS_DIFF': allow_ops})
    if compose_changes and not fri_switches:
        assert result.returncode != 0
        assert 'Fri Compose change must ship with a Fri release' in result.stderr
        assert not (tmp_path/'admitted').exists()
    else:
        assert result.returncode == 0, result.stderr
        assert (tmp_path/'admitted').exists()
