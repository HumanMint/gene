"""Learning receipt persistence, owner-only navigation and actual Discord call signatures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import discord
import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from tests.test_api import headers
from tests.test_fri_exploration import archive, assigned, finish, note, row
from tests.test_fri_learning import learning as learning


def complete(learning, outcome='note'):
    clock, store, settings, scheduler = archive(learning)
    claim = assigned(scheduler, scheduler.claim())
    scheduler.exploration.prepare(claim['id'], '11', '', 'Read an unread slice')
    item = row(clock[0]-86400)
    if outcome == 'duplicate':
        with store.connection() as db:
            db.execute('INSERT INTO fri_explore_seen VALUES (?,?,?,?)', (item['id'], '1', '11', clock[0]))
        scheduler.finish(claim['id'], [], {}, 1, item['id'], True, {}, scanned_ids=[item['id']])
    else:
        finish(scheduler, claim, [] if outcome == 'empty' else [item],
               observations=[note(item)], error='Model unavailable' if outcome == 'error' else '')
    return clock, store, settings, scheduler, scheduler.report(claim['id'])


@pytest.mark.parametrize('removal', ['optout', 'expiry'])
def test_card_retention_tracks_report_and_privacy_erasure(learning, removal):
    clock, store, _, scheduler, report = complete(learning)
    scheduler.delivered(report['id'])
    assert not scheduler.reports() and scheduler.report(report['id'])
    if removal == 'optout':
        store.personal_control('11', 'disable')
    else:
        clock[0] += 7*86400+1
        assert scheduler.report(report['id']) is None
        scheduler.state()  # Scheduled retention also removes the companion row.
    assert scheduler.report(report['id']) is None
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM agent_report_cards').fetchone()[0] == 0
        # Additive migration preserves the old INSERT layout for release rollback.
        db.execute('INSERT INTO agent_reports VALUES (?,?,?,0)', ('b'*32, clock[0], 'Legacy report'))
    assert scheduler.report('b'*32)['card'] == {}


def test_report_endpoint_requires_fri_service_and_configured_owner(learning):
    clock, _, settings, scheduler, report = complete(learning)
    client = TestClient(create_app(settings))
    endpoint = f"/agent/reports/{report['id']}/view"
    identity = {'guild_id': '123', 'user_id': '10'}
    for role in ('runtime', 'worker', 'deployer'):
        assert client.post(endpoint, headers=headers(role), json=identity).status_code in (401, 403)
    for identity_bad in ({'guild_id': '999', 'user_id': '10'}, {'guild_id': '123', 'user_id': '11'}):
        assert client.post(endpoint, headers=headers('fri'), json=identity_bad).status_code == 403
    result = client.post(endpoint, headers=headers('fri'), json=identity)
    assert result.status_code == 200 and result.json() == report
    assert scheduler.reports() == [report]  # Reading never acknowledges or approves anything.
    clock[0] += 7*86400+1
    assert client.post(endpoint, headers=headers('fri'), json=identity).status_code == 404


def event(report):
    # Autospec checks real discord.py method parameters, not just mock call counts.
    interaction = create_autospec(discord.Interaction, instance=True)
    fri = SimpleNamespace(owner_id=10, guild_id=123, user=SimpleNamespace(id=99),
        api=AsyncMock(return_value=report), agent=SimpleNamespace(member=AsyncMock(return_value=object())))
    interaction.client, interaction.guild_id = fri, None
    interaction.type = discord.InteractionType.component
    interaction.user = SimpleNamespace(id=10)
    interaction.message = SimpleNamespace(author=SimpleNamespace(id=99))
    interaction.data = {'custom_id': f"fri:learning:{report['id']}:0"}
    interaction.response = SimpleNamespace(defer=AsyncMock())
    interaction.followup = SimpleNamespace(send=AsyncMock())
    return fri, interaction
