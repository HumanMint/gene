"""HumanMint/fri#349: a kept member summary is rechecked against the messages it drew on, even when nothing rebuilds
it. A rebuild needs new exchanges, so a summary whose exchanges aged out was never checked again, and one of its
messages deleted where Fri never heard of it kept reaching other channels until it expired.

A Fri offering the recheck claims a few kept summaries at a time, with what each drew on, reads those messages again
from Discord, and says how it went: still there as they were (`ok`), not checkable now (`unchecked`, held back until a
later `ok`), or one gone or edited since (`changed`, dropped). The controller drops one whose sources aren't known, or
one of whose messages it knows was taken back, as it claims. Fri's first claim after it starts holds every summary
back until it's rechecked.
"""
import json

import pytest

from gene.fri_member_summary import (
    CLAIM_TIMEOUT,
    RECHECK,
    finish,
    generation,
    lapsed,
    read,
    recheck,
    rechecked,
)
from gene.fri_protocol import contract
from tests.test_api import headers
from tests.test_gene_controls import controls_api  # noqa: F401
from tests.test_member_summaries import background, chat, offered, shown
from tests.test_summary_sources import sources, tomorrow


def built(store, agent, user='20', messages=False):
    """A kept summary of the member's, from a Fri that doesn't name what it sent (an older one), or, with messages,
    one that checked them first."""
    chat(store, 'raid tonight?', user=user)
    chat(store, 'what build for the raid', 'public:2', user=user)
    offer = offered(agent)
    assert offer, 'nobody was due a summary'
    named = [m for row in offer['exchanges'] for m in row['messages']] if messages else None
    assert finish(agent, offer['id'], f'they planned a raid ({user})', 'completed',
                  sorted({row['conversation_id'] for row in offer['exchanges']}), False, named) == {'kept': True}
    return offer


def later(monkeypatch, seconds):
    import gene.fri_member_summary as summaries
    now = summaries.time.time
    monkeypatch.setattr(summaries.time, 'time', lambda: now()+seconds)


def test_a_summary_no_fri_checked_is_offered_for_a_recheck_with_what_it_drew_on(store, tmp_path):
    agent = background(store, tmp_path)
    built(store, agent)
    claim = recheck(agent)
    assert [item['messages'] for item in claim['summaries']] == [sources(store)]
    assert '"20"' not in json.dumps(claim)  # No member named.
    assert shown(store)  # Not held back meanwhile.


def test_ok_keeps_it_shown_and_not_offered_again_until_a_recheck_is_due(store, tmp_path, monkeypatch):
    agent = background(store, tmp_path)
    built(store, agent)
    item = recheck(agent)['summaries'][0]
    assert rechecked(agent, item['id'], 'ok') == {'ok': True}
    assert shown(store) and recheck(agent) == {'summaries': []}
    later(monkeypatch, RECHECK+1)
    assert len(recheck(agent)['summaries']) == 1


def test_a_build_by_a_fri_that_checked_its_messages_counts_as_a_check(store, tmp_path):
    agent = background(store, tmp_path)
    built(store, agent, messages=True)
    assert recheck(agent) == {'summaries': []} and shown(store)


def test_unchecked_holds_it_back_until_a_later_ok(store, tmp_path, monkeypatch):
    agent = background(store, tmp_path)
    built(store, agent)
    item = recheck(agent)['summaries'][0]
    rechecked(agent, item['id'], 'unchecked')
    assert shown(store) is None  # Held back, not dropped:
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_member_summaries').fetchone()[0] == 1
        assert read(db, '123', '20') is None and not lapsed(db, '123', '20')
    assert recheck(agent) == {'summaries': []}  # Not again at once.
    later(monkeypatch, CLAIM_TIMEOUT+1)
    item = recheck(agent)['summaries'][0]
    rechecked(agent, item['id'], 'ok')
    assert shown(store)


def test_changed_drops_it(store, tmp_path):
    agent = background(store, tmp_path)
    built(store, agent)
    with store.connection() as db:
        before = generation(db, '123', '20')
    item = recheck(agent)['summaries'][0]
    rechecked(agent, item['id'], 'changed')
    with store.connection() as db:
        assert not db.execute('SELECT count(*) FROM fri_member_summaries').fetchone()[0]
        assert generation(db, '123', '20') == before+1  # Replies that drew on it keep nothing of it.


def test_fris_first_claim_after_it_starts_holds_every_summary_back_until_rechecked(store, tmp_path):
    agent = background(store, tmp_path)
    built(store, agent, messages=True)
    built(store, agent, user='30', messages=True)
    claim = recheck(agent, fresh=True)
    assert shown(store) is None and shown(store, '30') is None
    assert len(claim['summaries']) == 2
    for item in claim['summaries']:
        rechecked(agent, item['id'], 'ok')
    assert shown(store) and shown(store, '30')


def test_one_whose_sources_arent_known_is_dropped_at_the_next_recheck(store, tmp_path):
    """Written by a controller from before they were recorded, or past what one names: it can never be checked, so it
    goes rather than being held back unused until it expires, and the next build starts from what can be."""
    agent = background(store, tmp_path)
    built(store, agent)
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_sources')
    assert recheck(agent) == {'summaries': []}
    with store.connection() as db:
        assert not db.execute('SELECT count(*) FROM fri_member_summaries').fetchone()[0]


def test_one_whose_message_the_controller_knows_was_taken_back_is_dropped_as_it_is_claimed(store, tmp_path):
    """The gap: its exchange aged out, so a withdrawal of that message by ID finds nothing holding it and spares the
    summary. The controller knows it was taken back, so the summary goes without Fri reading anything."""
    agent = background(store, tmp_path)
    built(store, agent)
    message = sources(store)[0]['id']
    with store.connection() as db:
        db.execute("DELETE FROM chat_turns WHERE user_id='20'")  # Aged out.
    store.withdraw_saved('123', [message], deleted=True)
    assert shown(store)  # Spared, as nothing held it.
    assert recheck(agent) == {'summaries': []}
    assert shown(store) is None


def test_a_message_taken_back_before_its_exchange_was_saved_leaves_the_summary(store, tmp_path):
    """An edit Fri answered again under the same ID: the summary drew on what it says now, so it stays and is offered."""
    agent = background(store, tmp_path)
    built(store, agent)
    first = sources(store)[0]
    with store.connection() as db:
        db.execute("INSERT INTO chat_turn_withdrawals VALUES ('123','20',?,?)", (first['id'], first['saved_at']-60))
    assert len(recheck(agent)['summaries']) == 1 and shown(store)


def test_each_claim_reads_a_bounded_number_of_messages(store, tmp_path, monkeypatch):
    import gene.fri_member_summary as summaries
    monkeypatch.setattr(summaries, 'READS', 3)
    agent = background(store, tmp_path)
    for user in ('20', '30', '40'):
        built(store, agent, user=user)
    first = recheck(agent)['summaries']
    assert len(first) == 1 and len(first[0]['messages']) == 2  # At least one, whatever it names.
    second = recheck(agent)['summaries']
    assert len(second) == 1 and second[0]['id'] != first[0]['id']  # A claimed one isn't offered again meanwhile.


def test_a_recheck_of_a_summary_replaced_since_changes_nothing(store, tmp_path):
    agent = background(store, tmp_path)
    built(store, agent)
    item = recheck(agent)['summaries'][0]
    chat(store, 'the raid went well', ago=5400)
    chat(store, 'next raid friday', ago=5400)
    tomorrow(store)
    offer = offered(agent)
    finish(agent, offer['id'], 'they raid on fridays', 'completed',
           sorted({row['conversation_id'] for row in offer['exchanges']}), True,
           [m for row in offer['exchanges'] for m in row['messages']])
    with pytest.raises(ValueError, match='expired'):
        rechecked(agent, item['id'], 'changed')
    assert shown(store)['text'] == 'they raid on fridays'


def test_a_build_still_carries_a_summary_held_back_for_a_recheck(store, tmp_path):
    """The build reads its messages again itself before sending any (fri #333), so a held one is still offered."""
    agent = background(store, tmp_path)
    built(store, agent, messages=True)
    recheck(agent, fresh=True)
    assert shown(store) is None
    chat(store, 'the raid went well', ago=5400)
    chat(store, 'next raid friday', ago=5400)
    tomorrow(store)
    offer = offered(agent)
    assert offer['previous'] and offer['previous']['messages']


@pytest.mark.parametrize('how', ['forget me', 'clear chats', 'an older controller rewriting it', 'expiry'])
def test_what_a_recheck_records_goes_with_its_summary(store, tmp_path, how, monkeypatch):
    import gene.fri_member_summary as summaries
    agent = background(store, tmp_path)
    built(store, agent)
    item = recheck(agent)['summaries'][0]
    rechecked(agent, item['id'], 'unchecked')
    if how == 'forget me':
        store.erase_user('20')
    elif how == 'clear chats':
        store.clear_chats('20')
    elif how == 'expiry':
        later(monkeypatch, summaries.TTL+1)
        with store.connection() as db:
            summaries.prune(db)
    else:
        with store.connection() as db:
            db.execute("UPDATE fri_member_summaries SET body='newer', through=through+1")
    with store.connection() as db:
        assert not db.execute('SELECT count(*) FROM fri_member_summary_rechecks').fetchone()[0]


def test_a_record_of_a_summary_thats_gone_is_swept_at_start(store, tmp_path):
    from gene.store import Store
    with store.connection() as db:
        db.execute("INSERT INTO fri_member_summary_rechecks VALUES ('123','20',1,0,1,'',0)")
    Store(store.path)
    with store.connection() as db:
        assert not db.execute('SELECT count(*) FROM fri_member_summary_rechecks').fetchone()[0]


def test_rechecks_over_the_api(controls_api):  # noqa: F811
    client, _ = controls_api
    store, agent = client.app.state.store, client.app.state.agent
    agent.update({'enabled': True}, 0, '10')
    built(store, agent)
    claim = client.post('/agent/member-summary/recheck', headers=headers('fri'), json={'fresh': False}).json()
    assert client.post('/agent/member-summary/recheck', headers=headers('runtime'), json={}).status_code == 401
    item = claim['summaries'][0]
    answer = client.post(f"/agent/member-summary/recheck/{item['id']}", headers=headers('fri'), json={'status': 'ok'})
    assert answer.status_code == 200
    assert client.post('/agent/member-summary/recheck/nope', headers=headers('fri'),
                       json={'status': 'ok'}).status_code in (400, 422)
    assert client.post(f"/agent/member-summary/recheck/{item['id']}", headers=headers('fri'),
                       json={'status': 'maybe'}).status_code == 422


def test_the_purge_takes_the_recheck_record_too(store, tmp_path):
    from gene.fri_member_summary import purge
    agent = background(store, tmp_path)
    built(store, agent)
    recheck(agent)
    purge(str(store.path))
    with store.connection() as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='fri_member_summary_rechecks'").fetchone()


def test_the_controller_offers_rechecks():
    assert 'summary-rechecks' in contract()['capabilities']


# Sol round 1 of #214.

def replaced(store, text='mostly raids and builds'):
    """The member's own Edit summary, in My memory."""
    from gene.fri_member_summary import edit
    from tests.test_member_summaries import private_source
    revision = store.chat_context('123', 'private:9', '20')['revision']
    edit(store, '123', 'private:9', '20', revision, private_source(), 'replace', text)


@pytest.mark.parametrize('fresh', [False, True], ids=['a recheck', "fri's first claim after it starts"])
def test_b1_a_summary_the_member_wrote_draws_on_no_message_and_survives_a_recheck(store, tmp_path, fresh):
    """B1: Edit summary wrote the member's own words with no record of what it drew on, so the next recheck took it for
    one whose sources aren't known and dropped it for good. It draws on no message: nothing to check, so it shows."""
    from gene.store import Store
    agent = background(store, tmp_path)
    built(store, agent)
    replaced(store)
    assert recheck(agent, fresh=fresh) == {'summaries': []}
    assert shown(store)['text'] == 'mostly raids and builds'
    Store(store.path)  # Started again: what it draws on is still known.
    assert recheck(agent) == {'summaries': []} and shown(store)['text'] == 'mostly raids and builds'


def test_b1_one_an_older_controller_wrote_for_the_member_survives_too(store, tmp_path):
    """Written by a controller from before sources were recorded: no record, but it names no conversation it came from,
    as only the member's own words don't, so it's theirs, not a summary whose sources are unknown."""
    agent = background(store, tmp_path)
    built(store, agent)
    replaced(store)
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_sources')
    assert recheck(agent) == {'summaries': []} and shown(store)['text'] == 'mostly raids and builds'


def test_b1_the_next_build_carries_the_members_words_on(store, tmp_path):
    agent = background(store, tmp_path)
    built(store, agent)
    replaced(store)
    chat(store, 'the raid went well', ago=5400)
    chat(store, 'next raid friday', ago=5400)
    tomorrow(store)
    offer = offered(agent)
    assert offer['previous'] == {'text': 'mostly raids and builds', 'origins': [], 'messages': []}


def test_s1_a_withdrawal_recorded_while_fri_read_takes_the_summary_whatever_fri_answers(store, tmp_path):
    """S1: one of its messages was taken back after the claim, while Fri was still reading; Fri's ok, from reads made
    before that, never shows it again."""
    agent = background(store, tmp_path)
    built(store, agent)
    item = recheck(agent)['summaries'][0]
    with store.connection() as db:
        db.execute("DELETE FROM chat_turns WHERE user_id='20'")  # Aged out: nothing holds it but the summary.
    store.withdraw_saved('123', [item['messages'][0]['id']], deleted=True)
    rechecked(agent, item['id'], 'ok')
    assert shown(store) is None
    with store.connection() as db:
        assert not db.execute('SELECT count(*) FROM fri_member_summaries').fetchone()[0]
