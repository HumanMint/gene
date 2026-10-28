import datetime as dt
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.reviews import request_reference
from gene.store import Store

gene_test_config = TemporaryDirectory(prefix='gene-test-config-')
os.environ['XDG_CONFIG_HOME'] = gene_test_config.name
# Tomorrow's window, not a fixed date: gene #65 allows one deployment per local day, counted from rows the store
# dates with the real clock. Real rows always fall on an earlier day than the window, so a test's own deployments
# never use up that day (a fixed date broke every test with two deployments once it arrived).
def inside_window():
    """Tomorrow's 04:05, worked out on every call, so a run that crosses midnight still dates its rows the day before."""
    zone = ZoneInfo('America/Los_Angeles')
    return dt.datetime.combine(dt.datetime.now(zone).date()+dt.timedelta(days=1), dt.time(4, 5), zone).timestamp()


@pytest.fixture(autouse=True)
def inside_maintenance_window(monkeypatch):
    """Deployments start only in the production window; tests not about the window run inside it."""
    monkeypatch.setattr('gene.maintenance.clock', inside_window)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'gene.sqlite3')
    result.bootstrap('a' * 40)
    result.update_models([{'id': 'gpt-6-astra', 'name': 'GPT-6 Astra',
                           'efforts': ['medium', 'high'], 'default_effort': 'medium'}])
    result.select_model('gpt-6-astra', 'medium', 'owner')
    result.set_paused(False, 'owner')
    return result


@pytest.fixture
def client(tmp_path):
    settings = Settings(Path(tmp_path / 'api.sqlite3'), 'guild', 'owner',
                        'runtime-' + 'x' * 32, 'worker-' + 'x' * 32, 'deploy-' + 'x' * 32,
                        workshop_channel_id='workshop', fri_token='fri-' + 'x' * 32)
    return TestClient(create_app(settings))


def request(store, user='member', source='message-1'):
    job = store.request('guild', user, source, 'https://discord.com/channels/1/2/3',
                        'Add a useful feature for our server')
    if job['state'] == 'awaiting_owner':
        store.review_decision(job['id'], 'intake', request_reference(job), 'approve', 'owner')
    return store.get(job['id'])


def proposed(store, sha='b' * 40, source='message-1'):
    request(store, source=source)
    job = store.claim()
    store.finish_build(job['id'], job['lease'], {
        'sha': sha, 'base_sha': job['base_sha'], 'branch': 'codex/test',
        'tests_passed': True, 'summary': 'A tested change'})
    return store.get(job['id'])
