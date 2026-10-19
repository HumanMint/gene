import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from redbot.core import commands

from gene.fri_agent import Agent, BehaviorPreview
from tests.test_api import actor, headers

PROPOSAL = Path(__file__).resolve().parents[2] / 'docs/features/fri-personality-preview.json'


@pytest.fixture
def changes():
    return json.loads(PROPOSAL.read_text())


def test_proposal_fits_existing_preview_and_note_limits(changes):
    assert set(changes) == {'voice_notes'}
    assert isinstance(changes['voice_notes'], str) and changes['voice_notes'].strip()
    assert len(changes['voice_notes']) <= 1500
    assert len(json.dumps(changes, indent=2)) <= 1500


def test_owner_receives_versioned_preview_without_saving(client, changes):
    async def scenario():
        before = client.get('/agent/state', headers=headers('fri')).json()
        fri = SimpleNamespace(owner_id=10, guild_id=123, api=AsyncMock(return_value=before))
        agent = Agent(fri)
        agent.run = AsyncMock(return_value=({'changes': [
            {'key': key, 'json_value': json.dumps(value)} for key, value in changes.items()
        ]}, {}, 'completed'))
        message = SimpleNamespace(author=SimpleNamespace(id=10), guild=None, reply=AsyncMock())

        assert await agent.owner_control(message, 'use sharper dry sarcasm in your voice settings')
        fri.api.assert_awaited_once_with('GET', '/agent/state')
        preview = message.reply.call_args.kwargs['view']
        assert isinstance(preview, BehaviorPreview)
        assert preview.changes == changes
        assert preview.version == before['version']
        assert json.dumps(changes, indent=2) in message.reply.call_args.args[0]
        assert client.get('/agent/state', headers=headers('fri')).json() == before
        preview.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize('user_id,author_id,allowed', [
    (10, 99, True), (11, 99, False), (10, 11, False),
])
def test_proposal_preview_checks_owner_and_bot_message(changes, user_id, author_id, allowed):
    async def scenario():
        agent = SimpleNamespace(fri=SimpleNamespace(owner_id=10, user=SimpleNamespace(id=99)))
        preview = BehaviorPreview(agent, changes, 0)
        interaction = SimpleNamespace(user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(author=SimpleNamespace(id=author_id)))
        assert await preview.interaction_check(interaction) is allowed
        preview.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize('outcome', ['apply', 'cancel', 'stale'])
def test_proposal_apply_cancel_and_stale_recovery(client, changes, outcome):
    async def scenario():
        before = client.get('/agent/state', headers=headers('fri')).json()

        async def api(method, route, payload):
            response = client.request(method, route, headers=headers('fri'), json=payload)
            if response.status_code != 200:
                raise commands.UserFeedbackCheckFailure(response.json()['detail'])
            return response.json()

        fri = SimpleNamespace(owner_id=10, guild_id='guild', api=AsyncMock(side_effect=api))
        agent = Agent(fri)
        agent.state = before
        preview = BehaviorPreview(agent, changes, before['version'])
        interaction = SimpleNamespace(user=SimpleNamespace(id='owner'),
            response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()),
            edit_original_response=AsyncMock())

        if outcome == 'cancel':
            await preview.cancel.callback(interaction)
            fri.api.assert_not_awaited()
            assert client.get('/agent/state', headers=headers('fri')).json() == before
            assert interaction.response.edit_message.call_args.kwargs['content'] == 'No settings changed.'
            return

        expected = before
        if outcome == 'stale':
            response = client.post('/agent/settings', headers=headers('fri'), json={
                **actor(), 'version': before['version'], 'changes': {'attention_notes': 'Stay quiet.'}})
            assert response.status_code == 200
            expected = response.json()

        await preview.apply.callback(interaction)
        fri.api.assert_awaited_once_with('POST', '/agent/settings', {
            **actor(), 'changes': changes, 'version': before['version']})
        after = client.get('/agent/state', headers=headers('fri')).json()
        content = interaction.edit_original_response.call_args.kwargs['content']
        if outcome == 'stale':
            assert after == expected
            assert 'fresh preview' in content
            assert 'Saved' not in content
            return

        assert after['config'] == {**before['config'], **changes}
        assert after['version'] == before['version'] + 1
        assert agent.state == after
        assert f"Saved behavior revision {after['version']}" in content
        # Reload the persisted state independently of the Discord view's cache.
        assert client.app.state.agent.state() == after

    asyncio.run(scenario())


@pytest.mark.parametrize('role,user,guild,status', [
    ('runtime', 'owner', 'guild', 401), ('worker', 'owner', 'guild', 401),
    ('deploy', 'owner', 'guild', 401), ('fri', 'member', 'guild', 403),
    ('fri', 'owner', 'other', 403),
])
def test_proposal_cannot_bypass_controller_authorization(client, changes, role, user, guild, status):
    before = client.get('/agent/state', headers=headers('fri')).json()
    response = client.post('/agent/settings', headers=headers(role), json={
        'guild_id': guild, 'user_id': user, 'version': before['version'], 'changes': changes})
    assert response.status_code == status
    assert client.get('/agent/state', headers=headers('fri')).json() == before
