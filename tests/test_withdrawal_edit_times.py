"""HumanMint/fri#358 E1: an edit withdrawn by ID with the time Discord says it was made.

Fri asked about an edit by ID only while its message was young, so a fresh edit of an old message left notes drawn from
it usable. It can't ask about every edit with no time: a pin or an embed refresh repeats an earlier edit's time, and the
withdrawal would take notes learned after that edit, from what the message says now. With the edit's time, only what
was certainly saved before the edit goes. What was certainly saved after it stays. What was saved too close to it to
tell which came first (or both before and after) isn't withdrawn: the answer names it as unsure, and Fri keeps the
message out of use instead.
"""
import time

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_protocol import compatible, contract
from tests.test_fri_endings import source
from tests.test_saved_withdrawal import withdraw_saved
from tests.test_turn_withdrawal import inputs
from tests.test_withdrawal_sources import LIKE, chatter, learn, notes

CAPABILITY = 'withdrawal-edit-times'
DAY = 86400


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'edits.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', agent_history_channels='51', agent_role_id='admin',
                        test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def saved_ago(store, seconds):
    """Everything saved so far as if saved `seconds` earlier: its exchanges and its notes, with their sources."""
    with store.connection() as db:
        db.execute('UPDATE chat_turns SET at=at-?', (seconds,))
        db.execute('UPDATE personal_notes SET at=at-?,sources_at=sources_at-?', (seconds, seconds))


def saved_at(store, table):
    with store.connection() as db:
        return db.execute(f'SELECT max(at) FROM {table}').fetchone()[0]


def withdrawals(store):
    with store.connection() as db:
        return [tuple(row) for row in db.execute('SELECT user_id,message_id FROM chat_turn_withdrawals')]


def old_note(store):
    """The issue's setup: a note learned today from a 100-day-old message, its exchange trimmed since."""
    taken, _ = learn(store, LIKE, age=100*DAY)
    chatter(store, 6)
    assert notes(store) == [LIKE]
    return taken


def test_the_controller_offers_edit_times():
    assert CAPABILITY in contract()['capabilities'] and compatible(contract())


def test_the_repro_a_fresh_edit_of_an_old_message_takes_the_note_saved_before_it(world):
    """Saved an hour ago, edited now: the note held what the message said before, so it goes, with its record."""
    client, store = world
    taken = old_note(store)
    saved_ago(store, 3600)
    assert withdraw_saved(client, taken, edited_at=time.time()).json() == {'ok': True, 'removed': 0, 'unsure': []}
    assert notes(store) == [] and withdrawals(store) == [('20', taken)]


def test_a_pin_update_repeating_an_old_edit_time_takes_nothing(world):
    """The message was edited 60 days ago, and the note learned today from what it says since. A pin repeats that
    edit's time: nothing was saved before it, so nothing goes."""
    client, store = world
    taken = old_note(store)
    assert withdraw_saved(client, taken, edited_at=time.time()-60*DAY).json() == {
        'ok': True, 'removed': 0, 'unsure': []}
    assert notes(store) == [LIKE] and withdrawals(store) == []


def test_an_edit_before_the_save_takes_nothing(world):
    """Edited a minute before the note was saved, its event arriving only now: the note is of what it says now."""
    client, store = world
    taken = old_note(store)
    assert withdraw_saved(client, taken, edited_at=saved_at(store, 'personal_notes')-60).json()['unsure'] == []
    assert notes(store) == [LIKE] and withdrawals(store) == []


def test_an_edit_within_seconds_of_the_save_takes_nothing_and_says_so(world):
    """Two seconds after the save, or two before: Discord's clock and this one can't tell which came first. Nothing
    is withdrawn (it may be the very version saved), and the answer names it, for Fri to keep it out of use."""
    client, store = world
    taken = old_note(store)
    at = saved_at(store, 'personal_notes')
    for edited_at in (at+2, at-2):
        assert withdraw_saved(client, taken, edited_at=edited_at).json() == {
            'ok': True, 'removed': 0, 'unsure': [taken]}
        assert notes(store) == [LIKE] and withdrawals(store) == []


@pytest.mark.parametrize('saved', ['before', 'after'])
def test_a_young_messages_exchange_goes_only_if_saved_before_the_edit(world, saved):
    """A young message's own exchange, as an old one's note: saved an hour before the edit, it goes; answered after
    the edit, it stays."""
    client, store = world
    said = source(age=7200, channel='51')
    store.save_chat('123', 'public:51', '20', 0, 'what about lunch', 'okay', 'model', 'high', source=said,
                    provenance=[])
    if saved == 'before':
        saved_ago(store, 3600)
    assert withdraw_saved(client, said['id'], edited_at=time.time()-1800).json() == {
        'ok': True, 'removed': int(saved == 'before'), 'unsure': []}
    assert inputs(store) == ([] if saved == 'before' else ['what about lunch'])


def test_saves_both_before_and_after_the_edit_are_unsure(world):
    """One exchange from before the edit and one answered again after it, under the same ID: withdrawing would take
    both. Neither goes; the answer names the message, for Fri to keep it out of use."""
    client, store = world
    said = source(age=7200, channel='51')
    store.save_chat('123', 'public:51', '20', 0, 'what about lunch', 'okay', 'model', 'high', source=said,
                    provenance=[])
    saved_ago(store, 3600)
    store.save_chat('123', 'public:51', '20', 0, 'what about dinner', 'okay', 'model', 'high', source=said,
                    provenance=[])
    assert withdraw_saved(client, said['id'], edited_at=time.time()-1800).json() == {
        'ok': True, 'removed': 0, 'unsure': [said['id']]}
    assert sorted(inputs(store)) == ['what about dinner', 'what about lunch']


def test_a_deletion_takes_everything_whatever_time_comes_with_it(world):
    client, store = world
    taken = old_note(store)
    assert withdraw_saved(client, taken, deleted=True, edited_at=time.time()-60*DAY).json() == {
        'ok': True, 'removed': 0}
    assert notes(store) == []


def test_without_a_time_an_edit_takes_everything_as_before(world):
    client, store = world
    taken = old_note(store)
    assert withdraw_saved(client, taken).json() == {'ok': True, 'removed': 0}
    assert notes(store) == []


def test_an_edit_time_must_be_a_time(world):
    client, _ = world
    for value in (0, -1, 'yesterday', None):
        response = withdraw_saved(client, source(channel='51')['id'], edited_at=value)
        assert response.status_code == 422 or value is None and response.status_code == 200
