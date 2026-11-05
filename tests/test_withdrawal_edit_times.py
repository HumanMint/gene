"""HumanMint/fri#358 E1: an edit withdrawn by ID with the time Discord says it was made.

Fri asked about an edit by ID only while its message was young, so a fresh edit of an old message left notes drawn from
it usable. It can't ask about every edit with no time: a pin or an embed refresh repeats an earlier edit's time, and the
withdrawal would take notes learned after that edit, from what the message says now.

Each save records which version of its messages it was made from, on Discord's clock (the edit time, or the creation
time of one never edited), and an edit's time is compared with that, never with when the controller saved it, as the
controller's clock may run seconds apart from Discord's either way (Sol round 1 of fri #364, B2). A save from an earlier
version goes; one from that version or a later one stays. A message with saves of both kinds, or a save that recorded
no version of an old message, keeps everything: the answer names it as unsure, for Fri to keep it out of use, and a
summary of its author's that may have drawn on it is held back until it's read again (B1).
"""
import json
import time

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_member_summary import read
from gene.fri_protocol import compatible, contract
from tests.test_api import headers
from tests.test_fri_endings import source
from tests.test_fri_learning import learning  # noqa: F401 (the fixture)
from tests.test_saved_withdrawal import withdraw_saved
from tests.test_turn_withdrawal import inputs
from tests.test_withdrawal_sources import LIKE, chatter, learn, notes

CAPABILITY = 'withdrawal-edit-times'
DAY = 86400
SKEWS = [-8, 0, 8]  # The controller's clock behind Discord's, with it, and ahead, by more than any window (B2).


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'edits.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', agent_history_channels='51', agent_role_id='admin',
                        test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def skewed(monkeypatch, seconds):
    """The controller's clock `seconds` off Discord's from now on. Discord's times are read before this."""
    real = time.time
    monkeypatch.setattr(time, 'time', lambda: real()+seconds)


def created(message_id):
    return ((int(message_id) >> 22)+1420070400000)/1000


def note_from(store, *, age, version=None, versioned=True):
    """A note learned now from member 20's message sent `age` seconds ago, its exchange trimmed since, saved from
    `version` of it (its creation time by default), or recording none."""
    said = source(age=age, channel='51')
    seen = created(said['id']) if version is None else version
    learn(store, LIKE, source=said, **({'versions': {said['id']: seen}} if versioned else {}))
    chatter(store, 6)
    assert notes(store) == [LIKE]
    return said['id']


def withdrawals(store):
    with store.connection() as db:
        return [tuple(row) for row in db.execute('SELECT user_id,message_id FROM chat_turn_withdrawals')]


def versions(store):
    with store.connection() as db:
        return [tuple(row) for row in db.execute('SELECT kind,message_id,version FROM fri_source_versions '
                                                 'ORDER BY kind,message_id')]


def test_the_controller_offers_edit_times():
    assert CAPABILITY in contract()['capabilities'] and compatible(contract())


@pytest.mark.parametrize('skew', SKEWS)
def test_the_repro_a_fresh_edit_of_an_old_message_takes_the_note_saved_from_before_it(world, monkeypatch, skew):
    """The issue: a note learned today from a 100-day-old message, which its author then edits."""
    client, store = world
    taken = note_from(store, age=100*DAY)
    edited = time.time()
    skewed(monkeypatch, skew)
    assert withdraw_saved(client, taken, edited_at=edited).json() == {'ok': True, 'removed': 0, 'unsure': []}
    assert notes(store) == [] and withdrawals(store) == [('20', taken)]


@pytest.mark.parametrize('skew', SKEWS)
@pytest.mark.parametrize('versioned', [True, False])
def test_b2_an_edit_a_second_after_the_save_takes_it_whatever_the_controllers_clock(world, monkeypatch, skew,
                                                                                   versioned):
    """Sol round 1's B2: saved with the controller's clock 8 s ahead of Discord's, then edited a second later on
    Discord's clock. Placed against the save's time, the edit looked older and the stale note stayed usable. Placed
    against the version of the message it was saved from, it's later, so the note goes, whichever way the clock is;
    a save that recorded no version of this young message goes too, as before."""
    client, store = world
    skewed(monkeypatch, skew)
    taken = note_from(store, age=10, versioned=versioned)
    edited = created(taken)+11  # Discord's clock, a second after the save.
    assert withdraw_saved(client, taken, edited_at=edited).json()['unsure'] == []
    assert notes(store) == []


@pytest.mark.parametrize('skew', SKEWS)
@pytest.mark.parametrize('when', ['a pin repeating the edit the note was learned from',
                                  'a late event of an edit before that one'])
def test_an_edit_no_later_than_the_version_saved_takes_nothing(world, monkeypatch, skew, when):
    """The message was edited 60 days ago, and the note learned today from what it says since: a pin repeats that
    edit's time, or an earlier edit's event arrives late. Nothing goes."""
    client, store = world
    version = time.time()-60*DAY
    taken = note_from(store, age=100*DAY, version=version)
    skewed(monkeypatch, skew)
    edited = version if when.startswith('a pin') else version-3600
    assert withdraw_saved(client, taken, edited_at=edited).json() == {'ok': True, 'removed': 0, 'unsure': []}
    assert notes(store) == [LIKE] and withdrawals(store) == []


@pytest.mark.parametrize('age,how', [(100*DAY, 'unsure'), (10*DAY, 'withdraw')])
def test_a_save_that_recorded_no_version_is_placed_as_before(world, age, how):
    """From an older Fri, or saved before versions were: an old message's edit can't be placed against it, so nothing
    goes and the answer says so; a young one's goes, as an edit with no time took it before."""
    client, store = world
    taken = note_from(store, age=age, versioned=False)
    answer = withdraw_saved(client, taken, edited_at=time.time()).json()
    assert answer['unsure'] == ([taken] if how == 'unsure' else [])
    assert notes(store) == ([LIKE] if how == 'unsure' else [])


def test_saves_of_an_earlier_and_a_later_version_are_unsure(world):
    """Its exchange answered before an edit and again after it, under the same ID: a withdrawal by ID would take both.
    Neither goes; the answer names the message."""
    client, store = world
    said, edited = source(age=7200, channel='51'), time.time()-60
    for text, version in (('what about lunch', created(said['id'])), ('what about dinner', edited)):
        store.save_chat('123', 'public:51', '20', 0, text, 'okay', 'model', 'high', source=said, provenance=[],
                        versions={said['id']: version})
    answer = withdraw_saved(client, said['id'], edited_at=edited).json()
    assert answer == {'ok': True, 'removed': 0, 'unsure': [said['id']]}
    assert sorted(inputs(store)) == ['what about dinner', 'what about lunch']


@pytest.mark.parametrize('version', ['before', 'same'])
def test_a_young_messages_exchange_goes_only_if_saved_from_an_earlier_version(world, version):
    client, store = world
    said = source(age=7200, channel='51')
    edited = time.time()-1800
    store.save_chat('123', 'public:51', '20', 0, 'what about lunch', 'okay', 'model', 'high', source=said,
                    provenance=[], versions={said['id']: created(said['id']) if version == 'before' else edited})
    assert withdraw_saved(client, said['id'], edited_at=edited).json() == {
        'ok': True, 'removed': int(version == 'before'), 'unsure': []}
    assert inputs(store) == ([] if version == 'before' else ['what about lunch'])


def summary_drawing_on(store, user, messages):
    """A kept summary of the member's, built from those messages (None: one whose sources aren't known)."""
    now = time.time()
    with store.connection() as db:
        db.execute("INSERT INTO fri_member_summaries VALUES ('123',?,'they planned a raid',0,'[]','[\"public:51\"]',"
                   '?,?)', (user, now, now))
        if messages is not None:
            db.execute("INSERT INTO fri_member_summary_sources VALUES ('123',?,?,?)", (user, now, json.dumps(
                [{'id': message, 'conversation_id': 'public:51', 'saved_at': now} for message in messages])))


def shown(store, user):
    with store.connection() as db:
        return read(db, '123', user) is not None


@pytest.mark.parametrize('sources', ['naming it', 'not known', 'naming another'])
def test_b1_an_unsure_message_holds_back_its_authors_summary_that_may_have_drawn_on_it(world, sources):
    """Sol round 1's B1: nothing of an unsure message is withdrawn, and Fri holds its ID back, but a summary built from
    it names no such ID in a later request elsewhere, so it carried what the edit took away. Its author's summary is
    held back until it's read again, where it drew on that message or its sources aren't known. Another member's
    summary, and one that drew on other messages, stay."""
    client, store = world
    taken = note_from(store, age=100*DAY, versioned=False)
    other = source(age=200, channel='51')['id']
    summary_drawing_on(store, '20', {'naming it': [taken], 'not known': None, 'naming another': [other]}[sources])
    summary_drawing_on(store, '30', [taken])
    assert shown(store, '20') and shown(store, '30')
    assert withdraw_saved(client, taken, edited_at=time.time()).json()['unsure'] == [taken]
    assert shown(store, '20') is (sources == 'naming another')
    assert shown(store, '30')  # Its saves aren't member 30's.


def test_a_deletion_takes_everything_whatever_time_comes_with_it(world):
    client, store = world
    taken = note_from(store, age=100*DAY, version=time.time())
    assert withdraw_saved(client, taken, deleted=True, edited_at=time.time()-60*DAY).json() == {
        'ok': True, 'removed': 0}
    assert notes(store) == []


def test_without_a_time_an_edit_takes_everything_as_before(world):
    client, store = world
    taken = note_from(store, age=100*DAY, version=time.time())
    assert withdraw_saved(client, taken).json() == {'ok': True, 'removed': 0}
    assert notes(store) == []


def test_versions_go_with_their_exchange_or_note_whichever_controller_changes_it(world):
    """A trimmed exchange takes its versions; a note saved again holds the new save's; one an older controller saves
    again (it knows nothing of versions) holds none, so it's placed as before."""
    client, store = world
    said = source(age=7200, channel='51')
    first = created(said['id'])
    learn(store, LIKE, source=said, versions={said['id']: first})
    assert versions(store) == [('note', said['id'], first), ('turn', said['id'], first)]
    chatter(store, 6)
    assert versions(store) == [('note', said['id'], first)]
    learn(store, LIKE, source=said, versions={said['id']: first+60})
    chatter(store, 6)
    assert versions(store) == [('note', said['id'], first+60)]
    with store.connection() as db:
        db.execute('UPDATE personal_notes SET at=at+1,sources_at=sources_at+1')
    assert versions(store) == []


def test_a_turn_names_its_versions_over_the_api(world):
    client, store = world
    said = source(age=10, channel='51')
    body = {'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51', 'user_id': '20', 'revision': 0,
            'source': said, 'input': 'i like lavender bicycles', 'reply': 'okay', 'model': 'model', 'effort': 'high',
            'provenance': []}
    for bad in ({'x': 1.0}, {said['id']: 0}, {said['id']: -1.0}, {said['id']: 'yesterday'}):
        assert client.post('/chat/turn', headers=headers('fri'), json={**body, 'versions': bad}).status_code == 422
    other = source(age=20, channel='51')['id']  # Not one of the turn's own: not recorded.
    answer = client.post('/chat/turn', headers=headers('fri'), json={
        **body, 'versions': {said['id']: created(said['id']), other: 5.0}})
    assert answer.status_code == 200, answer.text
    assert versions(store) == [('turn', said['id'], created(said['id']))]


def test_an_edit_time_must_be_a_time(world):
    client, _ = world
    for value in (0, -1, 'yesterday', None):
        response = withdraw_saved(client, source(channel='51')['id'], edited_at=value)
        assert response.status_code == 422 or value is None and response.status_code == 200


def test_a_learning_pass_records_the_version_each_note_was_learned_from(learning):  # noqa: F811
    """A note learned by a pass is placed against an edit by the version of the message the pass read, which its
    finish names beside the records (the model reads the records, not these)."""
    from dataclasses import replace

    from gene.fri_agent_store import AgentStore, snowflake_floor
    clock, store, settings, _ = learning
    settings = replace(settings, agent_history_channels='1')
    scheduler, started = AgentStore(store, settings), clock[0]-7200
    with store.connection() as db:
        scheduler.put(db, 'started', started)
        scheduler.put(db, 'cursors', {'1': {'before': '100', 'done_at': clock[0]-3600}})
    claim = scheduler.claim()
    said = str(int(snowflake_floor(started+600))+1)
    records = [{'id': said, 'user_id': '11', 'role_id': '90', 'revision': 0, 'text': 'I love trance',
                'at': started+600}]
    result = {'summary': 'Music chat', 'notes': [{'source_id': said, 'category': 'music', 'evidence': 'I love trance'}]}
    scheduler.finish(claim['id'], records, result, 1, said, True, {}, newest_id=said, versions={said: started+900})
    assert store.chat_context('123', 'public:1', '11')['personal_memory']['notes']
    assert versions(store) == [('note', said, started+900)]
