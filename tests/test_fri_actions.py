import asyncio
import json
import time
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_chat import Chat
from tests.test_api import actor, headers
from tests.test_fri_agent import finish, record
from tests.test_fri_agent import setup_agent as setup_agent
from tests.test_fri_chat import CONTEXT, response_event


def test_owner_learning_endpoints_reject_other_roles_and_forged_owners(setup_agent):
    _, settings, _ = setup_agent
    client = TestClient(create_app(settings))
    for endpoint in ('overview', 'request'):
        path = '/agent/learning/'+endpoint
        for role in ('runtime', 'worker', 'deploy'):
            assert client.post(path, headers=headers(role), json=actor()).status_code == 401
        for guild, user in [('guild', 'member'), ('wrong', 'owner')]:
            assert client.post(path, headers=headers('fri'), json={
                'guild_id': guild, 'user_id': user}).status_code == 403
        assert client.post(path, headers=headers('fri'), json=actor()).status_code == 200


def test_manual_pass_keeps_budget_cursor_report_gate_and_pause(setup_agent):
    store, _, agent = setup_agent
    with store.connection() as db:
        agent.put(db, 'next_learning', time.time()+99999)
        agent.put(db, 'cursors', {'1': {'before': '150', 'done_at': 0}})
    assert agent.claim() is None
    claim = agent.claim(manual=True)
    assert claim['before'] == '150' and claim['used_today'] == 30
    assert 'blocked' in agent.claim(manual=True)
    finish(agent, claim, [record(claim)])
    with store.connection() as db:
        agent.put(db, 'manual_learning_after', 0)
    assert 'report' in agent.claim(manual=True)['blocked']
    agent.delivered(claim['id'])
    with store.connection() as db:
        agent.put(db, 'budget', {'day': 0, 'used': 360})
    assert 'allowance' in agent.claim(manual=True)['blocked']
    agent.update({'learning_enabled': False}, 1, 'owner')
    assert 'paused' in agent.claim(manual=True)['blocked']
    assert agent.overview()['daily_reserved'] == 360


def test_manual_pass_checks_new_messages_after_finishing_archive(setup_agent):
    store, _, agent = setup_agent
    with store.connection() as db:
        agent.put(db, 'cursors', {'1': {'done_at': time.time(), 'before': '1'},
                                  '2': {'done_at': 0, 'before': '20'}})
    claim = agent.claim(manual=True)
    assert claim['channel_id'] == '1' and claim['before'] is None
    assert claim['mode'] == 'recent' and int(claim['after_id']) > 1


def test_overview_uses_delivered_reports_and_respects_erasure(setup_agent):
    store, _, agent = setup_agent
    claim = agent.claim()
    finish(agent, claim, [record(claim)], summary='Friends discussed music.')
    agent.delivered(claim['id'])
    overview = agent.overview()
    assert overview['reports'][0]['delivered'] == 1
    assert 'Conversation summary: Friends discussed music.' in overview['reports'][0]['body']
    assert 'Untrusted summary:' not in overview['reports'][0]['body']
    assert overview['summaries'][0]['body'] == 'Friends discussed music.'
    store.personal_control('person', 'disable')
    assert not agent.overview()['reports'] and not agent.overview()['summaries']


@pytest.mark.parametrize('offered,reply,kind,expected', [
    (True, '', 'read_recent', 'completed'),
    (False, '', 'read_recent', 'invalid_output'),
    (True, 'I already did it', 'read_recent', 'invalid_output'),
    (True, '', 'shell', 'invalid_output'),
])
def test_action_parser_accepts_only_offered_complete_requests(offered, reply, kind, expected):
    async def scenario():
        output = {'reply': reply, 'notes': [], 'request_draft': None, 'action': {'kind': kind, 'channel_id': '1'}}
        def handler(request):
            return httpx.Response(200, text='data: '+json.dumps(response_event('completed', json.dumps(output)))+'\n\n')
        chat = Chat(SimpleNamespace(access_token=lambda: 'test'), httpx.MockTransport(handler))
        result = await chat.respond('look around', {**CONTEXT, 'actions': {'read_recent': {}} if offered else {}})
        assert result.status == expected
    asyncio.run(scenario())
