"""fri #172 and fri #171: what Fri keeps from a turn that drew on its summary of the member doesn't outlive that summary.

A saved reply keeps the summary generation its turn sent (gene #91). Once the generation moves, the reply is a neutral
breadcrumb, so a later turn can't read it back as history and repeat it. A tool's write (saved work, a reminder) that
sends the generation is refused once it has moved, in the write's own transaction.
"""

import time
from contextlib import contextmanager
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


def test_a_rebuild_sent_the_old_summary_keeps_its_replies_until_a_withdrawal_reaches_them(world):
    """The daily rebuild that's sent the earlier summary carries its origins and provenance, so Fri's side of a DM stays.
    A withdrawal of what the old summary drew on still finds the new one, and takes the reply with it."""
    from gene.fri_member_summary import finish, settle
    client, store = world
    taken, generation = summarized(client, store)
    agent = client.app.state.agent
    quoted(store, generation, message_id='704')
    readable(store, 'new plans over here', 5000, conversation='public:52')
    readable(store, 'and more of them', 4900, conversation='public:52')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    claim_id, conversations, _ = claimed(agent)
    assert finish(agent, claim_id, 'they made new plans', 'completed', conversations, True) == {'kept': True}
    newer = summary_generation_now(store)
    assert newer != generation and QUOTING in replies(store)
    assert referenced(store, '704')['verbatim'] is True
    with store.connection() as db:
        assert [row[0] for row in db.execute('SELECT summary_generation FROM chat_turns WHERE reply_message_id=?',
                                             ('704',))] == [newer]
        settle(db)  # A restart finds it current.
    assert QUOTING in replies(store)
    assert withdraw(client, taken).json()['removed'] == 1
    assert summary(store) is None and retired(replies(store)[-1])


def test_an_empty_rebuild_retires_the_replies_instead_of_carrying_them_on(world):
    """A rebuild that comes out empty shows later turns no summary, so nothing would tag a reply repeating the old one."""
    from gene.fri_member_summary import finish
    client, store = world
    _, generation = summarized(client, store)
    agent = client.app.state.agent
    quoted(store, generation, message_id='705')
    readable(store, 'new plans over here', 5000, conversation='public:52')
    readable(store, 'and more of them', 4900, conversation='public:52')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    claim_id, conversations, _ = claimed(agent)
    finish(agent, claim_id, '', 'completed', conversations, True)
    assert retired(replies(store)[-1])


def test_a_question_a_summary_derived_turn_left_open_goes_with_it(world):
    """Its words are the model's too, and a later turn reads them back as what's pending."""
    client, store = world
    taken, generation = summarized(client, store)
    asked = {'goal': 'play', 'topic': '', 'question': 'the '+MARKER+' song or another one?'}
    for dm, kept_generation in (('9', generation), ('8', None)):  # The second: an older Fri's turn.
        store.save_chat('123', f'private:{dm}', '20', 0, 'fri play a song', asked['question'], 'model', 'high',
                        source=source(age=1, channel=dm, guild=None), provenance=[], clarification=asked,
                        summary_generation=kept_generation)
        assert store.chat_context('123', f'private:{dm}', '20', True, True)['pending']['question'] == asked['question']
    withdraw(client, taken)
    assert store.chat_context('123', 'private:9', '20', True, True)['pending'] is None
    assert store.chat_context('123', 'private:8', '20', True, True)['pending']['question'] == asked['question']


def test_a_summary_lapsing_as_context_is_read_retires_its_replies_in_that_same_read(world):
    from gene.fri_member_summary import TTL
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation)
    with store.connection() as db:
        db.execute('UPDATE fri_member_summaries SET refreshed=?', (time.time()-TTL-60,))
    context = store.chat_context('123', 'private:9', '20', True, True)
    assert context['member_summary'] is None and retired(context['turns'][-1]['reply'])


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
    with store.connection() as db:  # As gene #97's advance() does, without retiring anything.
        db.execute("INSERT INTO fri_member_summary_generations VALUES ('123','20',1) ON CONFLICT(guild_id, user_id) "
                   'DO UPDATE SET generation=generation+1')
    assert replies(store)[-2] == QUOTING
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


def lapsed(store):
    """The summary expired after a turn read it, with nothing pruning it since: no status check or build claim."""
    from gene.fri_member_summary import TTL
    with store.connection() as db:
        db.execute('UPDATE fri_member_summaries SET refreshed=?', (time.time()-TTL-60,))


def dropped(store, generation):
    """The summary went, moving its generation, and a reply that drew on it keeps only its breadcrumb. Read straight
    from the database: chat context would drop a lapsed summary itself."""
    with store.connection() as db:
        rows = db.execute('SELECT count(*) FROM fri_member_summaries').fetchone()[0]
        kept = [row[0] for row in db.execute('SELECT reply FROM chat_turns')]
    return rows == 0 and summary_generation_now(store) != generation and MARKER not in str(kept)


@pytest.mark.parametrize('route', [check, post_turn])
def test_a_turn_drawing_on_a_summary_that_lapsed_since_is_refused(world, route):
    """gene #113: the turn check and the save find the summary expired but not yet pruned. It goes there, as a read
    that finds it lapsed drops it, and the turn is refused."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation)
    lapsed(store)
    body = turn(source(age=1, channel='51'), 'what did i plan', reply=QUOTING, summary_generation=generation)
    refused = route(client, body)
    assert refused.status_code == 409 and MARKER not in refused.text
    assert dropped(store, generation)
    current = {**body, 'source': source(age=2, channel='51'), 'summary_generation': summary_generation_now(store)}
    assert route(client, current).status_code == 200
    older = {key: value for key, value in body.items() if key != 'summary_generation'}  # An older Fri: as before.
    assert route(client, {**older, 'source': source(age=3, channel='51')}).status_code == 200


@pytest.mark.parametrize('route,body,table', [('/workspace/save', work, 'fri_work'),
                                              ('/social/reminders/prepare', reminder, 'fri_reminders')])
def test_a_write_drawing_on_a_summary_that_lapsed_since_is_refused(world, route, body, table):
    """gene #113: saved work or a reminder can't keep the words of a summary that expired before the write landed."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation)
    lapsed(store)
    refused = client.post(route, headers=headers('fri'), json=body(dm_source(), generation))
    assert refused.status_code == 409 and MARKER not in refused.text
    assert kept(store, table) == [] and dropped(store, generation)
    current = client.post(route, headers=headers('fri'), json=body(dm_source(2), summary_generation_now(store)))
    assert current.status_code == 200, current.text


def test_a_turn_without_a_summary_leaves_a_lapsed_one_to_the_next_read(world):
    """The check of a turn that drew on no summary writes nothing, as before."""
    client, store = world
    _, generation = summarized(client, store)
    lapsed(store)
    assert check(client, turn(source(age=1, channel='51'), 'hello')).json() == {'ok': True}
    assert summary_generation_now(store) == generation


@pytest.fixture
def older(monkeypatch):
    """The controller before gene #112, run in a rollback window: the same privacy changes and drops, retiring
    nothing. Its saves leave the generation column empty."""
    import gene.fri_member_summary as summaries

    @contextmanager
    def running():
        with monkeypatch.context() as patched:
            patched.setattr(summaries, 'retire', lambda *args, **kwargs: None)
            yield
    return running


def note_edited_elsewhere(store, user='20'):
    """A note edit in another of the member's conversations: its chats go and their revision moves; these stay."""
    from gene.fri_memory_store import cleared
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        cleared(store, db, '123', user, 'private:8', store._privacy_state(db, user)[0])


def test_a_restart_retires_a_reply_whose_generation_an_older_controller_erased(world, older):
    """gene #114's repro: a generation-zero reply is saved, the older controller runs and a note edit elsewhere erases
    the summary and the member's generation with it, and the newer controller starts again."""
    client, store = world
    _, generation = summarized(client, store)
    assert generation == 0
    quoted(store, generation)
    quoted(store, None, reply='an older fri said '+MARKER, message_id='701')
    with older():
        note_edited_elsewhere(store)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_member_summary_generations').fetchone()[0] == 0
    assert replies(store)[-2] == QUOTING  # As the older controller left it.
    Store(store.path)
    assert retired(replies(store)[-2]) and replies(store)[-1] == 'an older fri said '+MARKER
    assert referenced(store) is None  # Its revision moved too, so a reference fails closed as before.


def test_a_restart_retires_a_reply_whose_generation_came_back_under_a_newer_revision(world, older):
    """The older controller erased the generation, then a drop there wrote it again at the number the reply carries.
    The erasure moved the member's revision, which the reply was saved under, so it still can't be confirmed."""
    from gene.fri_member_summary import advance, drop
    client, store = world
    summarized(client, store)
    with store.connection() as db:
        advance(db, '123', '20')
    quoted(store, summary_generation_now(store))
    assert summary_generation_now(store) == 1
    with older():
        note_edited_elsewhere(store)
        with store.connection() as db:  # Another build there, dropped: its generation starts again at one.
            drop(db, '123', '20')
    assert summary_generation_now(store) == 1 and replies(store)[-1] == QUOTING
    Store(store.path)
    assert retired(replies(store)[-1])


def test_an_upgrade_keeps_a_reply_on_a_first_summary_an_older_controller_saved(world):
    """The controller before gene #118 recorded no generation for a summary that never moved, so a reply saved on a
    member's first summary has none on record. An upgrade starts from that state: the reply still holds. An erasure
    that deleted a generation moved the member's revision, which still retires (see the test above)."""
    client, store = world
    _, generation = summarized(client, store)
    assert generation == 0
    quoted(store, generation, message_id='707')
    with store.connection() as db:  # As the older controller left it: generation zero was never written.
        db.execute('DELETE FROM fri_member_summary_generations')
    Store(store.path)
    assert replies(store)[-1] == QUOTING and referenced(store, '707')['verbatim'] is True


def test_a_plain_restart_keeps_current_summary_derived_replies(world):
    """No rollback in between: a generation-zero reply, and one a rebuild sent the old summary carried forward."""
    from gene.fri_member_summary import finish
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation, message_id='706')
    Store(store.path)
    assert replies(store)[-1] == QUOTING and referenced(store, '706')['verbatim'] is True
    readable(store, 'new plans over here', 5000, conversation='public:52')
    readable(store, 'and more of them', 4900, conversation='public:52')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    claim_id, conversations, _ = claimed(client.app.state.agent)
    assert finish(client.app.state.agent, claim_id, 'they made new plans', 'completed', conversations, True)['kept']
    Store(store.path)
    assert replies(store)[-1] == QUOTING and referenced(store, '706')['verbatim'] is True


def sources(client, *message_ids):
    answer = client.post('/chat/messages/sources', headers=headers('fri'),
                         json={'guild_id': '123', 'message_ids': list(message_ids)})
    assert answer.status_code == 200, answer.text
    return answer.json()['messages']


def test_a_reference_to_a_reply_on_a_summary_that_lapsed_since_is_retired(world):
    """gene #120: the sources lookup finds the summary a reply drew on expired but not yet pruned. It goes there, as a
    read that finds it lapsed drops it, so the reply is reported as a breadcrumb, not verbatim, and stays one."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation, conversation='public:51')
    quoted(store, None, reply='an older fri said hi', message_id='701', conversation='public:51')
    assert sources(client, '700')['700']['verbatim'] is True
    assert summary_generation_now(store) == generation  # A current summary stays.
    lapsed(store)
    found = sources(client, '700', '701')
    assert found['700']['verbatim'] is False and found['701']['verbatim'] is True
    assert dropped(store, generation)
    assert sources(client, '700')['700']['verbatim'] is False


def traced(store, monkeypatch):
    """The statements the store runs from here on."""
    ran, opened = [], store.connection

    @contextmanager
    def connection():
        with opened() as db:
            db.set_trace_callback(ran.append)
            yield db
    monkeypatch.setattr(store, 'connection', connection)
    return ran


def test_a_reference_to_a_reply_that_drew_on_no_summary_stays_a_plain_read(world, monkeypatch):
    """Only a reply that drew on a summary takes the write lock: the lookup of any other reads, as before, and leaves a
    lapsed summary to the next read."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, None, reply='an older fri said hi', message_id='701')
    lapsed(store)
    ran = traced(store, monkeypatch)
    assert sources(client, '701', '702') == {'701': {'verbatim': True, 'provenance': [{'user_id': '20', 'revision': 0}]}}
    assert ran and all(statement.lstrip().upper().startswith('SELECT') for statement in ran), ran
    monkeypatch.undo()
    assert summary_generation_now(store) == generation
