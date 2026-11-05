"""HumanMint/fri#355: a member summary held back until its messages are read again (fri #349) holds back what was
derived from it too.

Holding a summary hid it from fresh reads, but not what was already made from it. A turn that copied it still passed its
checks and saved; a saved reply that drew on it, or repeated one that did, still came back as history and as a
reference; a question such a reply left open still showed as pending; and drafts and reminders depending on it still
showed and went out.

While it's held, all of that is kept out of use: a turn that drew on it is refused at its checks and its save, a saved
reply reads as withheld for now, its open question isn't shown, and a draft or reminder depending on it is left out of
lists and deliveries, and refused when asked for by itself. Nothing is retired for that: once a recheck says ok, it all
shows again. Only a summary that goes (a recheck finding it changed, a withdrawal) retires what drew on it, as before.
"""
import time

import pytest

from gene.fri_protocol import contract
from tests.test_api import headers
from tests.test_fri_endings import source
from tests.test_saved_dependencies import (
    SAID,
    THEIRS,
    dm,
    history,
    reminder,
    rows,
    work,
    world,  # noqa: F401 (the fixture)
    written,
)
from tests.test_summary_dependencies import ECHOED, depending, repeated, saved_reply
from tests.test_summary_generation_retention import (
    QUOTING,
    quoted,
    referenced,
    replies,
    retired,
    summarized,
)
from tests.test_turn_withdrawal import MARKER, check, post_turn, summary_generation_now, turn

HOLDS = 'summary-holds'


def held_back(client):
    """Fri's first recheck claim since it started: every summary is held back until it's read again. Its claim."""
    answer = client.post('/agent/member-summary/recheck', headers=headers('fri'), json={'fresh': True})
    assert answer.status_code == 200, answer.text
    [item] = answer.json()['summaries']
    return item['id']


def answered(client, claim, status):
    answer = client.post(f'/agent/member-summary/recheck/{claim}', headers=headers('fri'), json={'status': status})
    assert answer.status_code == 200, answer.text


def theirs(client, route, **extra):
    """Member 30's DM asking the controller for their own drafts or reminders."""
    return client.post(route, headers=headers('fri'), json={**THEIRS, **extra})


def stored_replies(store):
    with store.connection() as db:
        return [row[0] for row in db.execute('SELECT reply FROM chat_turns')]


def test_the_controller_offers_holds():
    assert HOLDS in contract()['capabilities']


@pytest.mark.parametrize('route', [check, post_turn])
def test_a_turn_drawing_on_a_held_summary_is_refused_until_it_checks_out(world, route):  # noqa: F811
    """Item 1: the turn copied member 20's summary, which is then held back before its checks. The check before the
    model request, before delivery and the save refuse it; nothing is retired, and once a recheck says ok it passes."""
    client, store = world
    _, generation = summarized(client, store)
    claim = held_back(client)
    body = turn(source(age=1, channel='51'), 'what did i plan', reply=QUOTING, summary_generation=generation)
    refused = route(client, body)
    assert refused.status_code == 409 and MARKER not in refused.text
    assert summary_generation_now(store) == generation
    answered(client, claim, 'ok')
    assert route(client, {**body, 'source': source(age=2, channel='51')}).status_code == 200


@pytest.mark.parametrize('route', [check, post_turn])
def test_a_turn_depending_on_another_members_held_summary_is_refused_too(world, route):  # noqa: F811
    """Member 30's turn repeats a reply of Fri's that drew on member 20's summary (fri #193)."""
    client, store = world
    _, generation = summarized(client, store)
    claim = held_back(client)
    body = turn(source(age=1, user='30', channel='51'), 'what did they plan', user='30', reply=ECHOED,
                summary_dependencies=depending(generation), provenance=[{'user_id': '20', 'revision': 0}])
    assert route(client, body).status_code == 409
    answered(client, claim, 'ok')
    assert route(client, {**body, 'source': source(age=2, user='30', channel='51')}).status_code == 200


@pytest.mark.parametrize('conversation', ['private:9', 'public:51'])
def test_a_saved_reply_drawing_on_a_held_summary_is_withheld_until_it_checks_out(world, conversation):  # noqa: F811
    """Item 2: member 20's own saved reply on their summary, in a DM or the channel it came from. While the summary is
    held, the reply comes back as withheld, as history and as a reference to it; it's kept, not retired."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation, conversation=conversation)
    claim = held_back(client)
    reply = replies(store, conversation)[-1]
    assert MARKER not in reply and not retired(reply)
    assert QUOTING not in str(store.chat_context('123', conversation, '20', True, True, dependencies=True))
    assert referenced(store)['verbatim'] is False and 'summaries' not in referenced(store)
    assert referenced(store).get('held') is True  # Not retired: Fri keeps its copy (Sol round 1, S1).
    assert QUOTING in stored_replies(store)
    answered(client, claim, 'ok')
    assert QUOTING in replies(store, conversation) and referenced(store)['verbatim'] is True
    assert 'held' not in referenced(store)


def test_a_reply_repeating_one_that_drew_on_a_held_summary_is_withheld_too(world):  # noqa: F811
    """Member 30's saved reply repeated a reply of Fri's that drew on member 20's summary (fri #193)."""
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    claim = held_back(client)
    assert MARKER not in str(history(client, dependencies=True)['turns'])
    assert 'summaries' not in str(history(client, dependencies=True)['turns'])
    assert store.message_sources('123', ['800'])['800']['verbatim'] is False
    assert store.message_sources('123', ['800'])['800']['held'] is True
    assert saved_reply(store) == ECHOED
    answered(client, claim, 'ok')
    assert ECHOED in str(history(client)['turns'])
    assert store.message_sources('123', ['800'])['800']['verbatim'] is True


def test_once_a_recheck_finds_the_summary_changed_what_drew_on_it_is_retired(world):  # noqa: F811
    """As before: only a summary that goes retires what drew on it."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation)
    repeated(client, generation)
    answered(client, held_back(client), 'changed')
    assert retired(replies(store)[-1]) and retired(saved_reply(store))


def test_a_question_a_reply_on_a_held_summary_left_open_is_hidden_until_it_checks_out(world):  # noqa: F811
    client, store = world
    _, generation = summarized(client, store)
    asked = {'goal': 'play', 'topic': '', 'question': 'the '+MARKER+' song or another one?'}
    store.save_chat('123', 'private:9', '20', 0, 'fri play a song', asked['question'], 'model', 'high',
                    source=source(age=1, channel='9', guild=None), provenance=[], clarification=asked,
                    summary_generation=generation)
    claim = held_back(client)
    assert store.chat_context('123', 'private:9', '20', True, True)['pending'] is None
    answered(client, claim, 'ok')
    assert store.chat_context('123', 'private:9', '20', True, True)['pending']['question'] == asked['question']


def test_drafts_and_reminders_depending_on_a_held_summary_are_kept_out_until_it_checks_out(world):  # noqa: F811
    """Item 3: member 30's draft and reminder drew on member 20's summary. While it's held, neither is listed, shown in
    the DM's open loops, or given when asked for by itself; their own others are. Nothing is retired."""
    client, store = world
    _, generation = summarized(client, store)
    draft = written(client, '/workspace/save', work(dm(1), depending(generation)))
    note = written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    mine = written(client, '/workspace/save', work(dm(3), body='my own list'))
    claim = held_back(client)
    assert [item['id'] for item in theirs(client, '/workspace/list').json()['items']] == [mine['id']]
    assert theirs(client, f"/workspace/read/{draft['id']}").status_code == 423
    assert theirs(client, f"/workspace/read/{mine['id']}").status_code == 200
    assert note['id'] not in str(theirs(client, '/social/reminders/list').json())
    assert theirs(client, f"/social/reminders/{note['id']}/read").status_code == 423
    loops = history(client, conversation='private:19')['loops']
    assert MARKER not in str(loops) and loops['saved_work'] == [{'title': 'raid notes', 'updated': pytest.approx(
        rows(store, 'fri_work')[mine['id']]['updated'])}]
    assert rows(store, 'fri_work')[draft['id']]['body'] == SAID and rows(store, 'fri_reminders')[note['id']]['body'] == SAID
    answered(client, claim, 'ok')
    assert draft['id'] in str(theirs(client, '/workspace/list').json())
    assert theirs(client, f"/workspace/read/{draft['id']}").json()['body'] == SAID
    assert theirs(client, f"/social/reminders/{note['id']}/read").json()['body'] == SAID


def test_a_reminder_or_post_depending_on_a_held_summary_waits_to_be_delivered(world):  # noqa: F811
    """Due while member 20's summary is held: not claimed for delivery, so not sent, and still scheduled; another
    member's goes. Claimed once a recheck says ok."""
    client, store = world
    _, generation = summarized(client, store)
    draft = written(client, '/workspace/save', work(dm(1), depending(generation)))
    note = written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    plain = written(client, '/social/reminders/prepare', reminder(dm(3), body='stretch'))
    now = time.time()
    with store.connection() as db:
        db.execute("UPDATE fri_reminders SET state='scheduled',due=?", (now-60,))
        db.execute("INSERT INTO fri_posts(id,work_id,version,guild_id,user_id,dm_id,target_id,body,revision,state,"
                   "source,created,expires) VALUES ('p1',?,1,'123','30','19','51',?,0,'authorized','{}',?,?)",
                   (draft['id'], SAID, now, now+300))
    claim = held_back(client)
    claimed = client.post('/social/reminders/claim', headers=headers('fri')).json()
    assert claimed['id'] == plain['id']
    assert client.post('/social/reminders/claim', headers=headers('fri')).json() is None
    assert client.post('/workspace/outbox/claim', headers=headers('fri')).json() is None
    assert rows(store, 'fri_reminders')[note['id']]['state'] == 'scheduled'
    assert rows(store, 'fri_posts')['p1']['state'] == 'authorized'
    answered(client, claim, 'ok')
    assert client.post('/social/reminders/claim', headers=headers('fri')).json()['id'] == note['id']
    assert client.post('/workspace/outbox/claim', headers=headers('fri')).json()['id'] == 'p1'


@pytest.mark.parametrize('route,body', [('/workspace/save', work), ('/social/reminders/prepare', reminder)])
def test_a_write_depending_on_a_held_summary_is_refused_until_it_checks_out(world, route, body):  # noqa: F811
    client, store = world
    _, generation = summarized(client, store)
    claim = held_back(client)
    refused = client.post(route, headers=headers('fri'), json=body(dm(1), depending(generation)))
    assert refused.status_code == 409 and MARKER not in refused.text
    answered(client, claim, 'ok')
    written(client, route, body(dm(2), depending(generation)))


def held(store, user):
    """A kept summary of the member's, held back until it's read again."""
    now = time.time()
    with store.connection() as db:
        db.execute("INSERT INTO fri_member_summaries VALUES ('123',?,'they planned a raid',0,'[]','[\"public:1\"]',?,?)",
                   (user, now, now))
        db.execute("INSERT INTO fri_member_summary_rechecks(guild_id,user_id,through,checked,held) VALUES "
                   "('123',?,?,0,1)", (user, now))


def test_reply_checks_leave_out_a_pair_whose_reply_drew_on_a_held_summary(tmp_path):
    """A reply check sends both replies of a pair to its model: one that drew on a held summary isn't sent meanwhile
    (as a pair with a non-verbatim reply isn't). Another member's pair is."""
    from tests.test_fri_reply_checks import exchange
    from tests.test_fri_reply_checks import setup as reply_checks
    store, _, checks, _ = reply_checks.__wrapped__(tmp_path)
    exchange(store, user='20', summary_generation=0)
    other = exchange(store, user='21', summary_generation=0)
    held(store, '20')
    assert [pair['id'] for pair in checks.claim()['pairs']] == [str(other)]


# Sol round 1: B1, a delivery claimed before the hold; B3, work drawn on the writer's own summary.

def fri(client, route, **body):
    return client.post(route, headers=headers('fri'), json=body)


def scheduled(store):
    with store.connection() as db:
        db.execute("UPDATE fri_reminders SET state='scheduled',due=?", (time.time()-60,))


def authorized(store, work_id, post_id='p1'):
    now = time.time()
    with store.connection() as db:
        db.execute("INSERT INTO fri_posts(id,work_id,version,guild_id,user_id,dm_id,target_id,body,revision,state,"
                   "source,created,expires) VALUES (?,?,1,'123','30','19','51',?,0,'authorized','{}',?,?)",
                   (post_id, work_id, SAID, now, now+300))


@pytest.mark.parametrize('kind', ['reminder', 'post'])
def test_b1_a_delivery_claimed_before_its_summary_is_held_goes_back_unsent(world, kind):  # noqa: F811
    """Sol round 1's B1: claimed for delivery, then member 20's summary is held before it starts sending. The
    controller refused it like any other conflict, and Fri recorded the delivery as failed for good, so it never went.
    It's put back as it was before its claim (scheduled, or authorized), with its lease gone, and the answer says it's
    held (423), not refused; once a recheck says ok, it's claimed and sent."""
    client, store = world
    _, generation = summarized(client, store)
    if kind == 'reminder':
        item = written(client, '/social/reminders/prepare', reminder(dm(1), depending(generation)))['id']
        scheduled(store)
        claim_route, table, before = '/social/reminders/claim', 'fri_reminders', 'scheduled'
        sending = f'/social/reminders/{item}/sending'
    else:
        authorized(store, written(client, '/workspace/save', work(dm(1), depending(generation)))['id'])
        item, claim_route, table, before = 'p1', '/workspace/outbox/claim', 'fri_posts', 'authorized'
        sending = '/workspace/posts/p1/sending'
    claimed = fri(client, claim_route).json()
    assert claimed['id'] == item
    recheck = held_back(client)
    refused = fri(client, sending, lease=claimed['lease'])
    assert refused.status_code == 423 and MARKER not in refused.text
    assert (rows(store, table)[item]['state'], rows(store, table)[item]['lease']) == (before, '')
    assert fri(client, claim_route).json() is None
    answered(client, recheck, 'ok')
    again = fri(client, claim_route).json()
    assert again['id'] == item and fri(client, sending, lease=again['lease']).status_code == 200


def test_b1_once_sending_has_begun_a_hold_changes_nothing(world):  # noqa: F811
    """Its receipt still lands, sent or uncertain: delivery already holds what it sends."""
    client, store = world
    _, generation = summarized(client, store)
    item = written(client, '/social/reminders/prepare', reminder(dm(1), depending(generation)))['id']
    scheduled(store)
    claimed = fri(client, '/social/reminders/claim').json()
    assert fri(client, f'/social/reminders/{item}/sending', lease=claimed['lease']).status_code == 200
    held_back(client)
    finished = fri(client, f'/social/reminders/{item}/finish', lease=claimed['lease'], state='sent', message_id='987')
    assert finished.status_code == 200 and rows(store, 'fri_reminders')[item]['state'] == 'sent'


OWN = {'guild_id': '123', 'channel_id': '50', 'conversation_id': 'private:19', 'user_id': '20'}


def own_work(said, generation):
    return {**OWN, 'source': said, 'revision': 0, 'work_id': '', 'version': 0, 'title': 'raid notes', 'body': SAID,
            'state': 'open', 'request_text': 'save that as a draft', 'summary_generation': generation}


def own_reminder(said, generation):
    from tests.test_fri_social import future
    return {**OWN, 'source': said, 'revision': 0, 'body': SAID, 'local_time': future(), 'timezone': 'UTC',
            'summary_generation': generation}


def test_b3_work_drawn_on_the_writers_own_summary_is_held_too(world):  # noqa: F811
    """Sol round 1's B3 (pre-existing): member 20's own draft and reminder drew on their own summary, which records no
    dependency, as their own summary going doesn't retire them (gene #112). Held, the summary still left them readable
    and the reminder claimable. They're kept out of use while it's held, and back once a recheck says ok."""
    client, store = world
    _, generation = summarized(client, store)
    draft = written(client, '/workspace/save', own_work(dm(1, user='20'), generation))
    note = written(client, '/social/reminders/prepare', own_reminder(dm(2, user='20'), generation))
    scheduled(store)
    recheck = held_back(client)
    assert draft['id'] not in str(fri(client, '/workspace/list', **OWN).json())
    assert fri(client, f"/workspace/read/{draft['id']}", **OWN).status_code == 423
    assert note['id'] not in str(fri(client, '/social/reminders/list', **OWN).json())
    assert fri(client, '/social/reminders/claim').json() is None
    answered(client, recheck, 'ok')
    assert fri(client, f"/workspace/read/{draft['id']}", **OWN).json()['body'] == SAID
    assert fri(client, '/social/reminders/claim').json()['id'] == note['id']


def test_b3_own_summary_work_still_isnt_retired_when_the_summary_goes(world):  # noqa: F811
    """As before (gene #112): they asked for it, so their summary going leaves it."""
    client, store = world
    _, generation = summarized(client, store)
    draft = written(client, '/workspace/save', own_work(dm(1, user='20'), generation))
    answered(client, held_back(client), 'changed')
    assert fri(client, f"/workspace/read/{draft['id']}", **OWN).json()['body'] == SAID
