import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from redbot.core import commands

from gene.fri_requests import DraftView, request_dm_component
from gene.reviews import request_reference


def setup(user=10, guild=None):
    cog = SimpleNamespace(owner_id=10, guild_id=123, workshop_id=456, user=SimpleNamespace(id=99), api=AsyncMock(),
                          agent=SimpleNamespace(member=AsyncMock(return_value=object()), allowed=lambda c: c.id == 456))
    event = SimpleNamespace(user=SimpleNamespace(id=user), guild_id=guild, channel_id=456, channel=SimpleNamespace(id=456),
        type=discord.InteractionType.component, data={'custom_id': 'fri:request:preview:'+'a'*32},
        message=SimpleNamespace(author=cog.user, flags=SimpleNamespace(ephemeral=False)),
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock())
    return cog, event


@pytest.mark.parametrize('user', [10, 20])
def test_preview_is_not_approval_and_submission_approves_only_verified_owner(store, user):
    async def scenario():
        cog, event = setup(user)
        body = 'Please route music notifications to the configured bot commands channel.'
        async def api(method, path, payload):
            if path == '/requests':
                return store.request(payload['guild_id'], payload['user_id'], payload['source_id'], payload['source_url'], payload['body'])
            store.review_decision(path.split('/')[2], payload['phase'], payload['reference'], payload['decision'], payload['user_id'])
            return {'ok': True}
        cog.api.side_effect = api
        draft = DraftView(cog, user, body)
        await draft.preview(event)
        cog.api.assert_not_awaited()
        preview = event.response.edit_message.await_args.kwargs['view']
        assert preview.children[0].label == ('Submit & allow coding' if user == 10 else 'Submit for owner review')
        await preview.submit(event)
        job = store.recent('123')[0]
        assert job['body'] == body and job['user_id'] == str(user)
        assert job['state'] == ('queued' if user == 10 else 'awaiting_owner')
        if user == 10:
            assert job['intake_approved_ref'] == request_reference(job)
        await preview.submit(event)
        assert len(store.recent('123')) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize('denial', ['other_user', 'other_author', 'nonmember', 'other_guild', 'other_channel'])
def test_dm_and_public_drafts_cannot_be_approved_by_other_people_or_scope(denial):
    async def scenario():
        cog, event = setup()
        draft = DraftView(cog, 10, 'A new feature', reviewed=True)
        if denial == 'other_user':
            event.user.id = 20
        elif denial == 'other_author':
            event.message.author = SimpleNamespace(id=20)
        elif denial == 'nonmember':
            cog.agent.member.return_value = None
        elif denial == 'other_guild':
            event.guild_id = 777
        else:
            event.guild_id, event.channel_id, event.channel.id = 123, 999, 999
        await draft.submit(event)
        cog.api.assert_not_awaited()
    asyncio.run(scenario())


def test_request_dm_admission_only_admits_our_request_components():
    _, event = setup()
    assert request_dm_component(event, 99)
    event.data = {'custom_id': 'gene:home:settings'}
    assert not request_dm_component(event, 99)
    event.data = {'custom_id': 'fri:request:submit:'+'b'*32}
    event.message.author = SimpleNamespace(id=20)
    assert not request_dm_component(event, 99)


def test_preview_mismatch_never_approves_returned_job():
    async def scenario():
        cog, event = setup()
        draft = DraftView(cog, 10, 'A safe feature', reviewed=True)
        cog.api.return_value = {'body': 'Different malicious request', 'user_id': '10'}
        with pytest.raises(commands.UserFeedbackCheckFailure):
            await draft.submit(event)
        assert cog.api.await_count == 1
    asyncio.run(scenario())
