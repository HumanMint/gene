"""fri #216: what a turn keeps from Fri's replies that drew on another member's summary, beyond its own reply.

1. A member's saved exchanges come back as history. One whose reply depended on a summary (the member's own, or another
   member's through an echo or a reply of Fri's it answered) reports those summaries, when Fri asks, so a turn reading
   it as history depends on them in turn (`summaries`, beside nothing the model reads).
2. Saved work and reminders the model wrote take the turn's `summary_dependencies`, are refused once one moves, inside
   the write's own transaction, and keep them: a summary going later retires what they hold of it.

Both behind the optional capability `saved-dependencies`. An older Fri asks for and sends neither: answered as before.
"""

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_protocol import compatible, contract
from gene.store import Store
from tests.test_api import headers
from tests.test_fri_endings import source
from tests.test_summary_dependencies import ECHOED, depending, rebuilt, repeated, saved_reply
from tests.test_summary_generation_retention import lapsed, quoted, retired, summarized
from tests.test_turn_withdrawal import MARKER, claimed, readable, summary_generation_now, withdraw

THEIRS = {'guild_id': '123', 'channel_id': '50', 'conversation_id': 'private:19', 'user_id': '30'}
SAID = 'bring the '+MARKER


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'saved.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def history(client, user='30', conversation='public:51', **asked):
    channel = conversation.split(':')[1] if conversation.startswith('public:') else '50'
    answer = client.post('/chat/context', headers=headers('fri'), json={
        'guild_id': '123', 'channel_id': channel, 'conversation_id': conversation, 'user_id': user,
        'profiles': True, 'summaries': True, **asked})
    assert answer.status_code == 200, answer.text
    return answer.json()


def dm(age=1, user='30'):
    return source(age=age, user=user, channel='19', guild=None)


def work(said, dependencies=None, body=SAID, **extra):
    return {**THEIRS, 'source': said, 'revision': 0, 'work_id': '', 'version': 0, 'title': 'raid notes',
            'body': body, 'state': 'open', 'request_text': 'save that as a draft',
            **({} if dependencies is None else {'summary_dependencies': dependencies}), **extra}


def reminder(said, dependencies=None, body=SAID):
    from tests.test_fri_social import future
    return {**THEIRS, 'source': said, 'revision': 0, 'body': body, 'local_time': future(), 'timezone': 'UTC',
            **({} if dependencies is None else {'summary_dependencies': dependencies})}


WRITES = [('/workspace/save', work, 'fri_work'), ('/social/reminders/prepare', reminder, 'fri_reminders')]


def written(client, route, body):
    answer = client.post(route, headers=headers('fri'), json=body)
    assert answer.status_code == 200, answer.text
    return answer.json()


def rows(store, table):
    with store.connection() as db:
        return {row['id']: dict(row) for row in db.execute(f'SELECT * FROM {table}')}


def holds(store):
    """Whether any saved work or reminder still holds the summary's words."""
    return MARKER in str(rows(store, 'fri_work')) + str(rows(store, 'fri_reminders'))


def test_history_reports_the_summaries_each_saved_exchange_drew_on(world):
    """Item 1: B's saved reply that repeated A's summary-derived echo says so when it comes back as B's history."""
    client, store = world
    _, generation = summarized(client, store)
    quoted(store, generation, conversation='public:51')  # Member 20's own reply on their summary.
    repeated(client, generation)  # Member 30's, depending on it.
    plain = history(client)['turns']
    asked = history(client, dependencies=True)['turns']
    assert [turn.get('summaries') for turn in asked] == [depending(generation)]
    assert history(client, '20', dependencies=True)['turns'][-1]['summaries'] == depending(generation)
    # Nothing else in the answer changes, and without asking nothing does at all.
    assert [{k: v for k, v in turn.items() if k != 'summaries'} for turn in asked] == plain
    assert 'summaries' not in str(plain) and 'summaries' not in str(history(client, '20')['turns'])


def test_an_exchange_drawing_on_no_summary_reports_none(world):
    client, store = world
    readable(store, 'just a plain chat', 30, user='30')
    assert ['summaries' in turn for turn in history(client, dependencies=True)['turns']] == [False]


def test_a_history_read_retires_an_exchange_whose_summary_lapsed_first(world):
    """As the sources lookup does (gene #120): a lapsed summary goes before the turns are read, taking what drew on it."""
    client, store = world
    _, generation = summarized(client, store)
    repeated(client, generation)
    lapsed(store)
    [turn] = history(client, dependencies=True)['turns']
    assert retired(turn['reply']) and 'summaries' not in turn
    assert summary_generation_now(store) != generation and retired(saved_reply(store))


@pytest.mark.parametrize('route,body,table', WRITES)
def test_a_write_depending_on_a_withdrawn_summary_is_refused(world, route, body, table):
    """Item 2: B's write lands after A's summary source was withdrawn; nothing is kept."""
    client, store = world
    taken, generation = summarized(client, store)
    withdraw(client, taken)
    refused = client.post(route, headers=headers('fri'), json=body(dm(), depending(generation)))
    assert refused.status_code == 409 and MARKER not in refused.text
    assert rows(store, table) == {}
    written(client, route, body(dm(2), depending(summary_generation_now(store))))
    written(client, route, body(dm(3)))  # An older Fri sends none: as before.
    assert len(rows(store, table)) == 2


@pytest.mark.parametrize('route,body,table', WRITES)
def test_a_write_depending_on_a_summary_that_lapsed_since_is_refused(world, route, body, table):
    client, store = world
    _, generation = summarized(client, store)
    lapsed(store)
    assert client.post(route, headers=headers('fri'), json=body(dm(), depending(generation))).status_code == 409
    assert rows(store, table) == {} and summary_generation_now(store) != generation


def test_writes_depending_on_a_summary_go_when_it_does(world):
    """Item 2's other half: written while A's summary was current, B's work and reminder keep nothing of it once it
    goes. Their other writes stay."""
    client, store = world
    taken, generation = summarized(client, store)
    kept_work = written(client, '/workspace/save', work(dm(1), depending(generation)))
    kept_reminder = written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    other_work = written(client, '/workspace/save', work(dm(3), body='my own list'))
    other_reminder = written(client, '/social/reminders/prepare', reminder(dm(4), body='stretch'))
    assert withdraw(client, taken).json()['removed'] == 1
    assert not holds(store)
    works, reminders = rows(store, 'fri_work'), rows(store, 'fri_reminders')
    assert works[kept_work['id']]['version'] == 2 and works[kept_work['id']]['body'] != SAID
    assert (reminders[kept_reminder['id']]['state'], reminders[kept_reminder['id']]['body']) == ('cancelled', '')
    assert works[other_work['id']]['body'] == 'my own list' and reminders[other_reminder['id']]['body'] == 'stretch'
    assert MARKER not in str(history(client, conversation='private:19')['loops'])


def test_a_scheduled_reminder_depending_on_it_is_cancelled_and_a_sent_one_keeps_no_words(world):
    client, store = world
    taken, generation = summarized(client, store)
    scheduled = written(client, '/social/reminders/prepare', reminder(dm(1), depending(generation)))
    sent = written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    with store.connection() as db:
        db.execute("UPDATE fri_reminders SET state='scheduled' WHERE id=?", (scheduled['id'],))
        db.execute("UPDATE fri_reminders SET state='sent',message_id='5' WHERE id=?", (sent['id'],))
    withdraw(client, taken)
    reminders = rows(store, 'fri_reminders')
    assert [(reminders[i]['state'], reminders[i]['body']) for i in (scheduled['id'], sent['id'])] == [
        ('cancelled', ''), ('sent', '')]


def test_a_rebuild_sent_the_old_summary_carries_dependent_writes_on(world):
    """As for saved replies: the daily rebuild keeps the old summary's lineage, so the write stays and now depends on the
    new one, which a withdrawal of what the old one drew on still takes."""
    from gene.fri_member_summary import finish
    client, store = world
    taken, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    readable(store, 'new plans over here', 5000, conversation='public:52')
    readable(store, 'and more of them', 4900, conversation='public:52')
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    claim_id, conversations, _ = claimed(client.app.state.agent)
    assert finish(client.app.state.agent, claim_id, 'they made new plans', 'completed', conversations, True)['kept']
    assert summary_generation_now(store) != generation and holds(store)
    Store(store.path)  # A restart finds them current.
    assert holds(store)
    withdraw(client, taken)
    assert not holds(store)


@pytest.mark.parametrize('erase', ['disable', 'erase_user', 'clear_chats'])
def test_the_summarized_members_privacy_change_retires_dependent_writes(world, erase):
    client, store = world
    _, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    if erase == 'disable':
        store.personal_control('20', 'disable')
    else:
        getattr(store, erase)('20')
    assert not holds(store)


def test_what_a_summary_going_leaves_alone_in_writes(world):
    """Another member's summary's dependents; a write on the writer's own summary, which stays as gene #112 decided for
    work and reminders; and saved work in a channel, which is the member's own words from their own message."""
    client, store = world
    taken, generation = summarized(client, store)
    readable(store, 'i main a healer', 7300, user='40', conversation='public:52')
    readable(store, 'see you at the raid', 7250, user='40', conversation='public:52')
    from tests.test_turn_withdrawal import built
    built(client.app.state.agent, 'they main a healer')
    theirs = written(client, '/workspace/save', work(dm(1), depending(summary_generation_now(store, '40'), '40')))
    own = written(client, '/social/reminders/prepare', {**reminder(dm(2), depending(0, '30')), 'body': 'own '+SAID})
    said = source(age=3, user='30', channel='51')
    public = written(client, '/workspace/save', {**work(said, depending(generation)), 'channel_id': '51',
                                                  'conversation_id': 'public:51', 'request_text': 'save '+SAID})
    withdraw(client, taken)
    works, reminders = rows(store, 'fri_work'), rows(store, 'fri_reminders')
    assert works[theirs['id']]['body'] == SAID and reminders[own['id']]['body'] == 'own '+SAID
    assert works[public['id']]['body'] == SAID


def test_a_restart_retires_dependent_writes_an_older_controller_left(world):
    """A rollback window: the older controller moved or erased the generation without retiring dependent writes."""
    client, store = world
    _, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    with store.connection() as db:  # As an older controller's drop does.
        db.execute("UPDATE fri_member_summary_generations SET generation=generation+1 WHERE user_id='20'")
    assert holds(store)
    Store(store.path)
    assert not holds(store)
    generation = rebuilt(client, store)
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    with store.connection() as db:  # As its erasure does: the generation goes outright.
        db.execute("DELETE FROM fri_member_summary_generations WHERE user_id='20'")
    Store(store.path)
    assert not holds(store)


@pytest.fixture
def older(monkeypatch):
    """A controller before gene #154, run in a rollback window: it knows nothing of the writes' dependencies, so its
    privacy changes leave them where they are."""
    from contextlib import contextmanager

    import gene.fri_member_summary as summaries

    @contextmanager
    def running():
        with monkeypatch.context() as patched:
            patched.setattr(summaries, 'retire_writes', lambda *args, **kwargs: None)
            yield
    return running


def dependencies(store):
    with store.connection() as db:
        return [tuple(row) for table in ('fri_work_dependencies', 'fri_reminder_dependencies')
                for row in db.execute(f'SELECT * FROM {table}')]


def built_again(client, store):
    """Member 20, back with memory on: two readable exchanges, a summary built from them, and a reply that drew on it,
    which records its generation (zero, as nothing moved it since the erasure deleted it)."""
    import time

    from tests.test_turn_withdrawal import built
    with store.connection() as db:
        revision = store._privacy_state(db, '20')[0]
    for text, age in (('raid again, bring the '+MARKER, 7200), ('same build again', 7150)):
        store.save_chat('123', 'public:51', '20', revision, text, 'okay', 'model', 'high',
                        source=source(age=age, channel='51'), provenance=[], audience='server')
        with store.connection() as db:
            db.execute('UPDATE chat_turns SET at=? WHERE id=(SELECT max(id) FROM chat_turns)', (time.time()-age+1,))
    with store.connection() as db:
        db.execute('DELETE FROM fri_member_summary_calls')
    built(client.app.state.agent, 'they planned another raid with the '+MARKER)
    store.save_chat('123', 'public:51', '20', revision, 'what did i plan', 'your raid', 'model', 'high',
                    source=source(age=1, channel='51'), provenance=[], reply_message_id='705', verbatim=True,
                    summary_generation=summary_generation_now(store))


@pytest.mark.parametrize('enabled', [True, False], ids=['enabled-again', 'still-off'])
def test_a_restart_retires_writes_whose_dependency_an_older_controller_erased(world, older, enabled):
    """Round 1 of gene #154: A turns memory off on an older controller, which leaves B's writes and their link to A. A
    turns it back on, a summary is built again and a reply records generation zero again, the number the writes kept.
    A start here still retires them: A's privacy revision moved since they were written."""
    client, store = world
    _, generation = summarized(client, store)
    assert generation == 0
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    with older():
        store.personal_control('20', 'disable')
        if enabled:
            store.personal_control('20', 'enable')
            built_again(client, store)
            assert summary_generation_now(store) == 0
    # The database's own triggers retire them as the older controller moves A's revision (delta review, 2026-10-29),
    # and a start here finds nothing left to do.
    assert not holds(store) and dependencies(store) == []
    Store(store.path)
    assert not holds(store) and dependencies(store) == []
    assert len(rows(store, 'fri_work')) == 1 and len(rows(store, 'fri_reminders')) == 1  # B's items stay, emptied.


def test_a_plain_restart_keeps_current_dependent_writes(world):
    client, store = world
    _, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    Store(store.path)
    assert holds(store)


def test_a_purge_before_rollback_retires_dependent_writes_too(world):
    from gene.fri_member_summary import purge
    client, store = world
    _, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    purge(store.path)
    assert not holds(store)


def test_a_retried_write_answers_the_same_with_or_without_its_dependencies(world):
    """They stay out of the idempotency digests, so a retry across an upgrade or rollback matches."""
    client, store = world
    _, generation = summarized(client, store)
    said = dm()
    first = written(client, '/workspace/save', work(said, depending(generation)))
    assert written(client, '/workspace/save', work(said)) == first
    said = dm(2)
    first = written(client, '/social/reminders/prepare', reminder(said, depending(generation)))
    assert written(client, '/social/reminders/prepare', reminder(said)) == first


def test_dependencies_on_writes_are_checked_and_bounded(world):
    client, _ = world
    for route, body, _ in WRITES:
        for dependencies in ([{'user_id': 'x', 'generation': 0}], [{'user_id': '20', 'generation': -1}],
                             [{'user_id': '20', 'generation': 0, 'text': MARKER}],
                             [{'user_id': str(i), 'generation': 0} for i in range(1, 202)]):
            assert client.post(route, headers=headers('fri'), json=body(dm(), dependencies)).status_code == 422


def test_a_writers_forget_me_takes_their_writes_dependencies_too(world):
    client, store = world
    _, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    store.personal_control('30', 'disable')
    with store.connection() as db:
        assert [db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                for table in ('fri_work_dependencies', 'fri_reminder_dependencies')] == [0, 0]


def test_a_save_records_the_generation_a_write_depends_on(world):
    """As a reply does (gene #114): a start that finds none knows an older controller erased it."""
    client, store = world
    written(client, '/workspace/save', work(dm(), depending(0)))
    with store.connection() as db:
        assert tuple(db.execute("SELECT generation FROM fri_member_summary_generations WHERE user_id='20'").fetchone()) == (0,)


def test_the_contract_offers_saved_dependencies_as_optional():
    offered = contract()
    assert 'saved-dependencies' in offered['capabilities']
    assert compatible({**offered, 'capabilities': [c for c in offered['capabilities'] if c != 'saved-dependencies']})


def test_a_dependent_history_turn_stays_until_its_summary_goes(world):
    """The guard: asking changes nothing about what's kept. B's dependent exchange is current until A's summary goes."""
    client, store = world
    taken, generation = summarized(client, store)
    repeated(client, generation)
    assert history(client, dependencies=True)['turns'][-1]['reply'] == ECHOED
    withdraw(client, taken)
    [turn] = history(client, dependencies=True)['turns']
    assert retired(turn['reply']) and 'summaries' not in turn


def test_an_older_controllers_privacy_change_retires_dependent_writes_at_once(world):
    """Delta review of the 2026-10-30 release: an older controller, which knows nothing of these links, moves A's
    privacy revision the way it always has. The database's triggers retire B's writes and drop the links naming A
    then and there, so nothing identifying A outlives the change during a rollback window."""
    client, store = world
    _, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), depending(generation)))
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    assert holds(store) and dependencies(store)
    with store.connection() as db:  # The older controller's own statement for a privacy change.
        db.execute("INSERT INTO chat_revisions VALUES ('20',1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1")
    assert not holds(store) and dependencies(store) == []
    assert len(rows(store, 'fri_work')) == 1 and len(rows(store, 'fri_reminders')) == 1


def test_an_older_controllers_privacy_change_takes_every_link_of_a_write_it_retires(world):
    """Review of gene #160: a draft depending on A and on B. The trigger for A's moved revision retires the draft and,
    as retire_writes does, drops all its links, so a replacement saved later with no dependencies isn't retired when
    B's revision moves."""
    client, store = world
    _, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), depending(generation) + depending(0, '40')))
    with store.connection() as db:  # The older controller's own statement for A's privacy change.
        db.execute("INSERT INTO chat_revisions VALUES ('20',1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1")
    assert not holds(store) and dependencies(store) == []
    work_id, draft = next(iter(rows(store, 'fri_work').items()))
    written(client, '/workspace/save', work(dm(3), body='a fresh plan', work_id=work_id, version=draft['version']))
    with store.connection() as db:  # Then B's.
        db.execute("INSERT INTO chat_revisions VALUES ('40',1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1")
    assert rows(store, 'fri_work')[work_id]['body'] == 'a fresh plan'
