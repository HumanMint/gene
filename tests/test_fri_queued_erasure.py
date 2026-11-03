"""A queued message from before the member turned memory off, answered once it's on again, keeps nothing: no exchange
and no note (HumanMint/fri#331). Gene's other cutoffs, fresh turns and source-less saves are as before."""

import datetime as dt
import time
from dataclasses import replace

import discord
from fastapi.testclient import TestClient

from gene.api import create_app
from tests.test_api import headers
from tests.test_fri_learning import learning as learning


def test_the_profile_says_when_memory_was_last_turned_off_apart_from_removals(learning):
    """Fri holds a queued message to the time memory was turned off before it asks the model (fri #331), but not to a
    note's removal, which a queued message is still answered after and kept nowhere. So the profile says that time on
    its own (`erased_at`), besides `removed_at`, which counts both. Nobody else's, and 0 for a member who never did.
    The other fields are as before, and so is a history channel's narrower answer."""
    clock, store, settings, _ = learning
    client = TestClient(create_app(replace(settings, agent_history_channels='1,2,3')))  # 3: history, not chat.

    def profile(user='11', channel='1'):
        response = client.post('/agent/context', headers=headers('fri'),
                               json={'guild_id': '123', 'user_id': user, 'channel_id': channel})
        assert response.status_code == 200, response.text
        return response.json()

    before = profile()
    assert before['erased_at'] == 0
    store.personal_control('11', 'disable')
    turned_off = time.time()
    clock[0] += 60
    store.personal_control('11', 'enable')
    answer = profile()
    assert answer['erased_at'] == answer['removed_at'] == turned_off and profile('14')['erased_at'] == 0
    assert set(answer) == set(before)
    # A history channel's narrower answer, for learning, which no ordinary reply reads, is as it was.
    assert set(profile(channel='3')) == {'enabled', 'revision', 'removed_at'}


def source(age=0, user='20'):
    return {'kind': 'discord_message', 'id': str(discord.utils.time_snowflake(
        dt.datetime.now(dt.timezone.utc)-dt.timedelta(seconds=age))), 'user_id': user, 'channel_id': '456',
        'guild_id': '123', 'display_name': 'friend'}


def say(store, text, source, revision=2):
    return store.save_chat('123', 'public:456', '20', revision, text, 'noted', 'model', 'high', source=source,
                           notes=[{'category': 'music', 'evidence': text}])


def kept(store):
    context = store.chat_context('123', 'public:456', '20')
    return [turn['input'] for turn in context['turns']], [note['evidence'] for note in context['personal_memory']['notes']]


def test_a_queued_message_from_before_memory_was_turned_off_keeps_nothing_once_it_is_on_again(store):
    """fri #331: the member's message was sent, they turned memory off and on again through Gene, and only then did Fri
    answer it. The save kept the exchange and a note under the new revision: their words from before the erasure. It
    keeps nothing, as one from before a removal doesn't."""
    queued = source(age=30)
    store.personal_control('20', 'disable')
    store.personal_control('20', 'enable')
    assert say(store, 'i love trance', queued) == {'ok': True, 'first_memory': False, 'kept': False}
    assert kept(store) == ([], [])


def test_a_fresh_message_and_a_source_less_save_after_it_are_kept(store):
    """What's said after memory is on again is kept as before, and so is a save with no message to date it."""
    store.personal_control('20', 'disable')
    store.personal_control('20', 'enable')
    time.sleep(0.01)  # Snowflakes count milliseconds.
    assert say(store, 'i love jazz', source()).get('kept') is not False
    assert store.save_chat('123', 'public:456', '20', 2, 'and lofi', 'noted', 'model', 'high').get('kept') is not False
    turns, notes = kept(store)
    assert turns == ['i love jazz', 'and lofi'] and notes == ['i love jazz']
