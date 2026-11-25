"""Choosing Fri's model provider (Claude-native groundwork): owner-only enum settings, Claude ids beside the plan's
catalog, usage receipts with provider and cost in a side table an older Gene ignores, and the monthly spend guard."""

import io
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_protocol import contract
from gene.store import CLAUDE_MODELS, CLAUDE_SOFT_LIMIT, Conflict, Store
from tests.conftest import request
from tests.test_api import actor, headers
from tests.test_fri_agent import setup_agent  # noqa: F401
from tests.test_fri_mention_api import h, invited, post  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
BEFORE = '2827e85'  # Gene main before the provider settings: what a rollback goes back to.


def receipt(attempt, **extra):
    return {'attempt_id': attempt*32, 'model': 'claude-opus-5-5', 'effort': 'high', 'status': 'completed',
            'counters': {'input_tokens': 10, 'output_tokens': 2}, **extra}


def guard_rows(store):
    with store.connection() as db:
        return db.execute("SELECT count(*) FROM audit WHERE action='claude_spend_guard'").fetchone()[0]


def test_the_capability_is_advertised_and_optional():
    assert 'model-provider' in contract()['capabilities']


def test_provider_settings_default_to_the_plan_on_every_install(store):
    status = store.status()
    assert (status['provider'], status['model_provider'], status['claude_chat_model']) == ('chatgpt', 'chatgpt', '')
    assert status['claude_spend_guard'] is False


def test_provider_settings_are_owner_only_enums(setup_agent):  # noqa: F811
    store, settings, agent = setup_agent
    client = TestClient(create_app(settings))
    version = agent.state()['version']
    change = {'version': version, 'changes': {'model_provider': 'claude'}}
    assert client.post('/agent/settings', headers=headers('fri'), json={**actor('member'), **change}).status_code == 403
    assert store.status()['model_provider'] == 'chatgpt'
    for changes in ({'model_provider': 'openai'}, {'model_provider': True}, {'model_provider': ['claude']},
                    {'claude_chat_model': 'claude-haiku-5-5'}, {'claude_chat_model': 'gpt-6.2-sol'},
                    {'claude_chat_model': None}):
        with pytest.raises(ValueError):
            agent.update(changes, version, 'owner')
    state = client.post('/agent/settings', headers=headers('fri'), json={
        **actor(), 'version': version, 'changes': {'model_provider': 'claude', 'claude_chat_model': 'claude-sonnet-5-5'}
    }).json()
    assert (state['provider'], state['model_provider'], state['claude_chat_model']) == (
        'claude', 'claude', 'claude-sonnet-5-5')
    # Beside config, not in it: Fri's owner-settings model reads config whole.
    assert 'model_provider' not in state['config'] and 'claude_chat_model' not in state['config']
    assert store.status()['provider'] == 'claude'
    agent.update({'claude_chat_model': ''}, version+1, 'owner')
    assert store.status()['claude_chat_model'] == ''


def test_claude_ids_stay_out_of_plan_settings_and_the_plan_catalog(setup_agent):  # noqa: F811
    # An older Fri or a rolled-back Gene sends chat_model and role models to the plan, so they stay plan ids.
    store, _, agent = setup_agent
    catalog = store.chat_models()
    assert catalog['claude_models'] == list(CLAUDE_MODELS) and catalog['provider'] == 'chatgpt'
    before = store.status()['chat_model']
    with pytest.raises(Conflict):
        store.select_chat_model('claude-opus-5-5', 'high', 'owner')
    with pytest.raises(ValueError):
        agent.update({'attention_model': 'claude-haiku-5-5'}, agent.state()['version'], 'owner')
    store.update_chat_models(['gpt-6-luna', 'gpt-6.3-sol', 'claude-opus-5-5'])
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM chat_models WHERE id LIKE 'claude-%'").fetchone()
    assert store.status()['chat_model'] == before
    store.select_chat_model('gpt-6.3-sol', 'high', 'owner')
    assert store.status()['chat_model'] == 'gpt-6.3-sol'
    with pytest.raises(ValueError):  # A Claude-only refresh keeps the previous plan list.
        store.update_chat_models(['claude-opus-5-5'])
    assert 'gpt-6.3-sol' in store.chat_models()['models']
    with store.connection() as db:  # A catalog an older Gene saved can still list a Claude id.
        db.execute("INSERT INTO chat_models VALUES ('claude-opus-5-5')")
    with pytest.raises(Conflict):
        store.select_chat_model('claude-opus-5-5', 'high', 'owner')
    with pytest.raises(ValueError):
        agent.update({'attention_model': 'claude-opus-5-5'}, agent.state()['version'], 'owner')


def test_usage_receipts_with_and_without_provider_and_cost(client):
    old = receipt('a', model='gpt-6.2-sol')
    assert client.post('/chat/usage/report', headers=headers('fri'), json=old).status_code == 200
    for new in (receipt('b', provider='claude', cost_micros=1500), receipt('c', provider='claude', cost_micros=500),
                receipt('d', model='gpt-6.2-sol', provider='chatgpt', failover='rate_limited')):
        assert client.post('/chat/usage/report', headers=headers('fri'), json=new).status_code == 200
    for bad in ({'provider': 'openai'}, {'cost_micros': -1}, {'cost_micros': 'lots'}, {'cost_micros': 10**9+1},
                {'cost_micros': 2**63}, {'failover': 'x'*41}, {'failover': 'Rate Limited!'}, {'failover': 'other'}):
        assert client.post('/chat/usage/report', headers=headers('fri'), json=receipt('e', **bad)).status_code == 422
    rows = {(r['model'], r['provider']): r for r in client.post('/chat/usage', headers=headers('fri'),
                                                                 json=actor()).json()}
    assert rows[('claude-opus-5-5', 'claude')]['cost_micros'] == 2000
    assert rows[('claude-opus-5-5', 'claude')]['attempts'] == 2
    plan = rows[('gpt-6.2-sol', 'chatgpt')]
    assert plan['attempts'] == 2 and plan['cost_micros'] is None and plan['unknown_cost_attempts'] == 2
    by_role = client.post('/agent/usage', headers=headers('fri'), json=actor()).json()
    assert sorted((r['provider'], r['cost_micros']) for r in by_role) == [('chatgpt', None), ('claude', 2000)]
    with client.app.state.store.connection() as db:
        assert db.execute('SELECT failover FROM chat_usage_cost WHERE attempt_id=?', ('d'*32,)).fetchone()[0] == \
            'rate_limited'


def test_worker_usage_takes_provider_and_cost(store):
    request(store)
    job = store.claim()
    store.add_usage(job['id'], job['lease'], 'attempt-1', 'gpt-6.2-sol', {'input_tokens': 1, 'output_tokens': 1})
    store.add_usage(job['id'], job['lease'], 'attempt-2', 'claude-opus-5-5', {'input_tokens': 1, 'output_tokens': 1},
                    'claude', 700)
    with pytest.raises(ValueError):
        store.add_usage(job['id'], job['lease'], 'attempt-3', 'x', {}, 'claude', True)
    rows = {r['provider']: r for r in store.usage()}
    assert rows['claude']['cost_micros'] == 700 and rows['chatgpt']['cost_micros'] is None


def test_the_spend_guard_falls_back_to_the_plan_at_the_limit_and_audits_once(store):
    store.update_chat_models(['gpt-6.2-sol'])
    with store.connection() as db:
        db.execute("UPDATE settings SET value='claude' WHERE key='model_provider'")
    last_month = time.time() - 40*86400
    with patch('gene.store.time.time', return_value=last_month):  # Last month's spend never counts.
        store.add_chat_usage('a'*32, 'claude-opus-5-5', 'high', 'completed', {}, provider='claude',
                             cost_micros=CLAUDE_SOFT_LIMIT)
    store.add_chat_usage('b'*32, 'claude-opus-5-5', 'high', 'completed', {}, provider='claude',
                         cost_micros=CLAUDE_SOFT_LIMIT-2)
    store.add_chat_usage('c'*32, 'gpt-6.2-sol', 'high', 'completed', {}, provider='chatgpt', cost_micros=10**9)
    assert store.status()['provider'] == 'claude' and guard_rows(store) == 0
    assert store.chat_context('guild', 'private:1', 'person')['provider'] == 'claude'
    store.add_chat_usage('d'*32, 'claude-opus-5-5', 'high', 'completed', {}, provider='claude', cost_micros=1)
    assert store.status()['provider'] == 'claude'
    store.add_chat_usage('e'*32, 'claude-opus-5-5', 'high', 'completed', {}, provider='claude', cost_micros=1)
    status = store.status()
    assert (status['provider'], status['model_provider'], status['claude_spend_guard']) == ('chatgpt', 'claude', True)
    context = store.chat_context('guild', 'private:1', 'person')
    assert context['provider'] == 'chatgpt' and context['claude_chat_model'] == ''
    assert store.chat_models()['provider'] == 'chatgpt'
    assert guard_rows(store) == 1


def test_context_status_and_state_carry_the_effective_provider(h):  # noqa: F811
    _, scope, _ = invited(h)
    context = post(h, '/chat/context', scope).json()
    assert context['provider'] == 'chatgpt' and context['claude_chat_model'] == ''
    plain = {**actor('20'), 'guild_id': '123', 'channel_id': '456', 'conversation_id': 'private:20'}
    assert post(h, '/chat/context', plain).json()['provider'] == 'chatgpt'
    assert h.client.get('/status', headers=headers('fri')).json()['provider'] == 'chatgpt'
    assert h.client.get('/agent/state', headers=headers('fri')).json()['provider'] == 'chatgpt'


def previous_gene(tmp_path):
    """Gene's own code from before this change, or None in a shallow clone (CI's) that doesn't have it."""
    if subprocess.run(['git', '-C', str(ROOT), 'cat-file', '-e', BEFORE + '^{commit}'], capture_output=True).returncode:
        return None
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', BEFORE, 'src'], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(tmp_path, **({'filter': 'data'} if hasattr(tarfile, 'data_filter') else {}))
    return tmp_path/'src'


def test_an_older_gene_still_records_usage_in_a_database_this_one_wrote(tmp_path):
    path = tmp_path/'gene.sqlite3'
    store = Store(path)
    store.add_chat_usage('a'*32, 'claude-opus-5-5', 'high', 'completed', {}, provider='claude', cost_micros=5)
    # The older Gene's write, statement for statement (its chat_usage insert is positional).
    with store.connection() as db:
        db.execute('INSERT OR IGNORE INTO chat_usage VALUES (?,?,?,?,?,?,?,?,?)',
                   ('b'*32, 'gpt-6.2-sol', 'high', 'completed', time.time(), 1, None, 1, None))
    code = previous_gene(tmp_path/'previous')
    if code is not None:
        script = ('import sys\nfrom pathlib import Path\nfrom gene.store import Store\nstore = Store(Path(sys.argv[1]))\n'
                  "store.add_chat_usage('c'*32, 'gpt-6.2-sol', 'high', 'completed', {'input_tokens': 3})\n"
                  'print(sum(r["attempts"] for r in store.chat_usage()))\n')
        done = subprocess.run([sys.executable, '-c', script, str(path)], cwd=code, capture_output=True, text=True,
                              env={**os.environ, 'PYTHONPATH': str(code)})
        assert done.returncode == 0, done.stderr[-2000:]
        assert done.stdout.strip() == '3'
    rows = {r['provider']: r for r in Store(path).chat_usage()}
    assert rows['claude']['cost_micros'] == 5
    assert rows['chatgpt']['attempts'] == (2 if code is not None else 1)
