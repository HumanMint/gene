"""An exploration's target comes back with the member's privacy revision, read in the same locked transaction as the
context it's given, so Fri can hold its planner request to that context (HumanMint/fri#304)."""

import datetime as dt
import time

import discord
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.fri_memory_store import edit, entries
from tests.test_api import headers
from tests.test_fri_exploration import archive
from tests.test_fri_learning import learning as learning


def remove_a_preference(store, user):
    """The member saves a preference, then removes it through Gene as My memory does: their revision moves, and Gene's
    exploration state for them goes."""
    with store.connection() as db:
        revision = store._privacy_state(db, user)[0]
    store.save_chat('123', 'public:1', user, revision, 'I like trance', 'noted', 'model', 'high',
                    notes=[{'category': 'music', 'evidence': 'I like trance'}])
    [note] = [n for n in entries(store, '123', 'public:1', user)['entries'] if n['evidence'] == 'I like trance']
    now = dt.datetime.fromtimestamp(time.time(), dt.timezone.utc)  # The learning fixture's clock.
    source = {'kind': 'discord_interaction', 'user_id': user, 'display_name': 'friend', 'channel_id': '1',
              'guild_id': '123', 'id': str(discord.utils.time_snowflake(now))}
    edit(store, '123', 'public:1', user, note['id'], revision, source, 'remove')


def assign(client, scheduler):
    claim = scheduler.claim()
    response = client.post(f"/agent/learning/{claim['id']}/target", headers=headers('fri'),
                           json={'members': [{'user_id': '11', 'name': 'friend', 'role_id': '90'}]})
    assert response.status_code == 200, response.text
    return response.json()


def revision(client, user='11'):
    """The member's revision as Fri reads it for a fence (/agent/context)."""
    response = client.post('/agent/context', headers=headers('fri'),
                           json={'guild_id': '123', 'user_id': user, 'channel_id': '1'})
    assert response.status_code == 200, response.text
    return response.json()['revision']


def test_a_removal_after_the_target_is_built_is_told_apart_by_its_revision(learning):
    """The target's context (its historical topics and previous searches) was read at one revision, and the member
    removes a preference through Gene before Fri asks the planner. Fri's own read after the response sees only the
    new revision, so without the one the context was read at it can't tell the context is stale. The target says it."""
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    claim = assign(client, scheduler)
    assert claim['target']['revision'] == revision(client) == 0
    remove_a_preference(store, '11')  # After Gene built the response, before Fri resumes.
    assert revision(client) != claim['target']['revision']


def test_the_target_revision_is_the_one_its_context_was_read_at(learning):
    """A removal before the target is assigned is already in its context, and in the revision it comes with."""
    clock, store, settings, scheduler = archive(learning)
    client = TestClient(create_app(settings))
    remove_a_preference(store, '11')
    claim = assign(client, scheduler)
    assert claim['target']['revision'] == revision(client) == 1
    assert claim['target']['user_id'] == '11' and 'exploration' in claim  # The rest of the answer as before.
