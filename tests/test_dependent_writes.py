"""What saved work, one-off posts and reminders depending on another member's summary show or send once it goes.

gene #169: a post of a dependent draft that was sending when the summary went kept its words after delivery finished.
Delivery already holds what it sends, so the stored copy goes at once, whichever way the summary went; the post keeps
its state and lease, so its receipt still lands.

gene #158 (item 2): a draft or reminder saved while the summary was current kept its words after the summary expired,
until something pruned it. Every read that shows them, and every step that authorizes their delivery, checks the
summaries they depend on first, in its own transaction, as a turn's check does (gene #113).
"""

from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from tests.test_api import headers
from tests.test_saved_dependencies import SAID, THEIRS, dm, reminder, rows, work, written
from tests.test_summary_dependencies import depending
from tests.test_summary_generation_retention import lapsed, summarized
from tests.test_turn_withdrawal import MARKER, summary_generation_now, withdraw

OWNER = {**THEIRS, 'user_id': '9'}  # The verified owner, whose DM drafts may become one-off posts.
TITLE = 'raid notes on the '+MARKER


@pytest.fixture
def world(tmp_path):
    settings = Settings(tmp_path/'writes.sqlite3', '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


@pytest.fixture
def older(monkeypatch):
    """A controller before gene #137 and #154, the live release say: only the database's triggers act for it."""
    import gene.fri_member_summary as summaries

    @contextmanager
    def running():
        with monkeypatch.context() as patched:
            for name in ('retire_dependents', 'retire_writes'):
                patched.setattr(summaries, name, lambda *args, **kwargs: None)
            yield
    return running


def drafted(client, generation, age=1, scope=OWNER, said=None):
    """A DM draft depending on member 20's summary, whose title repeats it too."""
    said = said or dm(age, scope['user_id'])
    return written(client, '/workspace/save', {**work(said, depending(generation)), **scope, 'title': TITLE})


def posted(client, draft, age, state):
    """The owner's one-off post of a draft, taken as far as `state`: its ID and, once claimed, its lease."""
    post = written(client, '/workspace/posts/prepare', {**OWNER, 'source': dm(age, '9'), 'revision': 0,
                                                        'work_id': draft['id'], 'version': draft['version'],
                                                        'target_id': '777'})
    if state == 'pending':
        return post['id'], ''
    written(client, f"/workspace/posts/{post['id']}/bind", {'message_id': '888'})
    written(client, f"/workspace/posts/{post['id']}/decide", {
        'source': {**dm(age, '9'), 'kind': 'discord_interaction'}, 'preview_id': '888', 'approve': True})
    if state == 'authorized':
        return post['id'], ''
    claim = written(client, '/workspace/outbox/claim', {})
    assert claim['id'] == post['id'] and claim['body'] == draft['body']
    if state == 'sending':
        written(client, f"/workspace/posts/{post['id']}/sending", {'lease': claim['lease']})
    return post['id'], claim['lease']


def shown(client, post_id):
    answer = client.get(f'/workspace/posts/{post_id}', headers=headers('fri'))
    assert answer.status_code == 200, answer.text
    return answer.json()


def privacy_change(store, user='20'):
    """An older controller's own statement for a member's privacy change (gene #160's triggers act on it)."""
    with store.connection() as db:
        db.execute('INSERT INTO chat_revisions VALUES (?,1) ON CONFLICT(user_id) DO UPDATE SET revision=revision+1',
                   (user,))


# gene #169

@pytest.mark.parametrize('way', ['withdrawal', 'older withdrawal', 'older privacy change'])
def test_a_post_that_was_sending_keeps_no_words_once_its_drafts_summary_goes(world, older, way):
    """The issue's repro, on each path that retires a draft: this controller's withdrawal (retire_writes), an older
    controller's withdrawal (the summary trigger) and an older controller's privacy change (the revision trigger). The
    delivery still finishes, with its receipt, and nothing it stored holds the words. Posts not yet sending are
    cancelled with theirs, as before."""
    client, store = world
    taken, generation = summarized(client, store)
    draft = drafted(client, generation)
    sending, lease = posted(client, draft, 2, 'sending')
    others = [posted(client, draft, age, state)[0] for age, state in ((3, 'pending'), (4, 'authorized'))]
    if way == 'withdrawal':
        withdraw(client, taken)
    elif way == 'older withdrawal':
        with older():
            withdraw(client, taken)
    else:
        privacy_change(store)
    held = shown(client, sending)
    assert (held['state'], held['lease'], held['body']) == ('sending', lease, '')
    written(client, f'/workspace/posts/{sending}/finish', {'lease': lease, 'state': 'sent', 'message_id': '987'})
    assert {key: shown(client, sending)[key] for key in ('state', 'message_id', 'body')} == {
        'state': 'sent', 'message_id': '987', 'body': ''}
    assert [(shown(client, post)['state'], shown(client, post)['body']) for post in others] == [('cancelled', '')] * 2
    assert MARKER not in str(rows(store, 'fri_posts')) + str(rows(store, 'fri_work'))


def test_a_post_of_a_draft_on_a_current_summary_keeps_its_words(world):
    """The guard: nothing went, so the sending post's words stay until it's sent, as before."""
    client, store = world
    _, generation = summarized(client, store)
    sending, lease = posted(client, drafted(client, generation), 2, 'sending')
    assert SAID in shown(client, sending)['body']


# gene #158, item 2

def test_reading_a_draft_retires_it_first_once_its_summary_lapsed(world):
    """The issue's repro: B's draft was saved while A's summary was current; A's summary expired, and nothing pruned it.
    Reading the draft drops A's summary first, which retires the draft, so neither the read nor the list holds A's
    words."""
    client, store = world
    _, generation = summarized(client, store)
    draft = drafted(client, generation, scope=THEIRS)
    lapsed(store)
    read = client.post(f"/workspace/read/{draft['id']}", headers=headers('fri'), json=THEIRS)
    assert read.status_code == 200 and MARKER not in read.text and read.json()['version'] == 2
    assert summary_generation_now(store) != generation
    listed = client.post('/workspace/list', headers=headers('fri'), json=THEIRS)
    assert listed.status_code == 200 and MARKER not in listed.text


def test_listing_drafts_retires_one_whose_summary_lapsed_first(world):
    client, store = world
    _, generation = summarized(client, store)
    drafted(client, generation, scope=THEIRS)
    lapsed(store)
    listed = client.post('/workspace/list', headers=headers('fri'), json=THEIRS)
    assert listed.status_code == 200 and MARKER not in listed.text
    assert MARKER not in str(rows(store, 'fri_work')) and summary_generation_now(store) != generation


@pytest.mark.parametrize('route', ['list', 'read'])
def test_reading_reminders_retires_one_whose_summary_lapsed_first(world, route):
    client, store = world
    _, generation = summarized(client, store)
    item = written(client, '/social/reminders/prepare', reminder(dm(1), depending(generation)))
    lapsed(store)
    path = '/social/reminders/list' if route == 'list' else f"/social/reminders/{item['id']}/read"
    answer = client.post(path, headers=headers('fri'), json=THEIRS)
    assert answer.status_code == 200 and MARKER not in answer.text
    assert (rows(store, 'fri_reminders')[item['id']]['state'], summary_generation_now(store) != generation) == (
        'cancelled', True)


def test_open_loops_retire_a_draft_title_or_reminder_whose_summary_lapsed_first(world):
    """What Fri knows without a tool: the DM's reminders and its drafts' titles, in the turn's chat context."""
    client, store = world
    _, generation = summarized(client, store)
    drafted(client, generation, scope=THEIRS)
    written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
    lapsed(store)
    answer = client.post('/chat/context', headers=headers('fri'), json={
        'guild_id': '123', 'channel_id': '50', 'conversation_id': 'private:19', 'user_id': '30'})
    assert answer.status_code == 200
    loops = answer.json()['loops']
    assert MARKER not in str(loops) and summary_generation_now(store) != generation
    # The draft stays open under its breadcrumb; the reminder, cancelled, no longer shows.
    assert ([item['title'] for item in loops['saved_work']], loops['reminders']) == (['Withdrawn draft'], [])


def scheduled(client, store, generation, age=1):
    """B's reminder depending on A's summary, confirmed and now due."""
    item = written(client, '/social/reminders/prepare', reminder(dm(age), depending(generation)))
    with store.connection() as db:
        db.execute("UPDATE fri_reminders SET state='scheduled',due=0 WHERE id=?", (item['id'],))
    return item['id']


def test_a_due_reminder_whose_summary_lapsed_is_never_claimed(world):
    client, store = world
    _, generation = summarized(client, store)
    item = scheduled(client, store, generation)
    lapsed(store)
    claim = client.post('/social/reminders/claim', headers=headers('fri'), json={})
    assert claim.status_code == 200 and claim.json() is None
    assert (rows(store, 'fri_reminders')[item]['state'], rows(store, 'fri_reminders')[item]['body']) == ('cancelled', '')


def test_a_claimed_reminder_whose_summary_lapsed_is_never_sent(world):
    """Claimed while the summary was current, it lapsed before the send was authorized: refused, and the retirement
    stays though the refusal undoes the rest."""
    client, store = world
    _, generation = summarized(client, store)
    item = scheduled(client, store, generation)
    claim = written(client, '/social/reminders/claim', {})
    assert claim['id'] == item and SAID in claim['body']
    lapsed(store)
    refused = client.post(f'/social/reminders/{item}/sending', headers=headers('fri'), json={'lease': claim['lease']})
    assert refused.status_code >= 400 and MARKER not in refused.text
    assert (rows(store, 'fri_reminders')[item]['state'], rows(store, 'fri_reminders')[item]['body']) == ('cancelled', '')
    assert summary_generation_now(store) != generation


def test_confirming_a_reminder_whose_summary_lapsed_shows_none_of_it(world):
    client, store = world
    _, generation = summarized(client, store)
    item = written(client, '/social/reminders/prepare', reminder(dm(1), depending(generation)))
    written(client, f"/social/reminders/{item['id']}/bind", {**THEIRS, 'message_id': '888'})
    lapsed(store)
    answer = client.post(f"/social/reminders/{item['id']}/decide", headers=headers('fri'), json={
        **THEIRS, 'source': {**dm(1), 'kind': 'discord_interaction'}, 'revision': 0, 'decision': 'confirm',
        'preview_id': '888'})
    assert MARKER not in answer.text
    assert rows(store, 'fri_reminders')[item['id']]['state'] == 'cancelled'


@pytest.mark.parametrize('step', ['prepare', 'show', 'approve', 'claim', 'sending'])
def test_a_post_of_a_draft_whose_summary_lapsed_is_never_prepared_shown_or_sent(world, step):
    """Each step on the way to a one-off post checks the draft's summaries first. A refusal keeps the retirement."""
    client, store = world
    _, generation = summarized(client, store)
    draft = drafted(client, generation)
    state = {'prepare': None, 'show': 'pending', 'approve': 'pending', 'claim': 'authorized', 'sending': 'claimed'}
    post, lease = posted(client, draft, 2, state[step]) if state[step] else (None, '')
    if step == 'approve':
        written(client, f'/workspace/posts/{post}/bind', {'message_id': '888'})
    lapsed(store)
    if step == 'prepare':
        answer = client.post('/workspace/posts/prepare', headers=headers('fri'), json={
            **OWNER, 'source': dm(2, '9'), 'revision': 0, 'work_id': draft['id'], 'version': 1, 'target_id': '777'})
        assert answer.status_code >= 400 and rows(store, 'fri_posts') == {}
    elif step == 'show':
        assert shown(client, post)['state'] == 'cancelled'
    elif step == 'approve':
        answer = client.post(f'/workspace/posts/{post}/decide', headers=headers('fri'), json={
            'source': {**dm(2, '9'), 'kind': 'discord_interaction'}, 'preview_id': '888', 'approve': True})
        assert answer.json()['state'] == 'cancelled'
    elif step == 'claim':
        assert written(client, '/workspace/outbox/claim', {}) is None
    else:
        refused = client.post(f'/workspace/posts/{post}/sending', headers=headers('fri'), json={'lease': lease})
        assert refused.status_code >= 400 and MARKER not in refused.text
    assert MARKER not in str(rows(store, 'fri_posts')) + str(rows(store, 'fri_work'))
    assert summary_generation_now(store) != generation


def test_editing_or_replaying_a_draft_whose_summary_lapsed_keeps_none_of_it(world):
    """An edit of the old version is refused (its version moved as it was retired), and a retried save answers with the
    retired draft, not the words it first saved."""
    client, store = world
    _, generation = summarized(client, store)
    said = dm(1)
    draft = drafted(client, generation, scope=THEIRS, said=said)
    lapsed(store)
    edit = client.post('/workspace/save', headers=headers('fri'), json={
        **work(dm(2), body='keep the '+MARKER), 'work_id': draft['id'], 'version': 1})
    assert edit.status_code == 409 and MARKER not in str(rows(store, 'fri_work'))
    # Retried without its dependencies (an older Fri, or across an upgrade): the same digest, so it answers as saved.
    replay = client.post('/workspace/save', headers=headers('fri'), json={**work(said), 'title': TITLE})
    assert replay.status_code == 200 and MARKER not in replay.text and replay.json()['id'] == draft['id']


def test_reads_and_deliveries_on_a_current_summary_keep_their_words(world):
    """The guard: nothing lapsed, so every read and delivery step shows what was saved, and nothing moves."""
    client, store = world
    _, generation = summarized(client, store)
    draft = drafted(client, generation, scope=THEIRS)
    item = scheduled(client, store, generation, age=2)
    assert SAID in client.post(f"/workspace/read/{draft['id']}", headers=headers('fri'), json=THEIRS).text
    assert MARKER in client.post('/workspace/list', headers=headers('fri'), json=THEIRS).text
    assert SAID in client.post('/social/reminders/list', headers=headers('fri'), json=THEIRS).text
    claim = written(client, '/social/reminders/claim', {})
    assert claim['id'] == item and SAID in claim['body']
    assert SAID in written(client, f'/social/reminders/{item}/sending', {'lease': claim['lease']})['body']
    assert summary_generation_now(store) == generation


def test_reads_leave_another_members_lapsed_summary_they_dont_depend_on(world):
    """Only the summaries the shown items depend on are checked: B's own draft on nothing leaves A's lapsed summary for
    its prune."""
    client, store = world
    _, generation = summarized(client, store)
    written(client, '/workspace/save', work(dm(1), body='my own list'))
    lapsed(store)
    assert 'raid notes' in client.post('/workspace/list', headers=headers('fri'), json=THEIRS).text
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM fri_member_summaries').fetchone()[0] == 1
    assert summary_generation_now(store) == generation


def test_a_start_keeps_a_sent_posts_words_when_its_draft_is_edited_to_the_breadcrumbs(world):
    """Review of #171 (round 1): a member may type the breadcrumb's own words into a draft. That's no retirement, and
    a start keeps the words of a post sent from it before."""
    from gene.fri_member_summary import WORK_RETIRED, WORK_TITLE
    from gene.store import Store
    client, store = world
    plain = written(client, '/workspace/save', {**work(dm(1, '9'), body='ordinary authored post'), 'user_id': '9'})
    sent, lease = posted(client, plain, 2, 'claimed')
    written(client, f'/workspace/posts/{sent}/sending', {'lease': lease})
    written(client, f'/workspace/posts/{sent}/finish', {'lease': lease, 'state': 'sent', 'message_id': '986'})
    written(client, '/workspace/save', {**work(dm(3, '9'), title=WORK_TITLE, body=WORK_RETIRED), 'user_id': '9',
                                        'work_id': plain['id'], 'version': 1})
    Store(store.path)
    assert shown(client, sent)['body'] == 'ordinary authored post'
