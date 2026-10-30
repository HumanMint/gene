"""The installed Gene runtime must not depend on the retired Fri test client."""

import os
import subprocess
import sys


def test_controller_and_red_import_without_the_legacy_fri_client(tmp_path):
    # -I and an unrelated cwd exclude repository tests and PYTHONPATH. This also
    # runs against an installed wheel when its interpreter is used for pytest.
    code = '''
import importlib.util
import sys
from pathlib import Path

for name in ('fri', 'fri_agent', 'fri_chat', 'fri_actions', 'fri_memes', 'fri_eval'):
    assert importlib.util.find_spec('gene.' + name) is None, name

import gene.api
import gene.deploy
import gene.fri_release
import gene.runtime
import gene.worker
from fastapi.testclient import TestClient
from gene.config import Settings
from gene.fri_protocol import contract

settings = Settings(Path(sys.argv[1]), '123', '10', 'r'*32, 'w'*32, 'd'*32,
                    fri_token='f'*32)
with TestClient(gene.api.create_app(settings)) as client:
    assert client.get('/health').status_code == 200
    assert client.get('/fri/contract', headers={'Authorization': 'Bearer ' + 'f'*32}).json() == contract()
assert not any(name.startswith('tests.legacy_fri') for name in sys.modules)
'''
    result = subprocess.run(
        [sys.executable, '-I', '-c', code, str(tmp_path / 'controller.sqlite3')],
        cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env={'PATH': os.defpath, 'HOME': str(tmp_path),
             'XDG_CONFIG_HOME': str(tmp_path / 'config'), 'PYTHONDONTWRITEBYTECODE': '1'},
    )
    assert result.returncode == 0, result.stdout + result.stderr
