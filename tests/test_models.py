import pytest

from gene.models import discover_models
from gene.store import Conflict
from tests.conftest import request
from tests.test_api import actor, headers


def test_selected_model_is_snapshotted_per_job(store):
    request(store)
    store.select_model('gpt-6-astra', 'high', 'owner')
    job = store.claim()
    store.select_model('gpt-6-astra', 'medium', 'owner')
    assert job['model'] == 'gpt-6-astra'
    assert store.get(job['id'])['effort'] == 'high'


def test_refresh_updates_choices_without_switching_model(store):
    store.request_model_refresh('owner')
    assert store.model_state()['refresh_pending']
    store.update_models([{'id': 'new-model', 'name': 'New model',
                          'efforts': ['high'], 'default_effort': 'high'}])
    state = store.model_state()
    assert not state['refresh_pending']
    assert state['selected'] == 'gpt-6-astra'
    assert not state['selected_available']
    request(store)
    assert store.claim() is None
    store.select_model('new-model', '', 'owner')
    assert store.claim()['model'] == 'new-model'


def test_invalid_model_and_effort_are_rejected(store):
    with pytest.raises(Conflict):
        store.select_model('made-up-model', '', 'owner')
    with pytest.raises(ValueError):
        store.select_model('gpt-6-astra', 'impossible', 'owner')


def test_catalog_failure_retains_existing_choices(store):
    store.update_models([], error='Connection unavailable')
    assert store.model_state()['selected_available']
    assert store.model_state()['error'] == 'Connection unavailable'


def test_only_owner_can_change_or_refresh_models(client):
    for route, extra in [('/models/select', {'model_id': 'gpt-6-astra'}), ('/models/refresh', {})]:
        assert client.post(route, json={**actor('member'), **extra},
                           headers=headers('fri')).status_code == 403
        assert client.post(route, json={**actor(), **extra},
                           headers=headers('worker')).status_code == 401


def test_model_discovery_handles_pagination_and_batched_notifications(tmp_path):
    import sys
    fake = tmp_path / 'catalog.py'
    fake.write_text('''
import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if request['method'] == 'initialize':
        result = {}
    elif request['method'] == 'model/list':
        second = bool(request['params'].get('cursor'))
        result = {'data': [{'id': 'new' if second else 'old', 'displayName': 'Example',
                  'defaultReasoningEffort': 'high',
                  'supportedReasoningEfforts': [{'reasoningEffort': 'high'}]}],
                  'nextCursor': None if second else 'page-two'}
    else:
        continue
    print(json.dumps({'method': 'notification'}) + '\\n' + json.dumps({'id': request['id'], 'result': result}), flush=True)
''')
    models = discover_models([sys.executable, '-u', str(fake)])
    assert [m['id'] for m in models] == ['old', 'new']
    assert models[0]['efforts'] == ['high']
