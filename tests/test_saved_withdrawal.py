"""fri #174: a message edited or deleted that Fri doesn't know it saved an exchange with, one saved before Fri last
restarted say, is withdrawn by its ID alone while a saved exchange holds it. One nothing holds changes nothing."""
import logging

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_protocol import compatible, contract
from tests.test_api import headers
from tests.test_fri_endings import source
from tests.test_turn_withdrawal import (
    MARKER,
    background,
    built,
    inputs,
    readable,
    save,
    summary,
    withdraw,
)

SAVED = '/chat/turn/withdraw/saved'


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'withdraw.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def withdraw_saved(client, *message_ids, role='fri', guild='123', **extra):
    return client.post(SAVED, headers=headers(role), json={'guild_id': guild, 'message_ids': list(message_ids), **extra})


def fences(store):
    with store.connection() as db:
        return [tuple(row) for row in db.execute('SELECT guild_id,user_id,message_id FROM chat_turn_withdrawals')]


def test_the_exchange_holding_it_goes_under_the_member_and_conversation_it_was_saved_with(world):
    client, store = world
    taken = save(store, 'explain '+MARKER, age=30)
    save(store, 'what about lunch', age=20)
    other = save(store, 'explain that caption?', user='30', age=10, provenance=[{'user_id': '20', 'revision': 0}])
    private = save(store, 'dm about '+MARKER, conversation='private:52', age=5)
    assert withdraw_saved(client, taken).json() == {'ok': True, 'removed': 1}
    assert inputs(store) == ['what about lunch']
    assert inputs(store, '30') == ['explain that caption?']  # Another member's reply that drew on it stays (fri #101).
    assert fences(store) == [('123', '20', taken)]  # As a withdrawal Fri names the scope of leaves.
    assert withdraw_saved(client, other, private).json() == {'ok': True, 'removed': 2}
    assert inputs(store, '30') == [] and inputs(store, conversation='private:52') == []
    assert withdraw_saved(client, taken, other).json() == {'ok': True, 'removed': 0}  # Again: nothing left, still fine.
    assert store.check_chat('20', 0) == {'ok': True}  # Not a privacy change: their replies in flight still save.


def test_a_message_inside_a_burst_takes_the_burst_exchange_and_its_question(world):
    client, store = world
    first, last = source(age=4, channel='51'), source(age=2, channel='51')
    burst = [{'source': first, 'text': MARKER+' with the lights off'}, {'source': last, 'text': 'fri play music'}]
    store.save_chat('123', 'public:51', '20', 0, 'fri play music', 'which song?', 'model', 'high', source=last,
                    burst=burst, provenance=[], clarification={'goal': 'play', 'topic': '', 'question': 'which song?'})
    assert store.chat_context('123', 'public:51', '20')['pending']
    assert withdraw_saved(client, first['id']).json() == {'ok': True, 'removed': 1}
    assert inputs(store) == [] and store.chat_context('123', 'public:51', '20')['pending'] is None


def test_a_summary_built_on_it_goes_with_it(world):
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid with the '+MARKER)
    assert MARKER in summary(store)
    assert withdraw_saved(client, taken).json() == {'ok': True, 'removed': 1}
    assert summary(store) is None


def test_one_nothing_holds_changes_nothing(world):
    """No fence and no summary dropped: only a saved exchange shows Fri a member's message, and Fri asks about every
    edit and delete it can't place, so an unrelated one mustn't cost the member their summary."""
    client, store = world
    agent = background(client)
    readable(store, 'raid tonight', 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid')
    unrelated = source(age=7280, channel='51')['id']  # Said in a channel the summary read, before it was built.
    assert withdraw_saved(client, unrelated, '1').json() == {'ok': True, 'removed': 0}
    assert fences(store) == [] and summary(store) == 'they planned a raid'
    lab = save(store, 'lab chat', guild='777', conversation='public:78')
    assert withdraw_saved(client, lab).json()['removed'] == 0  # Another server's exchange isn't this one's.
    assert withdraw_saved(client, lab, guild='777').json()['removed'] == 1


def test_scope_and_credentials(world):
    client, store = world
    assert withdraw_saved(client, '1', guild='456').status_code == 403
    for role in ('runtime', 'worker', 'deploy'):
        assert withdraw_saved(client, '1', role=role).status_code == 401
    for body in ({'guild_id': '123', 'message_ids': []}, {'guild_id': '123', 'message_ids': ['abc']},
                 {'guild_id': '123', 'message_ids': [str(i) for i in range(1, 12)]},
                 {'guild_id': '123', 'message_ids': ['1'], 'user_id': '20'}, {'guild_id': 'x', 'message_ids': ['1']}):
        assert client.post(SAVED, headers=headers('fri'), json=body).status_code == 422
    assert withdraw(client, '1').status_code == 200  # The scoped withdrawal is unchanged.


def test_nothing_it_logs_holds_the_words(world, caplog):
    client, store = world
    caplog.set_level(logging.DEBUG)
    withdraw_saved(client, save(store, 'explain '+MARKER))
    assert MARKER not in caplog.text


def test_the_contract_offers_it_as_an_optional_capability():
    advertised = contract()
    assert 'saved-turn-withdrawal' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities']
                                                      if c != 'saved-turn-withdrawal']})


@pytest.mark.parametrize('ids', [1, 10])
def test_up_to_ten_at_once(world, ids):
    client, store = world
    taken = [save(store, f'line {i}', age=60-i) for i in range(ids)]
    assert withdraw_saved(client, *taken).json() == {'ok': True, 'removed': min(ids, 6)}  # Six kept a conversation.
    assert inputs(store) == []
