"""HumanMint/fri#333 item 2: a member summary's claim names the Discord messages it's built from, with when each exchange
was saved, and those its earlier summary drew on, so Fri can check each is still there, as it was, before any of it is
sent or carried on. A deletion Fri never heard of then stops a build instead of reaching the model."""
import json

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


def test_the_controller_says_it_names_them():
    assert 'summary-sources' in contract()['capabilities']
