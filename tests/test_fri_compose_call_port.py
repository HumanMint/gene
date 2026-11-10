"""The fixed calling port must remain loopback-only in the shared base Compose."""

import json
import re
from pathlib import Path


def test_fri_call_port_is_fixed_loopback_with_shutdown_grace():
    compose = Path('ops/fri-compose.yml').read_text()
    runtime = compose.split('  runtime:\n', 1)[1].split('\nnetworks:', 1)[0]
    ports = re.findall(r'^    ports: (.+)$', runtime, re.MULTILINE)
    assert len(ports) == 1
    assert json.loads(ports[0]) == ['127.0.0.1:18790:18790']
    assert re.findall(r'^    stop_grace_period: (.+)$', runtime, re.MULTILINE) == ['30s']
