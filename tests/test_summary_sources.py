"""HumanMint/fri#333 item 2: a member summary's claim names the Discord messages it's built from, with when each exchange
was saved, and those its earlier summary drew on, so Fri can check each is still there, as it was, before any of it is
sent or carried on. A deletion Fri never heard of then stops a build instead of reaching the model."""
import json

import pytest

from gene.fri_member_summary import finish
from gene.fri_protocol import contract
from tests.test_member_summaries import background, build, chat, offered


def tomorrow(store):
    with store.connection() as db:  # The member's call for today spent.
        db.execute('DELETE FROM fri_member_summary_calls')


def sources(store, user='20'):
    with store.connection() as db:
        rows = db.execute("SELECT json_extract(source,'$.id'),channel_id,at FROM chat_turns WHERE user_id=? "
                          "AND channel_id LIKE 'public:%' ORDER BY at, id", (user,)).fetchall()
    return [{'id': row[0], 'conversation_id': row[1], 'saved_at': row[2]} for row in rows]


def test_a_claim_names_each_exchanges_messages_and_when_it_was_saved(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    saved = sources(store)
    offer = offered(agent)
    assert [(row['messages'], row['saved_at']) for row in offer['exchanges']] == \
        [([source['id']], source['saved_at']) for source in saved]
    assert '"20"' not in json.dumps(offer)  # Still no member named.


def test_the_next_claim_names_what_the_earlier_summary_drew_on(store, tmp_path):
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    first = sources(store)
    offer, kept = build(agent)
    assert kept == {'kept': True}
    chat(store, 'the raid went well', ago=5400)
    chat(store, 'next raid friday', ago=5400)
    tomorrow(store)
    offer = offered(agent)
    assert offer['previous'] and offer['previous']['messages'] == first
    # Kept on: a summary built on that one names both.
    assert finish(agent, offer['id'], 'they raid on fridays', 'completed',
                  sorted({row['conversation_id'] for row in offer['exchanges']}), True) == {'kept': True}
    chat(store, 'raid recap', ago=4000)
    chat(store, 'raid recap two', ago=4000)
    tomorrow(store)
    assert [source['id'] for source in offered(agent)['previous']['messages']] == \
        [source['id'] for source in sources(store)[:4]]


def test_an_earlier_summary_whose_messages_arent_known_says_so(store, tmp_path):
    """Built before claims named them (or by an older controller since): what it drew on is unknown, so None, and Fri
    doesn't carry it on."""
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    build(agent)
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_sources')
    chat(store, 'the raid went well', ago=5400)
    chat(store, 'next raid friday', ago=5400)
    tomorrow(store)
    offer = offered(agent)
    assert offer['previous'] and offer['previous']['messages'] is None


def test_a_summary_built_without_the_earlier_one_names_only_its_own(store, tmp_path):
    """The earlier summary not carried on (Fri found one of its messages gone): the new one draws on none of it."""
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    build(agent)
    chat(store, 'the raid went well', ago=5400)
    chat(store, 'next raid friday', ago=5400)
    tomorrow(store)
    build(agent, text='they raid on fridays', previous=False)
    chat(store, 'raid recap', ago=4000)
    chat(store, 'raid recap two', ago=4000)
    tomorrow(store)
    assert offered(agent)['previous']['messages'] == sources(store)[2:4]


def test_past_what_fri_checks_in_one_build_what_a_summary_drew_on_isnt_known(store, tmp_path, monkeypatch):
    """A chain of summaries each carried on names ever more: past DRAWN_MOST, none, so Fri doesn't carry it on."""
    import gene.fri_member_summary as summaries
    monkeypatch.setattr(summaries, 'DRAWN_MOST', 3)
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    build(agent)
    chat(store, 'the raid went well', ago=5400)
    chat(store, 'next raid friday', ago=5400)
    tomorrow(store)
    build(agent, text='they raid on fridays')  # Carried on: four messages, past three.
    chat(store, 'raid recap', ago=4000)
    chat(store, 'raid recap two', ago=4000)
    tomorrow(store)
    assert offered(agent)['previous']['messages'] is None


def test_only_the_messages_fri_sent_are_named_as_drawn_on(store, tmp_path):
    """Fri left an exchange out (one of its messages gone in Discord): the summary names only what Fri sent."""
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid')
    chat(store, 'and the boss')
    first = sources(store)
    offer = offered(agent)
    sent = [first[0]['id'], first[2]['id']]
    assert finish(agent, offer['id'], 'they raid', 'completed', ['public:1'], False, sent) == {'kept': True}
    chat(store, 'the raid went well', ago=5400)
    chat(store, 'next raid friday', ago=5400)
    tomorrow(store)
    assert [m['id'] for m in offered(agent)['previous']['messages']] == sent


def kept_sources(store, user='20'):
    with store.connection() as db:
        return db.execute('SELECT count(*) FROM fri_member_summary_sources WHERE user_id=?', (user,)).fetchone()[0]


@pytest.mark.parametrize('how', ['forget me', 'memory off', 'clear chats', 'erase user', 'an older controller',
                                 'expiry', 'their own clear'])
def test_what_a_summary_drew_on_goes_with_it(store, tmp_path, how, monkeypatch):
    """Sol round 1 of #210, B1: forget me, clearing chats and erasing a member took their summary but kept the record of
    what it drew on (their ID, message IDs, channels, save times), and nothing ever pruned it. It goes with the summary
    however it goes, by a database trigger, so a controller rolled back to one that doesn't know the record, deleting
    the summary as it always did, takes it too."""
    import gene.fri_member_summary as summaries
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    build(agent)
    assert kept_sources(store) == 1
    if how == 'forget me':
        store.erase_user('20')
    elif how == 'memory off':
        store.personal_control('20', 'disable')
    elif how == 'clear chats':
        store.clear_chats('20')
    elif how == 'erase user':
        with store.connection() as db:
            summaries.erase(db, '20')
    elif how == 'an older controller':
        with store.connection() as db:  # As 4ffd668 deletes it: the summary row alone.
            db.execute('DELETE FROM fri_member_summaries WHERE user_id=?', ('20',))
    elif how == 'expiry':
        monkeypatch.setattr(summaries.time, 'time', lambda: 10**10)
        with store.connection() as db:
            summaries.prune(db)
    else:
        with store.connection() as db:  # Their own clear in My memory: the row goes, and an empty one replaces it.
            summaries.drop(db, '123', '20')
            db.execute("INSERT INTO fri_member_summaries VALUES ('123','20','',0,'[]','[]',1,1)")
    assert kept_sources(store) == 0


def test_an_older_controller_replacing_a_summary_leaves_no_record_of_the_old_one(store, tmp_path):
    """One that rewrites the summary row in place, as 4ffd668's finish does: what the old one drew on goes."""
    agent = background(store, tmp_path)
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    build(agent)
    with store.connection() as db:
        db.execute('UPDATE fri_member_summaries SET body=?, through=through+1 WHERE user_id=?', ('newer', '20'))
    assert kept_sources(store) == 0


def test_a_record_of_a_summary_thats_gone_is_swept_at_start(store, tmp_path):
    """One left from before the trigger, or by a database restored without it: swept when the controller starts."""
    from gene.store import Store
    with store.connection() as db:
        db.execute("INSERT INTO fri_member_summary_sources VALUES ('123','20',1,'[]')")
    assert kept_sources(store) == 1
    Store(store.path)  # Started again.
    assert kept_sources(store) == 0


# Sol round 2 of #210, B5: a claim Fri never finished kept the member's message IDs, channels and save times in
# agent_state (its own, and for a rebuild the earlier summary's) past their erasure, a restart and later claims.

def claimed(store):
    """The summary claim in flight, as stored, or None."""
    from gene.fri_member_summary import CLAIM
    with store.connection() as db:
        row = db.execute('SELECT value FROM agent_state WHERE key=?', (CLAIM,)).fetchone()
    return json.loads(row[0]) if row else None


def interrupted(store, agent, rebuild):
    """Two settled exchanges claimed, and Fri gone before it finishes; for a rebuild, an earlier summary first."""
    chat(store, 'raid tonight?')
    chat(store, 'what build for the raid', 'public:2')
    if rebuild:
        build(agent)
        chat(store, 'the raid went well', ago=5400)
        chat(store, 'next raid friday', ago=5400)
        tomorrow(store)
    offer = offered(agent)
    record = claimed(store)
    assert offer and record['messages'] and record['messages'][0]['saved_at']
    assert bool((record['previous'] or {}).get('messages')) is rebuild
    return offer


def erased(store, how):
    """The member's privacy changes, as each controller does it."""
    from gene.fri_member_summary import edit as edit_summary
    from tests.test_member_summaries import private_source
    if how == 'forget me':
        store.erase_user('20')
    elif how == 'memory off':
        store.personal_control('20', 'disable')
    elif how == 'clear chats':
        store.clear_chats('20')
    elif how == 'their own clear':  # Clear, in My memory.
        revision = store.chat_context('123', 'private:9', '20')['revision']
        edit_summary(store, '123', 'private:9', '20', revision, private_source(), 'remove')
    else:  # An older controller's erasure (4ffd668's, 34dbbb3's), of a database this one wrote: it moves the revision.
        with store.connection() as db:
            db.execute('INSERT INTO chat_revisions VALUES (?,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1',
                       ('20',))


ERASURES = [(how, rebuild) for how in ('forget me', 'memory off', 'clear chats', 'an older controller')
            for rebuild in (False, True)] + [('their own clear', True)]


@pytest.mark.parametrize(('how', 'rebuild'), ERASURES)
def test_a_claim_fri_never_finished_goes_with_the_members_erasure(store, tmp_path, how, rebuild):
    """The repro: save two settled exchanges, claim, stop before finish, erase. The claim named their message IDs,
    channels and save times, and for a rebuild the earlier summary's; all of it goes in the erasure's own transaction,
    by a trigger on the revision every privacy change moves, so an older controller's erasure takes it too, and a
    restart finds none."""
    from gene.store import Store
    agent = background(store, tmp_path)
    interrupted(store, agent, rebuild)
    erased(store, how)
    assert claimed(store) is None
    Store(store.path)  # Started again.
    assert claimed(store) is None


def test_another_members_erasure_leaves_the_claim(store, tmp_path):
    agent = background(store, tmp_path)
    offer = interrupted(store, agent, False)
    store.clear_chats('30')
    assert claimed(store)['id'] == offer['id']


def without_claim_triggers(store):
    """A database from before the claim's triggers, or restored without them."""
    with store.connection() as db:
        for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='chat_revisions' "
                                  "AND name LIKE 'fri_member_summary%'").fetchall():
            db.execute(f'DROP TRIGGER {name}')


@pytest.mark.parametrize('stale', ['its member erased', 'its member\'s memory off', 'past its time'])
def test_a_stale_claim_is_swept_at_start(store, tmp_path, stale):
    """One left by a controller from before this, by a database restored without the trigger, or by a Fri that never
    finished: whatever it names goes when the controller starts."""
    from gene.fri_member_summary import CLAIM, CLAIM_TIMEOUT
    from gene.store import Store
    agent = background(store, tmp_path)
    interrupted(store, agent, True)
    without_claim_triggers(store)
    with store.connection() as db:
        if stale == 'its member erased':
            db.execute('INSERT INTO chat_revisions VALUES (?,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1',
                       ('20',))
        elif stale == 'past its time':
            db.execute("UPDATE agent_state SET value=json_set(value,'$.at',json_extract(value,'$.at')-?) WHERE key=?",
                       (CLAIM_TIMEOUT+1, CLAIM))
        else:
            db.execute('INSERT INTO personal_preferences VALUES (?,0) ON CONFLICT(user_id) DO UPDATE SET enabled=0',
                       ('20',))
    assert claimed(store) is not None
    Store(store.path)  # Started again.
    assert claimed(store) is None


def test_a_claim_in_its_time_whose_member_changed_nothing_outlasts_a_restart(store, tmp_path):
    """Guard: a controller restarted while Fri builds still keeps what Fri then finishes."""
    from gene.store import Store
    agent = background(store, tmp_path)
    offer = interrupted(store, agent, False)
    Store(store.path)
    assert finish(agent, offer['id'], 'they plan raids', 'completed',
                  sorted({row['conversation_id'] for row in offer['exchanges']}), False) == {'kept': True}


def test_a_claim_past_its_time_goes_at_the_next_look_with_nobody_due(store, tmp_path):
    """The repro's tail: later looks with nobody due left a claim Fri never finished as it was. It's spent once past
    its time, so the next look takes it, whoever is due."""
    from gene.fri_member_summary import CLAIM, CLAIM_TIMEOUT
    agent = background(store, tmp_path)
    interrupted(store, agent, True)
    with store.connection() as db:
        db.execute("UPDATE agent_state SET value=json_set(value,'$.at',json_extract(value,'$.at')-?) WHERE key=?",
                   (CLAIM_TIMEOUT+1, CLAIM))
    assert offered(agent) is None  # Their call for today is spent: nobody is due.
    assert claimed(store) is None


def test_the_controller_says_it_names_them():
    assert 'summary-sources' in contract()['capabilities']
