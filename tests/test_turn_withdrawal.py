"""fri #129: a member's message edited or deleted after Fri saved their exchange takes that exchange with it."""
import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_member_summary import NEXT, claim
from gene.fri_protocol import compatible, contract
from tests.test_api import headers
from tests.test_fri_endings import source

MARKER = 'synthetic-withdrawn-marker'
SCOPE = {'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51', 'user_id': '20'}


@pytest.fixture
def world(tmp_path):
    settings = Settings(Path(tmp_path/'withdraw.sqlite3'), '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def save(store, text, *, user='20', conversation='public:51', age=0, guild='123', **extra):
    said = extra.pop('source', None) or source(age=age, user=user, channel=conversation.split(':')[1], guild=guild)
    store.save_chat(guild, conversation, user, 0, text, 'okay', 'model', 'high', source=said,
                    provenance=extra.pop('provenance', []), **extra)
    return said['id']


def inputs(store, user='20', conversation='public:51', guild='123'):
    return [turn['input'] for turn in store.chat_context(guild, conversation, user)['turns']]


def withdraw(client, message_id, role='fri', **scope):
    return client.post('/chat/turn/withdraw', headers=headers(role),
                       json={**SCOPE, **scope, 'message_id': message_id})


def test_only_that_members_exchange_holding_the_message_goes(world):
    client, store = world
    taken = save(store, 'explain '+MARKER, age=30)
    save(store, 'what about lunch', age=20)
    other = save(store, 'explain that caption?', user='30', age=10, provenance=[{'user_id': '20', 'revision': 0}])
    save(store, 'same member, other channel', conversation='public:52', age=5)
    assert withdraw(client, other).json() == {'ok': True, 'removed': 0}  # Not this member's message.
    assert withdraw(client, taken, conversation_id='public:52', channel_id='52').json()['removed'] == 0
    assert withdraw(client, taken, user_id='30').json()['removed'] == 0
    assert inputs(store) == ['explain '+MARKER, 'what about lunch']
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 1}
    assert inputs(store) == ['what about lunch']
    assert inputs(store, '30') == ['explain that caption?']  # Another member's reply that drew on it stays (fri #101).
    assert inputs(store, conversation='public:52') == ['same member, other channel']
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 0}  # Again: nothing left, still fine.
    assert store.check_chat('20', 0) == {'ok': True}  # Not a privacy change: their replies in flight still save.


def test_a_message_inside_a_burst_takes_the_burst_exchange(world):
    client, store = world
    first, last = source(age=4, channel='51'), source(age=2, channel='51')
    burst = [{'source': first, 'text': MARKER+' part one'}, {'source': last, 'text': 'part two'}]
    save(store, 'part two', source=last, burst=burst)
    assert MARKER in str(store.chat_context('123', 'public:51', '20')['turns'])
    assert withdraw(client, first['id']).json()['removed'] == 1
    assert inputs(store) == []


def test_it_leaves_the_members_other_conversations_view(world):
    client, store = world
    taken = save(store, 'said where all can read '+MARKER, age=10, audience='server')
    assert [t['input'] for t in store.chat_context('123', 'public:52', '20')['elsewhere']] == [
        'said where all can read '+MARKER]
    withdraw(client, taken)
    assert store.chat_context('123', 'public:52', '20')['elsewhere'] == []


def test_a_question_and_research_it_left_open_go_too(world):
    client, store = world
    taken, kept = source(age=3), source(age=1, user='30')
    with store.connection() as db:
        for said in (taken, kept):
            db.execute('INSERT INTO chat_pending VALUES (?,?,?,?,?,?,?)',
                       ('123', 'public:51', said['user_id'], said['id'], 0, 0, '{}'))
            db.execute('INSERT INTO web_requests VALUES (?,?,?,?,?,?,?)',
                       ('123', 'public:51', said['user_id'], said['id'], 0, 0, MARKER))
    withdraw(client, taken['id'])
    with store.connection() as db:
        assert [r[0] for r in db.execute('SELECT user_id FROM chat_pending')] == ['30']
        assert [r[0] for r in db.execute('SELECT user_id FROM web_requests')] == ['30']


def test_scope_and_credentials_are_those_of_chat(world):
    client, store = world
    lab = save(store, 'lab chat', guild='777', conversation='public:78')
    assert withdraw(client, lab, guild_id='777', channel_id='78', conversation_id='public:78').json()['removed'] == 1
    for scope in ({'channel_id': '99'}, {'guild_id': '456'}, {'guild_id': '777', 'channel_id': '51'},
                  {'guild_id': '777', 'channel_id': '78', 'conversation_id': 'public:51'}):
        assert withdraw(client, '1', **scope).status_code == 403
    for role in ('runtime', 'worker', 'deploy'):
        assert withdraw(client, '1', role=role).status_code == 401
    for scope in ({'message_id': 'abc'}, {'message_id': ''}, {'conversation_id': 'server'},
                  {'mention_grant': 'a'*64}):
        assert client.post('/chat/turn/withdraw', headers=headers('fri'),
                           json={**SCOPE, 'message_id': '1', **scope}).status_code == 422


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_erasure_still_works_around_a_withdrawal(world, erase):
    client, store = world
    taken = save(store, 'explain '+MARKER, age=10)
    save(store, 'what about lunch', age=5)
    withdraw(client, taken)
    if erase == 'disable':
        store.personal_control('20', 'disable')
    else:
        getattr(store, erase)('20')
    assert inputs(store) == []
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 0}
    assert store.chat_context('123', 'public:51', '20')['revision'] == 1


def test_nothing_it_logs_holds_the_words(world, caplog):
    client, store = world
    caplog.set_level(logging.DEBUG)
    withdraw(client, save(store, 'explain '+MARKER))
    assert MARKER not in caplog.text


def test_the_contract_offers_it_as_an_optional_capability():
    advertised = contract()
    assert 'turn-withdrawal' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != 'turn-withdrawal']})


def test_a_bursts_first_message_takes_the_question_its_burst_left(world):
    """gene #84: the question and its research permit name the burst's final message, not the one withdrawn."""
    client, store = world
    first, last = source(age=4, channel='51'), source(age=2, channel='51')
    burst = [{'source': first, 'text': MARKER+' with the lights off'}, {'source': last, 'text': 'fri play music'}]
    store.save_chat('123', 'public:51', '20', 0, 'fri play music', 'which song?', 'model', 'high', source=last,
                    burst=burst, provenance=[], clarification={'goal': 'play', 'topic': '', 'question': 'which song?'})
    theirs = source(age=1, user='30', channel='51')
    now = time.time()
    with store.connection() as db:
        db.execute('INSERT INTO web_requests VALUES (?,?,?,?,?,?,?)', ('123', 'public:51', '20', last['id'], 0, now, 'q'))
        db.execute('INSERT INTO chat_pending VALUES (?,?,?,?,?,?,?)', ('123', 'public:51', '30', theirs['id'], now, 0, '{}'))
        db.execute('INSERT INTO web_requests VALUES (?,?,?,?,?,?,?)', ('123', 'public:51', '30', theirs['id'], 0, now, 'q'))
    assert store.chat_context('123', 'public:51', '20')['pending']['source']['id'] == last['id']
    assert withdraw(client, first['id']).json() == {'ok': True, 'removed': 1}
    assert store.chat_context('123', 'public:51', '20')['pending'] is None
    for _ in range(2):  # Again: nothing left, still fine.
        with store.connection() as db:
            assert [r[0] for r in db.execute('SELECT user_id FROM chat_pending')] == ['30']
            assert [r[0] for r in db.execute('SELECT user_id FROM web_requests')] == ['30']
        assert withdraw(client, first['id']).json() == {'ok': True, 'removed': 0}


def background(client):
    agent = client.app.state.agent
    agent.update({'enabled': True}, 0, '9')
    return agent


def readable(store, text, age, user='20', conversation='public:51'):
    """An exchange said where everyone could read it, `age` seconds back, saved a second later."""
    message = save(store, text, user=user, conversation=conversation, age=age, audience='server')
    with store.connection() as db:
        db.execute('UPDATE chat_turns SET at=? WHERE id=(SELECT max(id) FROM chat_turns)', (time.time()-age+1,))
    return message


def summary(store, user='20'):
    shown = store.chat_context('123', 'private:9', user, True, True)['member_summary']
    return shown and shown['text']


def built(agent, text):
    """A build Fri finished from everything it was sent."""
    return finish(agent, *claimed(agent), text)


def claimed(agent):
    with agent.store.connection() as db:
        agent.put(db, NEXT, 0)  # Not waiting out the idle pause between looks.
    offer = claim(agent)
    assert offer, 'nobody was due a summary'
    return offer['id'], sorted({row['conversation_id'] for row in offer['exchanges']}), offer


def finish(agent, claim_id, conversations, offer, text):
    from gene.fri_member_summary import finish as finished
    return finished(agent, claim_id, text, 'completed', conversations, offer['previous'] is not None)


def test_a_summary_built_on_the_message_goes_with_it(world, caplog):
    """gene #83: only that member's, and a build in flight for someone else still keeps."""
    client, store = world
    agent = background(client)
    caplog.set_level(logging.DEBUG)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    readable(store, 'i main a healer', 7300, user='30', conversation='public:52')
    readable(store, 'see you at the raid', 7250, user='30', conversation='public:52')
    assert built(agent, 'they planned a raid with the '+MARKER) == {'kept': True}
    assert MARKER in summary(store)
    theirs = claimed(agent)  # Ash's build is under way when the message goes.
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 1}
    assert summary(store) is None
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM fri_member_summaries WHERE user_id='20'").fetchone()[0] == 0
    assert finish(agent, *theirs, 'they main a healer') == {'kept': True}
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 0}  # Again: nothing left, still fine.
    assert summary(store, '30') == 'they main a healer'
    assert MARKER not in caplog.text


def test_an_exchange_trimmed_since_still_takes_the_summary(world):
    """Six exchanges a conversation: the turn may be gone before the message is withdrawn, the summary not."""
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid with the '+MARKER)
    for age in range(60, 0, -10):
        readable(store, f'later chat {age}', age)
    assert MARKER not in str(store.chat_context('123', 'public:51', '20')['turns'])
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 0}
    assert summary(store) is None


def test_a_build_in_flight_keeps_nothing_from_it(world):
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    pending = claimed(agent)
    withdraw(client, taken)
    with pytest.raises(ValueError, match='expired'):
        finish(agent, *pending, 'they planned a raid with the '+MARKER)
    assert summary(store) is None


def test_a_build_that_was_sent_the_old_summary_keeps_nothing_either(world):
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid with the '+MARKER)
    readable(store, 'the next day stuff', 3700)
    readable(store, 'more of it', 3650)
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    pending = claimed(agent)
    assert MARKER in pending[2]['previous']['text']
    withdraw(client, taken)
    with pytest.raises(ValueError, match='expired'):
        finish(agent, *pending, 'they planned a raid with the '+MARKER+' and more')
    assert summary(store) is None


def test_a_summary_that_never_read_the_message_stays(world):
    client, store = world
    agent = background(client)
    readable(store, 'raid tonight', 7300)
    readable(store, 'what build for the raid', 7250)
    restricted = save(store, 'restricted channel stuff', conversation='public:52', age=7200)
    private = save(store, 'dm stuff', conversation='private:52', age=7200)
    built(agent, 'they planned a raid')
    later = readable(store, 'said after the build', 30)
    assert withdraw(client, later).json()['removed'] == 1
    assert withdraw(client, restricted, conversation_id='public:52', channel_id='52').json()['removed'] == 1
    assert withdraw(client, private, conversation_id='private:52', channel_id='52').json()['removed'] == 1
    assert summary(store) == 'they planned a raid'


def test_their_own_edit_of_it_stays(world):
    """An edit in My memory is in their own words and drew on no exchange."""
    from gene.fri_member_summary import edit
    from tests.test_fri_memory_items import source as control_source
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight', 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid')
    edit(store, '123', 'private:9', '20', 0, {**control_source(), 'channel_id': '9'}, 'replace', 'mostly raids')
    assert withdraw(client, taken).json()['removed'] == 0  # The edit cleared their saved chats.
    assert summary(store) == 'mostly raids'


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_erasure_still_takes_a_summary_a_withdrawal_left(world, erase):
    client, store = world
    agent = background(client)
    readable(store, 'raid tonight', 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid')
    withdraw(client, readable(store, 'said after the build', 30))
    assert summary(store) == 'they planned a raid'
    if erase == 'disable':
        store.personal_control('20', 'disable')
    else:
        getattr(store, erase)('20')
    assert summary(store) is None and inputs(store) == []
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_member_summaries').fetchone()[0] == 0


def turn(said, text, *, user='20', **extra):
    """A turn's body for /chat/turn and its check before delivery, answering `said`."""
    return {**SCOPE, 'user_id': user, 'revision': 0, 'source': said, 'input': text, 'reply': 'okay',
            'model': 'model', 'effort': 'high', 'provenance': [], **extra}


def check(client, body):
    return client.post('/chat/turn/check', headers=headers('fri'), json=body)


def post_turn(client, body):
    return client.post('/chat/turn', headers=headers('fri'), json=body)


def fences(store):
    with store.connection() as db:
        return [dict(row) for row in db.execute('SELECT * FROM chat_turn_withdrawals ORDER BY user_id')]


def test_a_save_landing_after_its_message_was_withdrawn_keeps_nothing(world, caplog):
    """gene #90: the withdrawal reached the controller first (a save Fri timed out on, say), and found nothing yet."""
    client, store = world
    caplog.set_level(logging.DEBUG)
    first, last = source(age=4, channel='51'), source(age=2, channel='51')
    burst = [{'source': first, 'text': MARKER+' with the lights off'}, {'source': last, 'text': 'fri play music'}]
    body = turn(last, 'fri play music', burst=burst, reply='which song? the '+MARKER+' one?',
                notes=[{'category': 'music', 'evidence': 'fri play music'}],
                clarification={'goal': 'play', 'topic': '', 'question': 'which song?'})
    assert withdraw(client, first['id']).json() == {'ok': True, 'removed': 0}
    for _ in range(2):  # A retry of the save is as harmless.
        assert post_turn(client, body).json() == {'ok': True, 'first_memory': False, 'kept': False}
    context = store.chat_context('123', 'public:51', '20', True, True)
    assert context['turns'] == [] and context['pending'] is None and context['personal_memory']['notes'] == []
    assert MARKER not in str(context) and MARKER not in caplog.text
    # Their next message, which isn't the one withdrawn, saves as ever, and so does another member's.
    later = source(age=1, channel='51')
    assert post_turn(client, turn(later, 'what about lunch')).json()['ok'] is True
    assert post_turn(client, turn(source(age=1, user='30', channel='51'), 'me too', user='30')).json()['ok'] is True
    assert inputs(store) == ['what about lunch'] and inputs(store, '30') == ['me too']


def test_a_late_save_of_the_message_itself_keeps_nothing(world):
    client, store = world
    said = source(age=3, channel='51')
    withdraw(client, said['id'])
    assert store.save_chat('123', 'public:51', '20', 0, 'explain '+MARKER, 'okay', 'model', 'high', source=said,
                           provenance=[])['kept'] is False
    assert inputs(store) == []


def test_the_check_before_delivery_refuses_a_reply_to_a_withdrawn_message(world, caplog):
    client, store = world
    caplog.set_level(logging.DEBUG)
    first, last = source(age=4, channel='51'), source(age=2, channel='51')
    body = turn(last, 'part two', burst=[{'source': first, 'text': MARKER+' part one'}, {'source': last, 'text': 'part two'}])
    assert check(client, body).json() == {'ok': True}
    withdraw(client, first['id'])
    for _ in range(2):  # Asked again, the same answer.
        refused = check(client, body)
        assert refused.status_code == 409 and MARKER not in refused.text
    assert check(client, {**body, 'burst': []}).json() == {'ok': True}  # Its last message alone isn't the one withdrawn.
    assert check(client, turn(last, 'part two', user='30', source={**last, 'user_id': '30'})).json() == {'ok': True}
    withdraw(client, last['id'])
    assert check(client, {**body, 'burst': []}).status_code == 409
    assert MARKER not in caplog.text


def test_research_for_a_withdrawn_message_is_refused(world):
    client, store = world
    said = source(age=2, channel='51')
    research = {**SCOPE, 'source': said, 'revision': 0, 'input': 'weather in Seoul', 'query': 'weather in Seoul'}
    withdraw(client, said['id'])
    assert client.post('/chat/research/start', headers=headers('fri'), json=research).status_code == 400
    fresh = {**research, 'source': source(age=1, channel='51')}
    assert client.post('/chat/research/start', headers=headers('fri'), json=fresh).json() == {'ok': True}
    with store.connection() as db:
        assert [r[0] for r in db.execute('SELECT source_id FROM web_requests')] == [fresh['source']['id']]


def test_the_fence_holds_no_words_and_lapses(world):
    client, store = world
    taken = save(store, 'explain '+MARKER)
    for _ in range(2):  # A repeat keeps one fence.
        withdraw(client, taken)
    [fence] = fences(store)
    assert set(fence) == {'guild_id', 'user_id', 'message_id', 'at'} and MARKER not in str(fence)
    assert (fence['guild_id'], fence['user_id'], fence['message_id']) == ('123', '20', taken)
    with store.connection() as db:  # Kept a week, as long as a saved exchange is: no turn runs that long.
        db.execute('UPDATE chat_turn_withdrawals SET at=at-7*86400+60')
    store.chat_context('123', 'public:51', '20')
    assert len(fences(store)) == 1
    with store.connection() as db:
        db.execute('UPDATE chat_turn_withdrawals SET at=at-120')
    store.chat_context('123', 'public:51', '20')
    assert fences(store) == []


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_erasure_after_a_fence_still_works_and_refuses_the_late_save(world, erase):
    client, store = world
    mine, theirs = source(age=3, channel='51'), source(age=3, user='30', channel='51')
    withdraw(client, mine['id'])
    withdraw(client, theirs['id'], user_id='30')
    if erase == 'disable':
        store.personal_control('20', 'disable')
    else:
        getattr(store, erase)('20')
    assert post_turn(client, turn(mine, 'explain '+MARKER)).status_code == 409  # Their revision moved.
    # Memory off keeps nothing of theirs, fences included; Forget my chats leaves them, like note removals.
    assert [f['user_id'] for f in fences(store)] == (['20', '30'] if erase == 'clear_chats' else ['30'])
    assert inputs(store) == []


def summary_generation(store, user='20'):
    return store.chat_context('123', 'public:51', user, True, True)['member_summary']['generation']


def test_a_reply_drawing_on_a_summary_withdrawn_since_is_refused(world, caplog):
    """gene #91: the turn fetched the summary, then the message it was built on was withdrawn."""
    client, store = world
    agent = background(client)
    caplog.set_level(logging.DEBUG)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid with the '+MARKER)
    shown = store.chat_context('123', 'public:51', '20', True, True)['member_summary']
    assert MARKER in shown['text']
    body = turn(source(age=1, channel='51'), 'what did i plan', reply='your raid with the '+MARKER,
                summary_generation=shown.get('generation', 0))
    assert check(client, body).json() == {'ok': True}
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 1}
    for _ in range(2):  # Asked again, the same answer.
        refused = check(client, body)
        assert refused.status_code == 409 and MARKER not in refused.text
        assert post_turn(client, body).status_code == 409
    assert MARKER not in str(store.chat_context('123', 'public:51', '20', True, True))
    assert MARKER not in caplog.text


def test_what_a_summary_withdrawal_leaves_alone(world):
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight', 7300)
    readable(store, 'what build for the raid', 7250)
    readable(store, 'i main a healer', 7300, user='30', conversation='public:52')
    readable(store, 'see you at the raid', 7250, user='30', conversation='public:52')
    built(agent, 'they planned a raid')
    built(agent, 'they main a healer')
    mine, theirs = summary_generation(store), summary_generation(store, '30')
    later = readable(store, 'said after the build', 30)

    def reply(generation, user='20', age=1):
        said = source(age=age, user=user, channel='51')
        return turn(said, 'what did i plan', user=user, **({} if generation is None else {'summary_generation': generation}))
    withdraw(client, later)  # Never read by the summary: it stays, and so does its generation.
    assert summary_generation(store) == mine and check(client, reply(mine)).json() == {'ok': True}
    withdraw(client, taken)
    assert check(client, reply(mine)).status_code == 409
    assert check(client, reply(None)).json() == {'ok': True}  # An older Fri says nothing, and is checked as before.
    assert post_turn(client, reply(None, age=2)).json()['ok'] is True
    assert check(client, reply(theirs, '30')).json() == {'ok': True}  # Another member's summary is untouched.
    assert summary_generation(store, '30') == theirs
    withdraw(client, taken)  # Again: nothing left to drop, nothing moves.
    readable(store, 'the next day stuff', 3700)
    readable(store, 'more of it', 3650)
    with store.connection() as db:
        db.execute("DELETE FROM fri_member_summary_calls WHERE user_id='20'")
    built(agent, 'they talked about the next day')
    assert summary_generation(store) != mine
    assert check(client, reply(summary_generation(store))).json() == {'ok': True}


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_erasure_still_takes_the_summary_and_its_generation(world, erase):
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight', 7300)
    readable(store, 'what build for the raid', 7250)
    readable(store, 'i main a healer', 7300, user='30', conversation='public:52')
    readable(store, 'see you at the raid', 7250, user='30', conversation='public:52')
    built(agent, 'they planned a raid')
    built(agent, 'they main a healer')
    old = summary_generation(store)
    withdraw(client, taken)
    if erase == 'disable':
        store.personal_control('20', 'disable')
    else:
        getattr(store, erase)('20')
    assert summary(store) is None and summary(store, '30') == 'they main a healer'
    assert check(client, turn(source(age=1, channel='51'), 'hi', summary_generation=old)).status_code == 409
    with store.connection() as db:
        assert [r[0] for r in db.execute('SELECT user_id FROM fri_member_summary_generations')] == []


def test_a_purge_before_rollback_takes_the_generations_too(world, tmp_path):
    from gene.fri_member_summary import purge
    client, store = world
    agent = background(client)
    taken = readable(store, 'raid tonight', 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid')
    withdraw(client, taken)
    purge(store.path)
    with store.connection() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & {'fri_member_summaries', 'fri_member_summary_generations'}


def test_the_contract_offers_summary_generations_as_optional():
    advertised = contract()
    assert 'summary-generations' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities']
                                                     if c != 'summary-generations']})


# gene #97 review round 1.

def drawn_on(client, store):
    """A summary built on a message, and a reply already drawing on it: the message's ID and the reply's body."""
    agent = background(client)
    taken = readable(store, 'raid tonight, bring the '+MARKER, 7300)
    readable(store, 'what build for the raid', 7250)
    built(agent, 'they planned a raid with the '+MARKER)
    shown = store.chat_context('123', 'public:51', '20', True, True)['member_summary']
    return taken, turn(source(age=1, channel='51'), 'what did i plan', reply='your raid with the '+MARKER,
                       summary_generation=shown['generation'])


@pytest.mark.parametrize('how', ['prune', 'read'])
def test_a_summary_expired_before_the_withdrawal_still_refuses_the_reply(world, how):
    """The summary expired while the reply was written, so the withdrawal found none to drop: the expiry moved the
    generation instead."""
    from gene.fri_member_summary import TTL, prune
    client, store = world
    taken, body = drawn_on(client, store)
    with store.connection() as db:
        db.execute('UPDATE fri_member_summaries SET refreshed=?', (time.time()-TTL-60,))
        if how == 'prune':
            prune(db)
    if how == 'read':
        assert store.chat_context('123', 'public:51', '20', True, True)['member_summary'] is None
    assert withdraw(client, taken).json() == {'ok': True, 'removed': 1}
    refused = check(client, body)
    assert refused.status_code == 409 and MARKER not in refused.text
    assert post_turn(client, body).status_code == 409


def test_another_members_privacy_change_taking_the_summary_refuses_a_reply_holding_it(world):
    client, store = world
    _, body = drawn_on(client, store)
    with store.connection() as db:  # It drew on member 30's words too.
        db.execute("UPDATE fri_member_summaries SET provenance=json_array(json_object('user_id','30','revision',0))")
    store.personal_control('30', 'disable')
    assert summary(store) is None
    assert check(client, body).status_code == 409
    assert check(client, {**body, 'summary_generation': summary_generation_now(store)}).json() == {'ok': True}


def summary_generation_now(store, user='20'):
    from gene.fri_member_summary import generation
    with store.connection() as db:
        return generation(db, '123', user)


def test_a_late_save_of_a_withdrawn_message_leaves_a_newer_question_pending(world):
    """Withdraw A; B, newer, leaves a question pending; then A's save arrives late. It keeps nothing, and B's question
    is still pending, neither cleared nor replaced by A's."""
    client, store = world
    first, later = source(age=4, channel='51'), source(age=2, channel='51')
    withdraw(client, first['id'])
    asked = {'goal': 'play', 'topic': '', 'question': 'which song?'}
    assert post_turn(client, turn(later, 'fri play music', reply='which song?', clarification=asked)).json()['ok']
    late = turn(first, 'fri turn it up '+MARKER, reply='how loud?',
                clarification={'goal': 'volume', 'topic': '', 'question': 'how loud?'})
    assert post_turn(client, late).json() == {'ok': True, 'first_memory': False, 'kept': False}
    pending = store.chat_context('123', 'public:51', '20', True, True)['pending']
    assert pending and pending['question'] == 'which song?' and MARKER not in str(pending)
