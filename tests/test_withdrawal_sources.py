"""gene #141: what a withdrawn message's words reached besides its own exchange goes with it.

1. A personal note learned from it, wherever it shows (a DM, the conversation, the member's profile), and in My memory.
   Each note records the messages it was learned from; one saved before that (or by an older controller) goes when
   the withdrawn exchange holds its words. Nothing learns it again: a late save or a learning batch already reading
   it keeps nothing.
2. Another member's saved reply whose model request carried it (`drew_on`, from a Fri offering withdrawal-sources):
   once it's deleted, that reply keeps only a breadcrumb, as one that drew on a withdrawn summary does, and so does one
   that carried Fri's reply to it, or a reply of Fri's that did. An edit leaves those replies, as before (gene #150).

Chat context names the messages a note or an exchange elsewhere came from, beside them, for a Fri that asks, so it can
keep them out of a request while a withdrawal is still on its way (fri #215).
"""
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient

from gene import store as controller
from gene.api import create_app
from gene.config import Settings
from gene.fri_agent_store import AgentStore
from gene.fri_memory_store import entries
from gene.fri_protocol import compatible, contract
from gene.store import Conflict, Store
from tests.test_api import headers
from tests.test_fri_endings import source
from tests.test_saved_withdrawal import withdraw_saved
from tests.test_turn_withdrawal import MARKER, inputs, withdraw

LIKE = 'i like lavender bicycles'
CAPABILITY = 'withdrawal-sources'


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'sources.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', agent_history_channels='51', agent_role_id='admin',
                        test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    client.settings = settings
    return client, client.app.state.store


def chatter(store, count, *, start=50):
    for i in range(count):
        store.save_chat('123', 'public:51', '20', 0, f'line {i}', 'okay', 'model', 'high',
                        source=source(age=start-i, channel='51'), provenance=[])


def learn(store, text, *, user='20', conversation='public:51', age=0, evidence=None, category='hobbies', revision=0,
          **extra):
    said = extra.pop('source', None) or source(age=age, user=user, channel=conversation.split(':')[1])
    result = store.save_chat('123', conversation, user, revision, text, 'okay', 'model', 'high', source=said,
                             provenance=extra.pop('provenance', []),
                             notes=[{'category': category, 'evidence': evidence or text}], **extra)
    return said['id'], result


def notes(store, user='20', conversation='public:51'):
    return [note['evidence'] for note in store.chat_context('123', conversation, user)['personal_memory']['notes']]


def listed(store, user='20'):
    """My memory, from the member's DM: every note of theirs anywhere."""
    return [row['evidence'] for row in entries(store, '123', 'private:52', user)['entries']]


@pytest.mark.parametrize('conversation, audience', [('public:51', 'conversation'), ('public:51', 'server'),
                                                    ('private:52', 'conversation')])
def test_a_note_learned_from_a_withdrawn_exchange_goes_with_it(world, conversation, audience):
    client, store = world
    taken, result = learn(store, LIKE, conversation=conversation, audience=audience, age=30)
    assert result['first_memory'] and LIKE in notes(store, conversation=conversation) and LIKE in listed(store)
    kept, _ = learn(store, 'i play chess on weekends', conversation=conversation, audience=audience, age=20)
    channel = conversation.split(':')[1]
    assert withdraw(client, taken, conversation_id=conversation, channel_id=channel).json()['removed'] == 1
    assert notes(store, conversation=conversation) == ['i play chess on weekends']  # Only the one it taught.
    assert listed(store) == ['i play chess on weekends']
    if audience == 'server':  # Nor in the member's other conversations, which their profile reached.
        assert notes(store, conversation='public:52') == ['i play chess on weekends']


def test_by_id_alone_too_even_once_its_exchange_was_trimmed(world):
    """fri #174's withdrawal by ID: a note lasts 90 days, an exchange six to a conversation. One whose exchange was
    trimmed goes all the same, and its message is fenced, so nothing reading it now learns it again. Nothing else
    changes: no exchange of theirs, and no summary."""
    client, store = world
    taken, _ = learn(store, LIKE, age=60)
    chatter(store, 6)
    assert LIKE not in str(store.chat_context('123', 'public:51', '20')['turns']) and LIKE in notes(store)
    assert withdraw_saved(client, taken).json() == {'ok': True, 'removed': 0}
    assert notes(store) == [] and len(inputs(store)) == 6
    with store.connection() as db:
        assert [tuple(r) for r in db.execute('SELECT user_id,message_id FROM chat_turn_withdrawals')] == [('20', taken)]
    assert withdraw_saved(client, source(age=5, channel='51')['id']).json()['removed'] == 0  # An unrelated one: nothing.


def test_the_scoped_withdrawal_takes_a_note_whose_exchange_was_trimmed(world):
    client, store = world
    taken, _ = learn(store, LIKE, age=60)
    chatter(store, 6)
    assert withdraw(client, taken).json()['removed'] == 0
    assert notes(store) == []


def test_a_note_said_again_in_a_message_still_there_stays(world):
    client, store = world
    first, _ = learn(store, LIKE, age=30)
    again, _ = learn(store, LIKE, age=10)  # The same words later: the note now comes from this one.
    withdraw(client, first)
    assert notes(store) == [LIKE]
    withdraw(client, again)
    assert notes(store) == []


def test_another_members_note_stays(world):
    client, store = world
    taken, _ = learn(store, LIKE, age=30)
    learn(store, LIKE, user='30', age=20)
    withdraw(client, taken)
    assert notes(store) == [] and notes(store, '30') == [LIKE]


def test_a_note_of_unknown_source_stays_as_on_main(world):
    """Saved before notes named their messages, or by an older controller during a rollback: which message supplied it
    can't be told (an older controller may have learned it again from another), so it stays, as on main, rather than
    risk deleting one another message still says (gene #145 round 4). It expires as before."""
    client, store = world
    taken, _ = learn(store, 'yo '+LIKE+' a lot', evidence=LIKE, age=30)
    learn(store, 'i play chess on weekends', age=20)
    learn(store, 'i love trance', category='music', age=10)
    with store.connection() as db:
        db.execute('UPDATE personal_notes SET sources=NULL')
    withdraw(client, taken)
    assert sorted(notes(store)) == sorted(['i love trance', 'i play chess on weekends', LIKE])


def test_a_shared_note_still_names_its_message(world):
    """Moved into the profile by the member's own press, or kept back in a DM: the same words, from the same message."""
    client, store = world
    taken, _ = learn(store, LIKE, age=30)
    note = next(row for row in entries(store, '123', 'private:52', '20')['entries'] if row['evidence'] == LIKE)
    from gene.fri_memory_store import edit
    edit(store, '123', 'private:52', '20', note['id'], 0, source(channel='52'), 'share')
    assert notes(store, conversation='public:52') == [LIKE]  # In the profile now.
    withdraw(client, taken)
    assert listed(store) == []


def test_a_replaced_note_holds_the_members_new_words_and_stays(world):
    client, store = world
    taken, _ = learn(store, LIKE, age=30)
    note = next(row for row in entries(store, '123', 'private:52', '20')['entries'] if row['evidence'] == LIKE)
    from gene.fri_memory_store import edit
    edit(store, '123', 'public:51', '20', note['id'], 0, source(channel='51'), 'replace', 'i like red bicycles')
    store.save_chat('123', 'public:51', '20', 1, 'i like red bicycles', 'okay', 'model', 'high',
                    source=source(age=2, channel='51'), provenance=[])  # The edit moved their revision.
    withdraw(client, taken)
    assert notes(store) == ['i like red bicycles']


def test_a_late_save_of_the_withdrawn_message_learns_nothing(world):
    """Guard: its save still on the way when the withdrawal landed keeps nothing (gene #90), notes included."""
    client, store = world
    said = source(age=5, channel='51')
    withdraw(client, said['id'])
    _, result = learn(store, LIKE, source=said)
    assert result['kept'] is False and notes(store) == []


def test_a_learning_batch_reading_it_learns_nothing_from_it(world):
    client, store = world
    agent = AgentStore(store, client.settings)
    agent.update({'enabled': True}, 0, '9')
    claim = agent.claim()
    taken, kept = source(age=200, channel='51'), source(age=190, channel='51')
    records = [{'id': taken['id'], 'user_id': '20', 'role_id': 'admin', 'revision': 0, 'text': LIKE,
                'at': time.time()-200},
               {'id': kept['id'], 'user_id': '20', 'role_id': 'admin', 'revision': 0,
                'text': 'i play chess on weekends', 'at': time.time()-190}]
    withdraw(client, taken['id'])  # While the batch was reading it.
    agent.finish(claim['id'], records, {'summary': 'chat', 'notes': [
        {'source_id': taken['id'], 'category': 'hobbies', 'evidence': LIKE},
        {'source_id': kept['id'], 'category': 'games', 'evidence': 'i play chess on weekends'}]},
        len(records), '100', False, {'input_tokens': 1, 'output_tokens': 1})
    assert notes(store) == ['i play chess on weekends']
    withdraw(client, kept['id'])  # And what it learned names its message.
    assert notes(store) == []


def test_a_note_an_older_controller_left_is_settled_at_start(world):
    """An older controller withdrew the exchange, and fenced its message, but kept the note: the next start drops it."""
    client, store = world
    taken, _ = learn(store, LIKE, age=30)
    learn(store, 'i play chess on weekends', age=20)
    with store.connection() as db:
        db.execute("DELETE FROM chat_turns WHERE json_extract(source,'$.id')=?", (taken,))
        db.execute('INSERT INTO chat_turn_withdrawals VALUES (?,?,?,?)', ('123', '20', taken, time.time()))
    assert LIKE in notes(store)
    restarted = Store(store.path)
    assert notes(restarted) == ['i play chess on weekends']


# 2. Another member's reply that drew on it.

def turn(store, text, *, user='30', conversation='public:51', age=0, reply_id=None, **extra):
    said = source(age=age, user=user, channel=conversation.split(':')[1])
    store.save_chat('123', conversation, user, 0, text, extra.pop('reply', 'about that: '+MARKER), 'model', 'high',
                    source=said, provenance=extra.pop('provenance', [{'user_id': '20', 'revision': 0}]),
                    reply_message_id=reply_id, verbatim=True, **extra)
    return said['id']


def replies(store, user='30', conversation='public:51'):
    return [turn['reply'] for turn in store.chat_context('123', conversation, user)['turns']]


def test_another_members_reply_that_carried_it_keeps_only_a_breadcrumb(world):
    client, store = world
    taken, _ = learn(store, 'explain '+MARKER, age=30)
    other_reply = str(int(taken)+1)  # Fri's reply to the other member, on Discord.
    turn(store, 'what did they mean', drew_on=[taken], reply_id=other_reply, age=20)
    turn(store, 'and unrelated', drew_on=[source(age=25, channel='51')['id']], reply='fine', age=10)
    assert withdraw(client, taken, deleted=True).json()['removed'] == 1
    assert replies(store) == [controller.DRAWN, 'fine']
    assert inputs(store, '30') == ['what did they mean', 'and unrelated']  # Their own words stay.
    assert store.message_sources('123', [other_reply])[other_reply]['verbatim'] is False  # A reference fails closed.


def test_one_that_carried_fris_reply_to_it_goes_too_and_one_that_carried_that(world):
    client, store = world
    said = source(age=40, channel='51')
    first_reply, second_reply, third_reply = (str(int(said['id'])+n) for n in (1, 2, 3))
    store.save_chat('123', 'public:51', '20', 0, 'explain '+MARKER, 'it means '+MARKER, 'model', 'high',
                    source=said, provenance=[], reply_message_id=first_reply, verbatim=True)
    turn(store, 'say that again', drew_on=[first_reply], reply_id=second_reply, age=30)  # Fri's reply to it, in view.
    turn(store, 'and again', user='40', drew_on=[second_reply], reply_id=third_reply, age=20)  # Two deep.
    withdraw(client, said['id'], deleted=True)
    assert replies(store) == [controller.DRAWN] and replies(store, '40') == [controller.DRAWN]


def test_by_id_alone_with_no_exchange_of_its_own(world):
    """A message in Fri's view it never answered: only the replies that carried it hold it."""
    client, store = world
    nearby = source(age=30, channel='51')['id']
    turn(store, 'what did they mean', drew_on=[nearby], age=20)
    assert withdraw_saved(client, nearby).json() == {'ok': True, 'removed': 0}  # An edit: it stays.
    assert replies(store) == ['about that: '+MARKER]
    assert withdraw_saved(client, nearby, deleted=True).json() == {'ok': True, 'removed': 0}
    assert replies(store) == [controller.DRAWN]
    with store.connection() as db:  # Nobody's exchange went, so no fence: its author is unknown here.
        assert db.execute('SELECT count(*) FROM chat_turn_withdrawals').fetchone()[0] == 0


def test_a_reply_saved_after_it_was_withdrawn_keeps_nothing_and_its_check_refuses(world):
    client, store = world
    taken, _ = learn(store, 'explain '+MARKER, age=30)
    withdraw(client, taken, deleted=True)
    with pytest.raises(Conflict):
        store.check_chat('30', 0, [{'user_id': '20', 'revision': 0}], guild_id='123', drew_on=[taken])
    said = source(age=2, user='30', channel='51')
    result = store.save_chat('123', 'public:51', '30', 0, 'what did they mean', 'about that: '+MARKER, 'model', 'high',
                             source=said, provenance=[{'user_id': '20', 'revision': 0}], drew_on=[taken])
    assert result['kept'] is False and replies(store) == []


def test_an_ordinary_reply_is_unchanged(world):
    """Guard: one that carried nothing withdrawn, or came from a Fri that says nothing, saves and checks as before."""
    client, store = world
    turn(store, 'what did they mean', drew_on=[source(age=30, channel='51')['id']], age=20)
    turn(store, 'older fri', age=10)
    assert store.check_chat('30', 0, [{'user_id': '20', 'revision': 0}], guild_id='123',
                            drew_on=[source(age=30, channel='51')['id']]) == {'ok': True}
    assert replies(store) == ['about that: '+MARKER]*2


def test_a_dependent_an_older_controller_left_stays_as_it_would_on_main(world):
    """An older controller's withdrawals don't say whether the message was edited or deleted, so a start leaves the
    replies that carried it, as main does. Only a deletion retires them (gene #150)."""
    client, store = world
    taken, _ = learn(store, 'explain '+MARKER, age=30)
    turn(store, 'what did they mean', drew_on=[taken], age=20)
    with store.connection() as db:  # As an older controller withdraws it: the exchange and its fence only.
        db.execute("DELETE FROM chat_turns WHERE json_extract(source,'$.id')=?", (taken,))
        db.execute('INSERT INTO chat_turn_withdrawals VALUES (?,?,?,?)', ('123', '20', taken, time.time()))
    assert replies(Store(store.path)) == ['about that: '+MARKER]


def test_the_api_takes_and_bounds_drew_on(world):
    client, store = world
    scope = {'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51', 'user_id': '30'}
    def body(**extra):
        return {**scope, 'source': source(age=1, user='30', channel='51'), 'revision': 0, 'input': 'hm',
                'reply': 'ok', 'model': 'model', 'effort': 'high', 'provenance': [], **extra}
    taken, _ = learn(store, 'explain '+MARKER, age=30)
    assert client.post('/chat/turn', headers=headers('fri'), json=body(drew_on=[taken])).status_code == 200
    withdraw(client, taken, deleted=True)
    assert replies(store) != ['ok'] and replies(store) == [controller.DRAWN]  # Its words no longer kept.
    assert client.post('/chat/turn/check', headers=headers('fri'), json=body(drew_on=[taken])).status_code == 409
    for bad in (['abc'], [str(i) for i in range(1, 1002)], [1.5]):
        assert client.post('/chat/turn', headers=headers('fri'), json=body(drew_on=bad)).status_code == 422


# Chat context, for a Fri that asks.

def test_context_names_where_notes_and_exchanges_elsewhere_came_from_only_when_asked(world):
    client, store = world
    taken, _ = learn(store, LIKE, audience='server', age=30)
    first, last = source(age=12, channel='51'), source(age=10, channel='51')
    store.save_chat('123', 'public:51', '20', 0, 'i play chess on weekends', 'okay', 'model', 'high', source=last,
                    burst=[{'source': first, 'text': 'yo'}, {'source': last, 'text': 'i play chess on weekends'}],
                    provenance=[], audience='server',
                    notes=[{'category': 'games', 'evidence': 'i play chess on weekends'}])
    with store.connection() as db:
        db.execute("INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at,origin,provenance) "
                   "VALUES ('123','public:52','20','music','i love trance',?,NULL,'[]')", (time.time(),))
    scope = {'guild_id': '123', 'channel_id': '52', 'conversation_id': 'public:52', 'user_id': '20', 'profiles': True}
    plain = client.post('/chat/context', headers=headers('fri'), json=scope).json()
    asked = client.post('/chat/context', headers=headers('fri'), json={**scope, 'sources': True}).json()
    assert 'note_sources' not in plain and all('messages' not in row for row in plain['elsewhere'])
    sources = dict(zip((note['evidence'] for note in asked['personal_memory']['notes']), asked['note_sources']))
    assert sources == {LIKE: [taken], 'i play chess on weekends': [last['id']],  # The message that says it.
                       'i love trance': None}  # None: unknown.
    assert [row['messages'] for row in asked['elsewhere']] == [[taken], sorted([first['id'], last['id']])]
    for row in asked['elsewhere']:
        row.pop('messages')
    asked.pop('note_sources')
    assert asked == plain  # Nothing else differs.


def test_the_contract_offers_it_as_an_optional_capability():
    advertised = contract()
    assert CAPABILITY in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != CAPABILITY]})


def test_nothing_it_logs_or_keeps_holds_the_words(world, caplog):
    client, store = world
    caplog.set_level(logging.DEBUG)
    taken, _ = learn(store, LIKE, age=30)
    turn(store, 'what did they mean', drew_on=[taken], age=20)
    withdraw(client, taken, deleted=True)
    assert LIKE not in caplog.text and MARKER not in caplog.text
    with store.connection() as db:
        assert LIKE not in json.dumps([dict(r) for r in db.execute('SELECT * FROM chat_turn_withdrawals')])


# Sol round 1 of gene #145.

def test_a_note_belongs_to_the_message_in_its_burst_that_says_it(world):
    """Its exact words come from one message of the burst: withdrawing another leaves the note, as main does, and
    withdrawing that one takes it."""
    client, store = world
    hello, said = source(age=6, channel='51'), source(age=4, channel='51')
    store.save_chat('123', 'public:51', '20', 0, LIKE, 'okay', 'model', 'high', source=said, provenance=[],
                    burst=[{'source': hello, 'text': 'hello'}, {'source': said, 'text': LIKE}],
                    notes=[{'category': 'hobbies', 'evidence': LIKE}])
    second = source(age=2, channel='51')
    store.save_chat('123', 'public:51', '20', 0, 'i play chess on weekends', 'okay', 'model', 'high', source=second,
                    provenance=[], notes=[{'category': 'games', 'evidence': 'i play chess on weekends'}])
    with store.connection() as db:  # Saved before notes named messages, or by an older controller.
        db.execute("UPDATE personal_notes SET sources=NULL WHERE evidence='i play chess on weekends'")
    withdraw(client, hello['id'])
    assert sorted(notes(store)) == sorted(['i play chess on weekends', LIKE])
    withdraw(client, said['id'])
    assert notes(store) == ['i play chess on weekends']


def test_a_note_of_unknown_source_goes_only_with_the_message_that_says_it(world):
    client, store = world
    hello, said = source(age=6, channel='51'), source(age=4, channel='51')
    store.save_chat('123', 'public:51', '20', 0, LIKE, 'okay', 'model', 'high', source=said, provenance=[],
                    burst=[{'source': hello, 'text': 'hello'}, {'source': said, 'text': LIKE}],
                    notes=[{'category': 'hobbies', 'evidence': LIKE}])
    with store.connection() as db:
        db.execute('UPDATE personal_notes SET sources=NULL')
    withdraw(client, hello['id'])  # The burst's exchange goes; the note it didn't say stays.
    assert notes(store) == [LIKE]


def fences(store, user='20'):
    with store.connection() as db:
        return [row[0] for row in db.execute('SELECT message_id FROM chat_turn_withdrawals WHERE user_id=?', (user,))]


def test_a_withdrawal_sent_before_an_erasure_writes_nothing_after_it(world):
    """A queued or in-flight withdrawal that reaches the controller after the member's "forget me" (or any privacy
    change since its exchange was saved) leaves no trace of them: no fence. Anything it would take still goes."""
    client, store = world
    taken = learn(store, LIKE, age=30)[0]
    store.personal_control('20', 'disable')  # "forget me": revision 0 -> 1, memory off.
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 0}  # An older Fri says no revision: memory is off.
    assert fences(store) == []
    assert withdraw(client, taken, revision=0).json() == {'ok': True, 'removed': 0}
    assert fences(store) == []
    store.personal_control('20', 'enable')  # Back on, revision 2: a stale one still writes nothing.
    assert withdraw(client, taken, revision=0).json() == {'ok': True, 'removed': 0}
    assert fences(store) == []
    with store.connection() as db:
        revision = db.execute("SELECT revision FROM chat_revisions WHERE user_id='20'").fetchone()[0]
    said = learn(store, 'i play chess on weekends', age=5, revision=revision)[0]
    assert withdraw(client, said, revision=revision).json()['removed'] == 1  # Saved under the current one: as before.
    assert fences(store) == [said]


def test_a_stale_withdrawal_still_takes_what_holds_the_message(world):
    """A privacy change that cleared one conversation leaves the member's exchanges elsewhere: a withdrawal saved
    under the older revision still takes those, without a fence, since a late save under it is refused anyway."""
    client, store = world
    taken = learn(store, 'yo '+LIKE, evidence=LIKE, conversation='public:52', age=30)[0]
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        store.advance_revision(db, '20')  # A note edited elsewhere, say: this exchange stays.
    assert inputs(store, conversation='public:52') == ['yo '+LIKE]
    assert withdraw(client, taken, revision=0, conversation_id='public:52', channel_id='52').json()['removed'] == 1
    assert inputs(store, conversation='public:52') == [] and fences(store) == []


def test_an_edit_takes_the_members_exchange_and_notes_but_not_others_replies(world):
    """gene #150: only a deletion retires other members' replies that carried the message."""
    client, store = world
    taken, _ = learn(store, LIKE, age=30)
    turn(store, 'what did they mean', drew_on=[taken], age=20)
    assert withdraw(client, taken).json()['removed'] == 1  # An edit: no `deleted`.
    assert notes(store) == [] and inputs(store) == []
    assert replies(store) == ['about that: '+MARKER]
    assert store.check_chat('30', 0, [{'user_id': '20', 'revision': 0}], guild_id='123', drew_on=[taken]) == {'ok': True}
    withdraw(client, taken, deleted=True)  # Then deleted: now they go.
    assert replies(store) == [controller.DRAWN]


def test_withdrawals_say_whether_it_was_deleted(world):
    client, store = world
    for path, body in (('/chat/turn/withdraw', {'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51',
                                                 'user_id': '20', 'message_id': '1'}),
                       ('/chat/turn/withdraw/saved', {'guild_id': '123', 'message_ids': ['1']})):
        for deleted in (True, False):
            assert client.post(path, headers=headers('fri'), json={**body, 'deleted': deleted}).status_code == 200
        assert client.post(path, headers=headers('fri'), json={**body, 'deleted': 'maybe'}).status_code == 422
    assert client.post('/chat/turn/withdraw', headers=headers('fri'), json={
        'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51', 'user_id': '20', 'message_id': '1',
        'revision': -1}).status_code == 422


def test_my_memory_names_where_each_note_came_from_only_when_asked(world):
    client, store = world
    taken, _ = learn(store, LIKE, age=30)
    scope = {'guild_id': '123', 'channel_id': '52', 'conversation_id': 'private:52', 'user_id': '20', 'profiles': True}
    plain = client.post('/chat/memory/notes', headers=headers('fri'), json=scope).json()
    asked = client.post('/chat/memory/notes', headers=headers('fri'), json={**scope, 'sources': True}).json()
    assert all('sources' not in row for row in plain['entries'])
    assert [row.pop('sources') for row in asked['entries']] == [[taken]]
    assert asked == plain


# Sol round 2 of gene #145.

def renewed_by_an_older_controller(store, evidence, said, user='20', conversation='public:51'):
    """Gene main's save of a note learned again, word for word: its time moves; its sources it doesn't know."""
    with store.connection() as db:
        db.execute('INSERT INTO personal_notes(guild_id,channel_id,user_id,category,evidence,at,origin,provenance) '
                   'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id,category,evidence) '
                   'DO UPDATE SET at=excluded.at,origin=excluded.origin,provenance=excluded.provenance '
                   'WHERE excluded.at>personal_notes.at',
                   ('123', conversation, user, 'hobbies', evidence, time.time(), None, '[]'))
        db.execute('INSERT INTO chat_turns(guild_id,channel_id,user_id,input,reply,model,effort,at,source,burst,provenance,'
                   'speaker_revision,attributed) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   ('123', conversation, user, evidence, 'okay', 'model', 'high', time.time(), json.dumps(said), '[]',
                    '[]', 0, 1))


def test_a_note_an_older_controller_renewed_keeps_no_stale_source(world):
    """Learned from A here, then word for word from B by an older controller, which renews its time but not its
    sources. A's withdrawal by ID (its exchange trimmed) leaves it, as main does: B still says it. Judged the legacy
    way, by the withdrawn message's own words, it goes with B."""
    client, store = world
    taken, _ = learn(store, LIKE, age=600)
    chatter(store, 6, start=500)  # A's exchange trimmed since.
    again = source(age=5, channel='51')
    renewed_by_an_older_controller(store, LIKE, again)
    restarted = Store(store.path)
    assert withdraw_saved(client, taken).json()['removed'] == 0
    assert notes(restarted) == [LIKE]
    assert withdraw(client, again['id']).json()['removed'] == 1
    assert notes(restarted) == [LIKE]  # Its sources are stale, so which message supplied it can't be told: as on main.


def test_chat_context_and_my_memory_call_a_stale_source_unknown(world):
    client, store = world
    learn(store, LIKE, age=600)
    renewed_by_an_older_controller(store, LIKE, source(age=5, channel='51'))
    scope = {'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51', 'user_id': '20', 'profiles': True,
             'sources': True}
    assert client.post('/chat/context', headers=headers('fri'), json=scope).json()['note_sources'] == [None]
    listed = client.post('/chat/memory/notes', headers=headers('fri'), json={
        **scope, 'channel_id': '52', 'conversation_id': 'private:52'}).json()['entries']
    assert [row['sources'] for row in listed] == [None]


def deletions(store):
    with store.connection() as db:
        return [row[0] for row in db.execute('SELECT message_id FROM chat_turn_deletions')]


def test_forget_me_takes_the_members_deletion_records_too(world):
    client, store = world
    scoped, _ = learn(store, LIKE, age=30)
    by_id, _ = learn(store, 'i play chess on weekends', age=20)
    other, _ = learn(store, 'i love trance', category='music', user='30', age=10)
    turn(store, 'what did they mean', user='40', drew_on=[scoped, by_id, other], age=5)
    withdraw(client, scoped, deleted=True)
    withdraw_saved(client, by_id, deleted=True)
    withdraw_saved(client, other, deleted=True)
    assert sorted(deletions(store)) == sorted([scoped, by_id, other])
    store.personal_control('20', 'disable')
    assert deletions(store) == [other]  # Another member's stays.
    assert deletions(Store(store.path)) == [other]


# Sol round 3 of gene #145.

def test_a_stale_note_renewed_after_the_withdrawn_message_stays(world):
    """G1: learned here from A (its exchange still kept), then renewed word for word from B by an older controller.
    Withdrawing A finds the words in A, but the note was renewed after A was sent, so a later message supplied it:
    it stays, as on main. Withdrawing B, which it wasn't renewed after, takes it."""
    client, store = world
    taken, _ = learn(store, LIKE, source=source(age=900, channel='51'))
    again = source(age=5, channel='51')
    renewed_by_an_older_controller(store, LIKE, again)
    assert withdraw(client, taken).json()['removed'] == 1
    assert notes(store) == [LIKE]
    withdraw(client, again['id'])
    assert notes(store) == [LIKE]  # Stale sources: as on main (round 4).


@pytest.mark.parametrize('after', [30, 299, 300])
def test_a_stale_note_renewed_soon_after_the_withdrawn_message_stays(world, after):
    """Sol r4 G1: renewed word for word from B by an older controller within the clock-skew allowance of A. Time can't
    tell that renewal from A's own save, so the note's stale sources decide nothing: it stays, as on main."""
    client, store = world
    taken, _ = learn(store, LIKE, source=source(age=after, channel='51'))  # Sent `after` seconds before the renewal.
    renewed_by_an_older_controller(store, LIKE, source(age=0, channel='51'))
    withdraw(client, taken)
    assert notes(store) == [LIKE]


def test_a_deletion_whose_author_is_unknown_keeps_no_record(world):
    """G2a: only another member's reply carried it. That reply retires now, and no deletion record is kept: its author
    is unknown. Only a fence by its ID alone, for a save still on its way (gene #149; see test_message_fences)."""
    client, store = world
    nearby = source(age=30, channel='51')['id']
    turn(store, 'what did they mean', drew_on=[nearby], age=20)
    withdraw_saved(client, nearby, deleted=True)
    assert replies(store) == [controller.DRAWN] and deletions(store) == []


def records(store):
    with store.connection() as db:
        return [tuple(row) for row in db.execute('SELECT message_id,user_id,revision FROM chat_turn_deletions')]


def test_a_deletion_record_goes_once_its_authors_revision_moved_at_start(world):
    """G2b: an older controller's "forget me" (or any privacy change) moves the author's revision but leaves this
    table alone: the next start drops their records, and keeps everyone else's."""
    client, store = world
    mine, _ = learn(store, LIKE, age=30)
    theirs, _ = learn(store, 'i love trance', category='music', user='30', age=20)
    withdraw(client, mine, deleted=True)
    withdraw(client, theirs, deleted=True, user_id='30')
    assert sorted(deletions(Store(store.path))) == sorted([mine, theirs])  # Nothing moved: both kept.
    with store.connection() as db:  # As an older controller's disable does: revision moved, this table untouched.
        db.execute('BEGIN IMMEDIATE')
        store.advance_revision(db, '20')
    assert deletions(Store(store.path)) == [theirs]
    assert records(store) == [(theirs, '30', 0)]  # With its author's revision then.
