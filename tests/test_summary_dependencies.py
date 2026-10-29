"""fri #193: a reply of Fri's that repeated another reply of its drawn on a member's summary (an echo it saw in the
channel, a reply of Fri's a member answered) depends on that summary too.

A turn sends those summaries' generations as `summary_dependencies`. It's refused once one moves, as a turn that drew
on the member's own summary is (gene #91), and its saved reply keeps only a breadcrumb once that summary goes (fri #172),
so a later reference or echo of it can't bring the withdrawn words back to another member's request. The sources lookup
reports each reply's dependencies, so a turn answering one carries them on.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_protocol import compatible, contract
from gene.store import Store
from tests.test_fri_endings import source
from tests.test_summary_generation_retention import (
    lapsed,
    quoted,
    retired,
    sources,
    summarized,
)
from tests.test_turn_withdrawal import (
    MARKER,
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

ECHOED = 'like they said, the raid with the '+MARKER


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'dependencies.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def depending(generation, user='20'):
    return [{'user_id': user, 'generation': generation}]


def repeated(client, generation, *, user='30', message_id='800', provenance=None, age=1, on='20'):
    """Another member's turn in #51 whose reply repeated a summary-derived reply (member `on`'s), saved through the API."""
    body = turn(source(age=age, user=user, channel='51'), 'what did they plan', user=user, reply=ECHOED,
                reply_message_id=message_id, verbatim=True, summary_dependencies=depending(generation, on),
                provenance=[{'user_id': on, 'revision': 0}] if provenance is None else provenance)
    saved = post_turn(client, body)
    assert saved.status_code == 200, saved.text
    return body


def rebuilt(client, store):
    """Member 20's summary built again after it went: its generation."""
    readable(store, 'raid again, bring the '+MARKER, 7200)
    readable(store, 'same build again', 7150)
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    built(client.app.state.agent, 'they planned another raid with the '+MARKER)
    return summary_generation_now(store)


def saved_reply(store, message_id='800'):
    with store.connection() as db:
        return db.execute('SELECT reply FROM chat_turns WHERE reply_message_id=?', (message_id,)).fetchone()[0]


def test_a_reply_repeating_a_summary_derived_one_goes_when_that_summary_does(world):
    """Item 1's repro: A's summary shapes an echo, B's reply repeats it, then A's source is withdrawn."""
    client, store = world
    taken, generation = summarized(client, store)
    quoted(store, generation, conversation='public:51')
    repeated(client, generation)
    found = sources(client, '700', '800')
    assert found['800']['verbatim'] is True and found['800']['summaries'] == depending(generation)
    assert found['700']['summaries'] == depending(generation)  # Its own summary, for a turn answering it to carry.
    assert withdraw(client, taken).json()['removed'] == 1
    found = sources(client, '700', '800')
    assert found['700']['verbatim'] is False and found['800']['verbatim'] is False
    assert 'summaries' not in found['800']
    assert retired(saved_reply(store)) and MARKER not in str(store.chat_context('123', 'public:51', '30', True, True))


def test_a_turn_carrying_a_dependency_is_refused_once_it_moves(world):
    """Items 2 and 4: a reply target or an echo whose summary went while the model wrote."""
    client, store = world
    taken, generation = summarized(client, store)
    body = turn(source(age=1, user='30', channel='51'), 'what did they plan', user='30', reply=ECHOED,
                provenance=[{'user_id': '20', 'revision': 0}], summary_dependencies=depending(generation))
    assert check(client, body).json() == {'ok': True}
    assert withdraw(client, taken).json()['removed'] == 1
    for _ in range(2):
        refused = check(client, body)
        assert refused.status_code == 409 and MARKER not in refused.text
        assert post_turn(client, body).status_code == 409
    assert ECHOED not in str(store.chat_context('123', 'public:51', '30', True, True))


def test_a_dependency_on_a_summary_that_lapsed_since_is_refused_and_retired(world):
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    lapsed(store)
    assert sources(client, '800')['800']['verbatim'] is False  # The lookup finds it lapsed, as for its own (gene #120).
    assert summary_generation_now(store) != generation and retired(saved_reply(store))
    generation = rebuilt(client, store)
    body = turn(source(age=1, user='30', channel='51'), 'hm', user='30', summary_dependencies=depending(generation),
                provenance=[{'user_id': '20', 'revision': 0}])
    lapsed(store)
    assert check(client, body).status_code == 409
    assert summary_generation_now(store) != generation


def test_a_rebuild_sent_the_old_summary_carries_its_dependents_on(world):
    """The daily rebuild keeps the old summary's lineage, so a reply that depended on it stays and now depends on the
    new one, which a withdrawal of what the old one drew on still takes."""
    from gene.fri_member_summary import finish
    client, store = world
    taken, generation = summarized(client, store)
    repeated(client, generation)
    readable(store, 'new plans over here', 5000, conversation='public:52')
    readable(store, 'and more of them', 4900, conversation='public:52')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    claim_id, conversations, _ = claimed(client.app.state.agent)
    assert finish(client.app.state.agent, claim_id, 'they made new plans', 'completed', conversations, True)['kept']
    newer = summary_generation_now(store)
    assert newer != generation and saved_reply(store) == ECHOED
    assert sources(client, '800')['800'] == {'verbatim': True, 'summaries': depending(newer), 'provenance': [
        {'user_id': '20', 'revision': 0}, {'user_id': '30', 'revision': 0}]}
    Store(store.path)  # A restart finds it current.
    assert saved_reply(store) == ECHOED
    assert withdraw(client, taken).json()['removed'] == 1
    assert retired(saved_reply(store))


def test_an_empty_rebuild_retires_dependents(world):
    from gene.fri_member_summary import finish
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    readable(store, 'nothing much', 5000, conversation='public:52')
    readable(store, 'really nothing', 4900, conversation='public:52')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    claim_id, conversations, _ = claimed(client.app.state.agent)
    finish(client.app.state.agent, claim_id, '', 'completed', conversations, False)
    assert retired(saved_reply(store))


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_the_summarized_members_privacy_change_retires_dependents(world, erase):
    """Even one whose provenance didn't name them, which their revision alone wouldn't reach."""
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation, provenance=[])
    if erase == 'disable':
        store.personal_control('20', 'disable')
    else:
        getattr(store, erase)('20')
    assert summary(store) is None and retired(saved_reply(store))


def test_what_a_summary_going_leaves_alone(world):
    """Another member's summary's dependents, an ordinary reply, and the member's own words."""
    client, store = world
    taken, generation = summarized(client, store)
    readable(store, 'i main a healer', 7300, user='40', conversation='public:52')
    readable(store, 'see you at the raid', 7250, user='40', conversation='public:52')
    built(client.app.state.agent, 'they main a healer')
    theirs = summary_generation_now(store, '40')
    repeated(client, theirs, message_id='801', age=3, on='40')
    plain = turn(source(age=2, user='30', channel='51'), 'hi', user='30', reply='hey', reply_message_id='802',
                 verbatim=True)
    assert post_turn(client, plain).status_code == 200
    repeated(client, generation)
    withdraw(client, taken)
    found = sources(client, '800', '801', '802')
    assert found['800']['verbatim'] is False
    assert found['801']['verbatim'] is True and found['801']['summaries'] == depending(theirs, '40')
    assert found['802'] == {'verbatim': True, 'provenance': [{'user_id': '30', 'revision': 0}]}  # As before.
    assert 'what did they plan' in str(store.chat_context('123', 'public:51', '30', True, True)['turns'])


def test_a_restart_retires_dependents_an_older_controller_left(world):
    """A rollback window: the older controller moved or erased the generation without retiring dependents."""
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation, message_id='800', age=3)
    repeated(client, generation, user='40', message_id='801', age=2)
    with store.connection() as db:  # As an older controller's drop does, retiring only member 20's own.
        db.execute("UPDATE fri_member_summary_generations SET generation=generation+1 WHERE user_id='20'")
    assert saved_reply(store) == ECHOED
    Store(store.path)
    assert retired(saved_reply(store)) and retired(saved_reply(store, '801'))
    generation = rebuilt(client, store)
    repeated(client, generation, message_id='803', age=1)
    with store.connection() as db:  # As its erasure does: the generation goes outright.
        db.execute("DELETE FROM fri_member_summary_generations WHERE user_id='20'")
    Store(store.path)
    assert retired(saved_reply(store, '803'))


def test_a_restart_retires_a_dependent_whose_speakers_revision_moved(world):
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation, provenance=[])
    with store.connection() as db:  # Member 30's privacy changed on an older controller, which left the reply.
        db.execute("INSERT INTO chat_revisions VALUES ('30',1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1")
    Store(store.path)
    assert retired(saved_reply(store))


def test_a_plain_restart_keeps_current_dependents(world):
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    Store(store.path)
    assert saved_reply(store) == ECHOED and sources(client, '800')['800']['verbatim'] is True


def test_a_purge_before_rollback_retires_dependents_too(world):
    from gene.fri_member_summary import purge
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    purge(store.path)
    assert retired(saved_reply(store))


def test_dependencies_are_checked_and_bounded(world):
    client, store = world
    base = turn(source(age=1, user='30', channel='51'), 'hi', user='30')
    for dependencies in ([{'user_id': 'x', 'generation': 0}], [{'user_id': '20', 'generation': -1}],
                         [{'user_id': '20', 'generation': 0, 'text': MARKER}],
                         [{'user_id': str(i), 'generation': 0} for i in range(1, 202)]):
        assert check(client, {**base, 'summary_dependencies': dependencies}).status_code == 422
    assert check(client, base).json() == {'ok': True}  # An older Fri sends none, and is checked as before.
    assert check(client, {**base, 'summary_dependencies': depending(0)}).json() == {'ok': True}  # Nothing yet: zero.


def test_a_save_records_the_generation_it_depends_on(world):
    """As a reply on the member's own summary does (gene #114): a start that finds none knows an older one erased it."""
    client, store = world
    repeated(client, 0)
    with store.connection() as db:
        assert tuple(db.execute("SELECT generation FROM fri_member_summary_generations WHERE user_id='20'").fetchone()) == (0,)


def test_what_the_reply_model_reads_is_unchanged(world):
    """The dependencies stay out of chat context: history turns show what they always did."""
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    turns = store.chat_context('123', 'public:51', '30', True, True)['turns']
    assert turns and all('summary_dependencies' not in t and 'summaries' not in t for t in turns)


def test_the_contract_offers_summary_dependencies_as_optional():
    advertised = contract()
    assert 'summary-dependencies' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities']
                                                      if c != 'summary-dependencies']})


def test_nothing_logged_holds_the_words(world, caplog):
    caplog.set_level(logging.DEBUG)
    client, store = world
    taken, generation = summarized(client, store)
    repeated(client, generation)
    withdraw(client, taken)
    sources(client, '800')
    assert MARKER not in caplog.text
