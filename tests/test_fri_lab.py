from dataclasses import replace

from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_agent_store import AgentStore
from tests.test_api import headers
from tests.test_fri_agent import setup_agent as setup_agent


def test_lab_chat_isolated_and_cannot_reach_owner_or_feature_routes(setup_agent):
    store, settings, _ = setup_agent
    settings = replace(settings, test_guild_id='999', test_channel_id='789')
    client = TestClient(create_app(settings))
    auth = headers('fri')
    scope = {'guild_id': '999', 'channel_id': '789', 'user_id': 'owner', 'conversation_id': 'public:789'}
    store.remember('guild', 'owner', 'FF private fact', '')
    store.save_chat('guild', 'public:789', 'owner', 0, 'FF secret', 'FF reply', 'model', 'high')
    assert client.post('/chat/context', headers=auth, json=scope).json()['facts'] == []
    assert client.post('/chat/context', headers=auth, json=scope).json()['turns'] == []
    saved = client.post('/chat/turn', headers=auth, json={**scope, 'revision': 0,
        'source': {'kind': 'discord_message', 'id': '100000000000000001', 'user_id': 'owner',
                   'display_name': 'owner', 'channel_id': '789', 'guild_id': '999'},
        'input': 'Lab hello', 'reply': 'hello', 'model': 'model', 'effort': 'high'})
    assert saved.status_code == 200
    assert store.chat_context('999', 'public:789', 'owner')['turns'][0]['input'] == 'Lab hello'
    assert store.chat_context('guild', 'public:789', 'owner')['turns'][0]['input'] == 'FF secret'
    for changes in ({'guild_id': 'guild'}, {'guild_id': '1000'}, {'channel_id': '1'},
                    {'conversation_id': 'private:789'}, {'conversation_id': 'public:1'}):
        assert client.post('/chat/context', headers=auth, json={**scope, **changes}).status_code == 403
    for route, extra in [('/requests', {'body': 'Please add a useful command', 'source_id': '100', 'source_url': ''}),
                         ('/agent/settings', {'changes': {'voice_notes': 'bad'}, 'version': 1}),
                         ('/agent/learning/overview', {}), ('/agent/learning/request', {}),
                         ('/pause', {'paused': True}), ('/memory/list', {})]:
        assert client.post(route, headers=auth, json={**scope, **extra}).status_code == 403
    for role in ('runtime', 'worker', 'deploy'):
        assert client.post('/chat/context', headers=headers(role), json=scope).status_code == 401


def test_lab_attention_admission_and_cooldowns_keep_separate_scope(setup_agent):
    store, settings, _ = setup_agent
    settings = replace(settings, test_guild_id='999', test_channel_id='789')
    client = TestClient(create_app(settings))
    agent = AgentStore(store, settings)
    scope = {'guild_id': '999', 'channel_id': '789', 'user_id': 'member'}
    for channel in ('1', '456', ''):
        assert client.post('/agent/context', headers=headers('fri'), json={**scope, 'channel_id': channel}).status_code == 403
    assert client.post('/agent/context', headers=headers('fri'), json=scope).json()['summary'] == ''
    assert client.post('/agent/admit', headers=headers('fri'), json={**scope, 'space': 'dm'}).status_code == 403
    for _ in range(agent.state()['config']['public_turns']):
        assert agent.admit('member', 'public', 'guild')['allowed']
    assert not agent.admit('member', 'public', 'guild')['allowed']
    assert client.post('/agent/admit', headers=headers('fri'), json={**scope, 'space': 'public'}).json()['allowed']
    assert agent.state()['history_channels'] == ['1', '2']
    assert agent.claim()['channel_id'] in ('1', '2')
