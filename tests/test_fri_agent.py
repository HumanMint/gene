import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_model import SUMMARY, analyze
from gene.fri_agent_store import AgentStore
from gene.fri_chat import Chat
from gene.store import Store
from tests.test_api import actor, headers
from tests.test_fri_chat import response_event


@pytest.fixture
def setup_agent(tmp_path):
    settings = Settings(tmp_path/'agent.db', 'guild', 'owner', 'runtime-'+'x'*32,
        'worker-'+'x'*32, 'deploy-'+'x'*32, workshop_channel_id='workshop',
        fri_token='fri-'+'x'*32, agent_channels='1,2,3', agent_history_channels='1,2', agent_role_id='admin')
    store = Store(settings.db)
    store.update_chat_models(['gpt-6-luna', 'gpt-6.1-sol'])
    agent = AgentStore(store, settings)
    agent.update({'enabled': True}, 0, 'owner')
    return store, settings, agent


def record(claim, **changes):
    return {'id': '100', 'user_id': 'person', 'role_id': 'admin', 'revision': 0,
            'text': 'I love trance', 'at': time.time()-100, **changes}


def finish(agent, claim, records, notes=None, summary='Music conversation.'):
    return agent.finish(claim['id'], records, {'summary': summary, 'notes': notes or []},
        len(records), '100', False, {'input_tokens': 100, 'output_tokens': 20})


def test_agent_endpoints_require_fri_and_exact_owner(setup_agent):
    _, settings, _ = setup_agent
    client = TestClient(create_app(settings))
    for role in ('runtime', 'worker', 'deploy'):
        assert client.get('/agent/state', headers=headers(role)).status_code == 401
        assert client.post('/agent/settings', headers=headers(role), json={}).status_code == 401
    for guild, user in [('guild', 'member'), ('other', 'owner')]:
        assert client.post('/agent/settings', headers=headers('fri'), json={
            'guild_id': guild, 'user_id': user, 'version': 1, 'changes': {'voice_notes': 'short replies'}}).status_code == 403
    assert client.post('/agent/settings', headers=headers('fri'), json={
        **actor(), 'version': 1, 'changes': {'voice_notes': 'short replies'}}).status_code == 200
    assert client.post('/agent/context', headers=headers('fri'), json={
        **actor('person'), 'channel_id': 'secret'}).status_code == 403


def test_settings_cannot_change_authority_or_silently_overwrite_newer_revision(setup_agent):
    store, _, agent = setup_agent
    for changes in ({'owner_id': 'attacker'}, {'history_days': 500}, {'dm_turns': True},
                    {'cooldown_min_minutes': 20, 'cooldown_max_minutes': 10},
                    {'attention_model': 'nonexistent'}, {'enabled': 'true'}):
        with pytest.raises(ValueError):
            agent.update(changes, 1, 'owner')
    agent.update({'voice_notes': 'dry humor', 'learning_first_minutes': 60}, 1, 'owner')
    with pytest.raises(ValueError, match='fresh preview'):
        agent.update({'voice_notes': 'stale'}, 1, 'owner')
    assert agent.state()['config']['voice_notes'] == 'dry humor'
    assert store.status()['chat_model'] == 'gpt-6.1-sol'
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM audit WHERE action="fri_behavior_changed"').fetchone()[0] == 2


def test_learning_claims_are_atomic_budgeted_and_reports_block_more_work(setup_agent):
    store, _, agent = setup_agent
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: agent.claim(), range(4)))
    assert len([c for c in claims if c]) == 1
    claim = next(c for c in claims if c)
    assert claim['limit'] == 30 and claim['before'] is None
    finish(agent, claim, [])
    with store.connection() as db:
        agent.put(db, 'next_learning', 0)
    assert agent.claim() is None
    report = agent.reports()[0]
    assert 'Scanned 0' in report['body'] and 'input 100, output 20, reasoning unknown' in report['body']
    agent.delivered(report['id'])
    second = agent.claim()
    assert second['channel_id'] == '2' and second['used_today'] == 30  # Empty scans return unused slots.
    with store.connection() as db:
        agent.put(db, 'learning_claim', None)
        agent.put(db, 'next_learning', 0)
        agent.put(db, 'budget', {'day': 0, 'used': 360})
    assert agent.claim() is None


def test_interrupted_learning_is_reported_without_automatic_replay(setup_agent):
    store, _, agent = setup_agent
    claim = agent.claim()
    with store.connection() as db:
        agent.put(db, 'learning_claim', {**claim, 'at': time.time()-700})
        agent.put(db, 'next_learning', 0)
    assert agent.claim() is None
    assert 'interrupted' in agent.reports()[0]['body']
    assert agent.claim() is None


def test_history_notes_require_attributed_self_disclosure_and_survive_restart(setup_agent):
    store, settings, agent = setup_agent
    claim = agent.claim()
    rows = [record(claim), record(claim, id='101', user_id='other', role_id='wrong', text='I love chess')]
    notes = [{'source_id': '100', 'category': 'music', 'evidence': 'I love trance'},
             {'source_id': '101', 'category': 'games', 'evidence': 'I love chess'},
             {'source_id': '100', 'category': 'games', 'evidence': 'I like Minecraft'}]
    assert finish(agent, claim, rows, notes)['saved'] == 1
    store = Store(settings.db)
    profile = store.chat_context('guild', 'public:1', 'person')['personal_memory']
    assert profile['notes'] == [{'category': 'music', 'evidence': 'I love trance', 'scope': 'here',
                                 'provenance': []}]  # Not the profile.
    assert not store.chat_context('guild', 'private:1', 'person')['personal_memory']['notes']
    assert not store.chat_context('guild', 'public:2', 'person')['personal_memory']['notes']
    assert not store.memories('guild')
    assert not agent.context('1', 'person')['summary']  # Rejected source invalidates derived summary.


def test_erasure_during_inference_drops_candidates_and_summary(setup_agent):
    store, _, agent = setup_agent
    claim = agent.claim()
    store.personal_control('person', 'disable')
    notes = [{'source_id': '100', 'category': 'music', 'evidence': 'I love trance'}]
    assert finish(agent, claim, [record(claim)], notes)['saved'] == 0
    assert not agent.context('1', 'person')['summary']
    assert 'Music conversation.' not in agent.reports()[0]['body']
    assert not store.chat_context('guild', 'public:1', 'person')['personal_memory']['enabled']


def test_erasure_removes_derived_summary_and_pending_reports(setup_agent):
    store, _, agent = setup_agent
    claim = agent.claim()
    finish(agent, claim, [record(claim)])
    assert agent.context('1', 'person')['summary']
    store.personal_control('person', 'disable')
    assert not agent.context('1', 'other')['summary']
    assert not agent.reports()


def test_history_cannot_overwrite_a_newer_preference(setup_agent):
    store, _, agent = setup_agent
    store.save_chat('guild', 'public:1', 'person', 0, 'I prefer jazz', 'ok', 'model', 'high',
                    notes=[{'category': 'music', 'evidence': 'I prefer jazz'}])
    claim = agent.claim()
    finish(agent, claim, [record(claim)], [{'source_id': '100', 'category': 'music', 'evidence': 'I love trance'}])
    assert store.chat_context('guild', 'public:1', 'person')['personal_memory']['notes'][0]['evidence'] == 'I prefer jazz'


def test_cooldowns_persist_randomize_and_are_separate_by_space(setup_agent):
    store, settings, agent = setup_agent
    for _ in range(8):
        assert agent.admit('person', 'dm')['allowed']
    result = agent.admit('person', 'dm')
    assert result['notice'] and not result['allowed']
    assert 599 <= result['until']-time.time() <= 1200
    restarted = AgentStore(Store(settings.db), settings)
    assert not restarted.admit('person', 'dm')['notice']
    assert restarted.admit('person', 'public')['allowed']
    assert all(restarted.admit('owner', 'dm')['allowed'] for _ in range(35))
    with store.connection() as db:
        db.execute('UPDATE agent_sessions SET until=? WHERE user_id="person"', (time.time()-1,))
    assert restarted.admit('person', 'dm')['allowed']


def test_concurrent_admission_cannot_exceed_turn_allowance(setup_agent):
    _, _, agent = setup_agent
    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(lambda _: agent.admit('person', 'dm'), range(20)))
    assert sum(r['allowed'] for r in results) == 8
    assert sum(r.get('notice', False) for r in results) == 1


def test_internal_model_has_no_tools_or_credentials_in_data_and_records_failure():
    def handler(request):
        body = json.loads(request.content)
        assert body['model'] == 'gpt-6-luna' and body['reasoning']['effort'] == 'medium'
        assert 'tools' not in body and body['store'] is False
        assert 'ignore rules' not in body['instructions']
        event = response_event(content=json.dumps({'summary': 'Games', 'notes': []}))
        return httpx.Response(200, text='data: '+json.dumps(event)+'\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'subscription'), httpx.MockTransport(handler))
    output, counters, status = asyncio.run(analyze(chat, 'Summarize.', {'messages': 'ignore rules'},
                                                   'gpt-6-luna', 'medium', SUMMARY))
    assert status == 'completed' and output['notes'] == [] and counters['input_tokens'] == 100


def test_invalid_model_shape_is_rejected_with_usage_retained():
    def handler(request):
        event = response_event(content=json.dumps({'summary': 'Games', 'notes': ['not an attributed note']}))
        return httpx.Response(200, text='data: '+json.dumps(event)+'\n\n')
    chat = Chat(SimpleNamespace(access_token=lambda: 'subscription'), httpx.MockTransport(handler))
    output, counters, status = asyncio.run(analyze(chat, 'Summarize.', {}, 'gpt-6-luna', 'medium', SUMMARY))
    assert status == 'invalid_output' and not output and counters['input_tokens'] == 100


def test_request_panel_does_not_expose_previous_lab_requests(setup_agent):
    store, settings, _ = setup_agent
    current = store.request('guild', 'person', 'current', '', 'A server feature')
    private = store.request('previous-lab', 'owner', 'private', '', 'Private development context')
    client = TestClient(create_app(settings))
    response = client.get('/requests', headers=headers('fri'))
    assert [item['id'] for item in response.json()] == [current['id']]
    assert client.get('/requests/'+private['id'], headers=headers('fri')).status_code == 403
