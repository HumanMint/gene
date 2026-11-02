"""gene #176: what an older controller in a rollback window shows and sends of work whose summary expired unpruned.

A controller before gene #154 (the release before the live one, `34dbbb3`, say) knows nothing of the summaries saved
drafts and reminders depend on. Once one expires with nothing pruning it, it still showed their words, and authorized
the post of such a draft, or the send of such a reminder. This controller checks first (gene #158); the database acts
for one that doesn't. Each dependent's summary is mirrored as the time it was refreshed, in a table nothing drops, so
a delivery about to be claimed or sent while that's past the summary's life is refused, and the next of the frequent
writes every controller makes (gene #177) retires what depends on it, as a start here would.
"""

import sqlite3
import time
from contextlib import closing, contextmanager

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from tests.test_api import headers
from tests.test_dependent_writes import drafted, posted, scheduled
from tests.test_message_fences import OLDER
from tests.test_saved_dependencies import SAID, dm, reminder, rows, written
from tests.test_summary_dependencies import depending
from tests.test_summary_generation_retention import lapsed, summarized
from tests.test_turn_withdrawal import MARKER, withdraw


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'expired.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


@pytest.fixture
def unaware(monkeypatch):
    """A controller before gene #154: it never looks at what a draft or reminder depends on before showing or sending
    it, so only the database's triggers act for it. The real older code is run in the PR's probe."""
    @contextmanager
    def running():
        with monkeypatch.context() as patched:
            for module in ('gene.fri_workspace_store', 'gene.fri_social_store'):
                patched.setattr(f'{module}.current_writes', lambda *args, **kwargs: False)
            yield
    return running


def older(store, write='heartbeat'):
    """One of the frequent writes an older controller makes, as its own statement (see gene #177)."""
    with closing(sqlite3.connect(store.path)) as raw, raw:
        statement, args = OLDER[write]
        raw.execute(statement, args())


def refused(client, path, body):
    """Whether a delivery step was refused: a 4xx, or the database refusing it under the controller."""
    try:
        return client.post(path, headers=headers('fri'), json=body).status_code >= 400
    except sqlite3.IntegrityError:
        return True


def mirrored(store):
    with store.connection() as db:
        return sorted(row[0] for row in db.execute(
            "SELECT key FROM agent_state WHERE substr(key,1,18)='summary_refreshed:'"))


@pytest.mark.parametrize('step', ['claim', 'sending'])
def test_an_older_controller_never_sends_a_post_whose_drafts_summary_expired(world, unaware, step):
    """The issue's repro: a post of a draft depending on member 20's summary, authorized (or claimed) while the summary
    was current. It expires, with nothing pruning it, and a controller that doesn't check claims or sends it: refused.
    Its next frequent write cancels the post and retires the draft."""
    client, store = world
    _, generation = summarized(client, store)
    post, lease = posted(client, drafted(client, generation), 2, 'authorized' if step == 'claim' else 'claimed')
    lapsed(store)
    with unaware():
        if step == 'claim':
            assert refused(client, '/workspace/outbox/claim', {})
        else:
            assert refused(client, f'/workspace/posts/{post}/sending', {'lease': lease})
        assert rows(store, 'fri_posts')[post]['state'] != 'sending'
        older(store)
    assert (rows(store, 'fri_posts')[post]['state'], rows(store, 'fri_posts')[post]['body']) == ('cancelled', '')
    assert MARKER not in str(rows(store, 'fri_work')) and MARKER not in str(rows(store, 'fri_posts'))


@pytest.mark.parametrize('step', ['claim', 'sending'])
def test_an_older_controller_never_sends_a_reminder_whose_summary_expired(world, unaware, step):
    client, store = world
    _, generation = summarized(client, store)
    item = scheduled(client, store, generation)
    lease = written(client, '/social/reminders/claim', {})['lease'] if step == 'sending' else None
    lapsed(store)
    with unaware():
        if step == 'claim':
            assert refused(client, '/social/reminders/claim', {})
        else:
            assert refused(client, f'/social/reminders/{item}/sending', {'lease': lease})
        assert rows(store, 'fri_reminders')[item]['state'] != 'sending'
        older(store)
    assert (rows(store, 'fri_reminders')[item]['state'], rows(store, 'fri_reminders')[item]['body']) == ('cancelled', '')


@pytest.mark.parametrize('write', ['heartbeat', 'agent state', 'session', 'usage'])
def test_an_older_controllers_next_write_retires_what_depends_on_an_expired_summary(world, unaware, write):
    """Reads: what an older controller shows of a draft or reminder is what the database holds, so the next frequent
    write retires their words once the summary expired, as a start here would. Before that write, it still shows them."""
    client, store = world
    _, generation = summarized(client, store)
    draft = drafted(client, generation)
    item = written(client, '/social/reminders/prepare', reminder(dm(3), depending(generation)))
    lapsed(store)
    with unaware():
        older(store, write)
    works, reminders = rows(store, 'fri_work'), rows(store, 'fri_reminders')
    assert works[draft['id']]['body'] != SAID and MARKER not in works[draft['id']]['title']
    assert (reminders[item['id']]['state'], reminders[item['id']]['body']) == ('cancelled', '')
    assert mirrored(store) == []  # Nothing depends on the summary any more.


def test_a_current_summary_leaves_an_older_controllers_delivery_as_before(world, unaware):
    """Guard: refreshed within its life, the post is claimed and sent, and its words stay."""
    client, store = world
    _, generation = summarized(client, store)
    draft = drafted(client, generation)
    post, lease = posted(client, draft, 2, 'claimed')
    with unaware():
        older(store)
        written(client, f'/workspace/posts/{post}/sending', {'lease': lease})
    assert rows(store, 'fri_posts')[post]['state'] == 'sending' and SAID in rows(store, 'fri_posts')[post]['body']
    assert rows(store, 'fri_work')[draft['id']]['body'] == SAID


def test_what_is_mirrored_is_only_who_something_depends_on_and_goes_with_it(world):
    """The mirror holds a member and when their summary was refreshed, only while a draft or reminder depends on it: it
    goes when they do, and when the summary goes."""
    client, store = world
    assert mirrored(store) == []
    taken, generation = summarized(client, store)
    assert mirrored(store) == []  # A summary nothing depends on.
    drafted(client, generation)
    assert mirrored(store) == ['summary_refreshed:123:20']
    withdraw(client, taken)  # The summary goes, and what depended on it.
    older(store)
    assert mirrored(store) == []


def test_a_purge_before_a_rollback_leaves_an_older_controller_able_to_write(world):
    """A rollback to a release without member summaries purges their tables. Nothing the database runs names them
    from elsewhere: the posts, reminders and frequent writes an older controller makes still go through, and nothing
    mirrored outlives the purge."""
    from gene.fri_member_summary import purge
    client, store = world
    _, generation = summarized(client, store)
    post, _ = posted(client, drafted(client, generation), 2, 'claimed')
    item = scheduled(client, store, generation, age=3)
    purge(store.path)
    with closing(sqlite3.connect(store.path)) as raw, raw:
        raw.execute("UPDATE fri_posts SET state='sending' WHERE id=?", (post,))
        raw.execute("UPDATE fri_reminders SET state='claimed' WHERE id=?", (item,))
        raw.execute("UPDATE fri_reminders SET state='sending' WHERE id=?", (item,))
    for write in ('heartbeat', 'agent state', 'session', 'usage'):
        older(store, write)
    assert mirrored(store) == []


def test_a_summary_refreshed_again_moves_what_is_mirrored(world, unaware):
    """The mirror follows the summary's refresh time: refreshed again after it lapsed (a rebuild sent it, say), the post
    depending on it is sent, not refused for the time it had before."""
    client, store = world
    _, generation = summarized(client, store)
    post, lease = posted(client, drafted(client, generation), 2, 'claimed')
    lapsed(store)
    with store.connection() as db:
        db.execute('UPDATE fri_member_summaries SET refreshed=?', (time.time(),))
    with unaware():
        written(client, f'/workspace/posts/{post}/sending', {'lease': lease})
    assert rows(store, 'fri_posts')[post]['state'] == 'sending'
