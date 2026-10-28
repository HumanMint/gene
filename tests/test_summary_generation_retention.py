"""fri #172 and fri #171: what Fri keeps from a turn that drew on its summary of the member doesn't outlive that summary.

A saved reply keeps the summary generation its turn sent (gene #91). Once the generation moves, the reply is a neutral
breadcrumb, so a later turn can't read it back as history and repeat it. A tool's write (saved work, a reminder) that
sends the generation is refused once it has moved, in the write's own transaction.
"""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_protocol import compatible, contract
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_endings import source
from tests.test_turn_withdrawal import (
    MARKER,
    background,
    built,
    check,
    claimed,
    post_turn,
    readable,
    summary,
    summary_generation_now,
    turn,
    withdraw,
)

DM = {'guild_id': '123', 'channel_id': '50', 'conversation_id': 'private:9', 'user_id': '20'}
QUOTING = 'your raid with the '+MARKER


@pytest.fixture
def world(tmp_path):
    settings = Settings(Path(tmp_path/'retention.sqlite3'), '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def summarized(client, store):
    """A summary built on a message in #51: the message's ID and the summary's generation."""
    agent = background(client)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid with the '+MARKER)
    return taken, summary_generation_now(store)


def dm_source(age=1, user='20'):
    return source(age=age, user=user, channel='9', guild=None)


def quoted(store, generation, *, conversation='private:9', user='20', reply=QUOTING, message_id='700', **extra):
    """A delivered reply saved with the summary generation its turn sent (None: an older Fri, or no summary)."""
    said = dm_source(user=user) if conversation.startswith('private:') else source(age=1, user=user, channel='51')
    store.save_chat('123', conversation, user, 0, 'what did i plan', reply, 'model', 'high', source=said,
                    provenance=[], reply_message_id=message_id, verbatim=True, summary_generation=generation)
    return said['id']


def replies(store, conversation='private:9', user='20'):
    return [t['reply'] for t in store.chat_context('123', conversation, user, True, True)['turns']]


def referenced(store, message_id='700'):
    return store.message_sources('123', [message_id]).get(message_id)


def retired(reply):
    return MARKER not in reply and 'older summary' in reply


@pytest.mark.parametrize('conversation', ['private:9', 'public:51'])
def test_a_saved_reply_quoting_a_withdrawn_summary_stops_coming_back(world, conversation):
    """fri #172's repro: in a DM, or the public channel the summary came from."""
    client, store = world
    taken, generation = summarized(client, store)
    quoted(store, generation, conversation=conversation)
    assert QUOTING in replies(store, conversation)
    assert referenced(store)['verbatim'] is True
    assert withdraw(client, taken).json()['removed'] == 1
    assert summary(store) is None
    reply = replies(store, conversation)[-1]
    assert retired(reply)
    assert MARKER not in str(store.chat_context('123', conversation, '20', True, True))
    assert referenced(store)['verbatim'] is False  # A reference to Fri's message fails closed as a breadcrumb's does.
    with store.connection() as db:
        assert MARKER not in str([tuple(row) for row in db.execute('SELECT reply FROM chat_turns')])


def test_every_way_the_summary_goes_retires_what_drew_on_it(world):
    """A rebuild, an expiry, and another member's privacy change taking it, as each moves the generation (gene #97)."""
    from gene.fri_member_summary import TTL, finish, prune
    client, store = world
    _, generation = summarized(client, store)
    agent = client.app.state.agent
    quoted(store, generation, message_id='701')
    readable(store, 'new plans over here', 5000, conversation='public:52')
    readable(store, 'and more of them', 4900, conversation='public:52')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    claim_id, conversations, _ = claimed(agent)
    assert finish(agent, claim_id, 'they made new plans', 'completed', conversations, False) == {'kept': True}
    assert retired(replies(store)[-1])
    newer = summary_generation_now(store)
    quoted(store, newer, reply='your new plans '+MARKER, message_id='702')
    with store.connection() as db:
        db.execute('UPDATE fri_member_summaries SET refreshed=?', (time.time()-TTL-60,))
        prune(db)
    assert all(retired(reply) for reply in replies(store))
    readable(store, 'third round', 3700, conversation='public:52')
    readable(store, 'of plans', 3650, conversation='public:52')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    claim_id, conversations, _ = claimed(agent)
    finish(agent, claim_id, 'they made a third plan', 'completed', conversations, False)
    quoted(store, summary_generation_now(store), reply='your third plan '+MARKER, message_id='703')
    with store.connection() as db:  # It drew on member 30's words too.
        db.execute("UPDATE fri_member_summaries SET provenance=json_array(json_object('user_id','30','revision',0))")
    store.personal_control('30', 'disable')
    assert summary(store) is None and all(retired(reply) for reply in replies(store))


def test_the_members_own_privacy_change_retires_it_where_their_chats_stay(world):
    """A change that takes the summary but leaves a conversation's saved chats, as a note edit elsewhere does."""
    from gene.fri_member_summary import erase
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation)
    with store.connection() as db:
        erase(db, '20')
    assert retired(replies(store)[-1])


def test_what_a_summary_going_leaves_alone(world):
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    readable(store, 'i main a healer', 7300, user='30', conversation='public:52')
    readable(store, 'see you at the raid', 7250, user='30', conversation='public:52')
    built(agent, 'they planned a raid with the '+MARKER)
    built(agent, 'they main a healer')
    generation = summary_generation_now(store)
    quoted(store, None, reply='an older fri said '+MARKER, message_id='710')  # Says nothing: kept as before.
    quoted(store, summary_generation_now(store, '30'), user='30', reply='your healer', message_id='711')
    later = readable(store, 'said after the build', 30)
    quoted(store, generation, reply=QUOTING, message_id='712')
    withdraw(client, later)  # Never read by the summary: nothing moves.
    assert replies(store)[-1] == QUOTING
    withdraw(client, taken)
    assert replies(store) == ['an older fri said '+MARKER, replies(store)[-1]] and retired(replies(store)[-1])
    assert replies(store, user='30') == ['your healer']  # Another member's summary and replies are untouched.
    assert referenced(store, '710')['verbatim'] is True
    after = summary_generation_now(store)
    quoted(store, after, reply='nothing remembered', message_id='713')  # A turn after the drop keeps its reply.
    withdraw(client, taken)  # Again: nothing left to drop.
    assert replies(store)[-1] == 'nothing remembered'


def test_a_restart_retires_what_an_older_controller_left(world):
    """A rollback window: an older controller moved the generation without retiring anything. The next start does."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation)
    quoted(store, None, reply='an older fri said '+MARKER, message_id='701')
    with store.connection() as db:
        db.execute('UPDATE fri_member_summary_generations SET generation=generation+1')
    Store(store.path)
    assert retired(replies(store)[-2]) and replies(store)[-1] == 'an older fri said '+MARKER


def test_a_purge_before_rollback_retires_them_too(world):
    """A controller without summaries can't tell which replies drew on one, so none outlives the purge."""
    from gene.fri_member_summary import purge
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation)
    quoted(store, None, reply='an older fri said '+MARKER, message_id='701')
    purge(store.path)
    with store.connection() as db:  # As the older controller will read it.
        rows = [row['reply'] for row in db.execute('SELECT reply FROM chat_turns WHERE user_id=? ORDER BY id', ('20',))]
    assert retired(rows[-2]) and rows[-1] == 'an older fri said '+MARKER


def work(said, generation=None, **extra):
    return {**DM, 'source': said, 'revision': 0, 'work_id': '', 'version': 0, 'title': 'raid notes',
            'body': 'bring the '+MARKER, 'state': 'open', 'request_text': 'save that as a draft',
            **({} if generation is None else {'summary_generation': generation}), **extra}


def reminder(said, generation=None):
    from tests.test_fri_social import future
    return {**DM, 'source': said, 'revision': 0, 'body': 'bring the '+MARKER, 'local_time': future(),
            'timezone': 'UTC', **({} if generation is None else {'summary_generation': generation})}


def kept(store, table):
    with store.connection() as db:
        return [tuple(row) for row in db.execute(f'SELECT * FROM {table}')]


@pytest.mark.parametrize('route,body,table', [('/workspace/save', work, 'fri_work'),
                                              ('/social/reminders/prepare', reminder, 'fri_reminders')])
def test_a_write_drawing_on_a_withdrawn_summary_is_refused(world, route, body, table):
    """fri #171: the save lands after the summary it drew on was withdrawn."""
    client, store = world
    taken, generation = summarized(client, store)
    withdraw(client, taken)
    refused = client.post(route, headers=headers('fri'), json=body(dm_source(), generation))
    assert refused.status_code == 409 and MARKER not in refused.text
    assert kept(store, table) == []
    current = client.post(route, headers=headers('fri'), json=body(dm_source(2), summary_generation_now(store)))
    assert current.status_code == 200, current.text
    older = client.post(route, headers=headers('fri'), json=body(dm_source(3)))  # An older Fri: as before.
    assert older.status_code == 200, older.text
    assert len(kept(store, table)) == 2


def test_a_retried_work_save_answers_the_same_with_or_without_the_generation(world):
    """The generation stays out of the save's idempotency digest, so a retry across an upgrade or rollback matches."""
    client, store = world
    _, generation = summarized(client, store)
    said = dm_source()
    first = client.post('/workspace/save', headers=headers('fri'), json=work(said, generation))
    assert first.status_code == 200, first.text
    again = client.post('/workspace/save', headers=headers('fri'), json=work(said))
    assert again.status_code == 200 and again.json()['id'] == first.json()['id']


def test_the_contract_offers_summary_generation_writes_as_optional():
    advertised = contract()
    assert 'summary-generation-writes' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities']
                                                     if c != 'summary-generation-writes']})


def test_a_refused_reply_still_refuses_and_a_current_one_saves(world):
    """The turn check and save are as gene #97 left them; only what is kept changes."""
    client, store = world
    taken, generation = summarized(client, store)
    body = turn(source(age=1, channel='51'), 'what did i plan', reply=QUOTING, summary_generation=generation)
    assert check(client, body).json() == {'ok': True}
    withdraw(client, taken)
    assert post_turn(client, body).status_code == 409
    current = {**body, 'source': source(age=2, channel='51'), 'summary_generation': summary_generation_now(store)}
    assert post_turn(client, current).json()['ok'] is True
