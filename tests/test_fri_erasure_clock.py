"""The erasure cutoff on Discord's clock (HumanMint/fri#337 items 4 and 5). Gene stamped a member's erasure with its own
clock and compared it with a message's Discord time, its snowflake. With Gene's clock behind Discord's, a message sent
just before the erasure counted as after it and was kept (item 4); with Gene's ahead, one sent just after counted as
before it and was dropped (item 5). The erasure is dated by the Discord message or interaction that asked for it, when
there is one, so both sides of the comparison are on Discord's clock."""

import datetime as dt
import time
from contextlib import contextmanager

import discord
import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from tests.test_api import headers

REAL = time.time


@pytest.fixture
def gene(tmp_path):
    settings = Settings(tmp_path/'clock.db', '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32,
                        workshop_channel_id='456', fri_token='fri-'+'x'*32, agent_channels='1,456',
                        agent_history_channels='1,3')  # 3: history, not chat.
    return TestClient(create_app(settings))


def source(at, user='20', channel='456'):
    """A message of the member's in the workshop, sent at `at` on Discord's clock."""
    return {'kind': 'discord_message', 'id': str(discord.utils.time_snowflake(dt.datetime.fromtimestamp(at, dt.timezone.utc))),
            'user_id': user, 'display_name': 'friend', 'channel_id': channel, 'guild_id': '123'}


@contextmanager
def gene_clock(offset):
    """Gene's clock `offset` seconds off Discord's, which the snowflakes here keep."""
    real = REAL
    time.time = lambda: real() + offset
    try:
        yield
    finally:
        time.time = real


def control(gene, action, at, offset, user='20', **changes):
    body = {'guild_id': '123', 'user_id': user, 'channel_id': '456', 'conversation_id': 'public:456',
            'action': action, 'source': source(at, user), **changes}
    with gene_clock(offset):
        assert gene.post('/chat/memory/control', headers=headers('fri'), json=body).status_code == 200


def saved(gene, at, text='i love trance', revision=2):
    store = gene.app.state.store
    return store.save_chat('123', 'public:456', '20', revision, text, 'noted', 'model', 'high', source=source(at),
                           notes=[{'category': 'music', 'evidence': text}])


def profile(gene, user='20', channel='456'):
    response = gene.post('/agent/context', headers=headers('fri'),
                         json={'guild_id': '123', 'user_id': user, 'channel_id': channel})
    assert response.status_code == 200, response.text
    return response.json()


def test_gene_behind_discord_a_message_sent_just_before_the_erasure_keeps_nothing(gene):
    """Item 4: Gene's clock two seconds behind Discord's. The member's message went out half a second before their
    "forget me"; they turned memory on again, and Fri answered it. Gene's stamp, two seconds early, put the message
    after the erasure, so the exchange and a note were kept. On Discord's clock it's before: nothing is kept."""
    now = REAL()
    control(gene, 'disable', now, -2)
    control(gene, 'enable', now + 0.2, -2)
    assert saved(gene, now - 0.5) == {'ok': True, 'first_memory': False, 'kept': False}


def test_gene_ahead_of_discord_a_message_sent_just_after_the_reenable_is_kept(gene):
    """Item 5: Gene's clock two seconds ahead. A fresh message right after the member turned memory on again counted
    as before the erasure, and kept nothing. On Discord's clock it's after: it's kept as any is."""
    now = REAL()
    control(gene, 'disable', now - 1, 2)
    control(gene, 'enable', now - 0.8, 2)
    assert saved(gene, now - 0.5).get('kept') is not False


def test_the_profile_says_the_cutoffs_on_discords_clock_beside_gene_s_own(gene):
    """Fri holds a member's words to the same cutoff (fri #331, #337), so the profile says it: erased_at and
    removed_at as before, on Gene's clock, which an older Fri reads, and the same on Discord's clock beside them.
    A history channel's narrower answer, which older Fris check field for field, is as before."""
    now = REAL()
    before = profile(gene)
    assert before['erased_at_discord'] == before['removed_at_discord'] == 0
    control(gene, 'disable', now, -2)
    control(gene, 'enable', now + 0.2, -2)
    answer = profile(gene)
    asked = discord.utils.snowflake_time(int(source(now)['id'])).timestamp()
    assert answer['erased_at_discord'] == answer['removed_at_discord'] == pytest.approx(asked, abs=0.002)
    assert answer['erased_at'] == pytest.approx(now - 2, abs=1)
    assert set(profile(gene, channel='3')) == {'enabled', 'revision', 'removed_at'}


def test_an_erasure_with_no_discord_event_holds_a_few_seconds_past_gene_s_stamp(gene):
    """Red's data deletion request carries no Discord message: its erasure keeps Gene's stamp (erased_at), and its
    cutoff on Discord's clock is a few seconds (MARGIN) later (HumanMint/fri#368). With Gene's clock two seconds
    behind, the member's message sent half a second before it counted as after it, and was kept; it's held back."""
    from gene.fri_memory_store import MARGIN
    now = REAL()
    with gene_clock(-2):
        assert gene.post('/privacy/erase', headers=headers('fri'), json={'guild_id': '123', 'user_id': '20'}).status_code == 200
    control(gene, 'enable', now + 0.2, -2)
    answer = profile(gene)
    assert answer['erased_at'] == pytest.approx(now - 2, abs=1)
    assert answer['erased_at_discord'] == pytest.approx(answer['erased_at'] + MARGIN, abs=0.001)
    assert saved(gene, now - 0.5) == {'ok': True, 'first_memory': False, 'kept': False}
    assert saved(gene, now - 3, text='i love jazz') == {'ok': True, 'first_memory': False, 'kept': False}


def test_a_discord_time_from_an_earlier_erasure_is_never_used_for_a_later_one(gene):
    """A Discord time belongs to the erasure it was recorded with. A later one without its own (an older controller's,
    or Red's deletion) is on Gene's clock again, never on the earlier one's Discord time, which would admit what was
    said between the two."""
    now = REAL()
    control(gene, 'disable', now - 30, 0)
    with gene_clock(0):
        assert gene.post('/privacy/erase', headers=headers('fri'), json={'guild_id': '123', 'user_id': '20'}).status_code == 200
    control(gene, 'enable', now + 0.2, 0)
    assert profile(gene)['erased_at_discord'] == pytest.approx(now, abs=1)
    assert saved(gene, now - 10, revision=3) == {'ok': True, 'first_memory': False, 'kept': False}


def test_a_discord_time_far_from_gene_s_is_held_to_a_minute_before_it(gene):
    """A Discord time never moves the cutoff more than a minute before Gene's own stamp: a stale one (a control that
    waited out an outage) holds back what was said after it, never admits it."""
    now = REAL()
    control(gene, 'disable', now - 600, 0)
    control(gene, 'enable', now + 0.2, 0)
    assert profile(gene)['erased_at_discord'] == pytest.approx(now - 60, abs=1)
    assert saved(gene, now - 90) == {'ok': True, 'first_memory': False, 'kept': False}


def test_a_source_that_is_not_the_members_still_erases_on_gene_s_clock(gene):
    """A control whose source isn't the member's own message is still carried out: an erasure is never refused.
    Its Discord time isn't trusted, so the cutoff is Gene's own stamp."""
    now = REAL()
    control(gene, 'disable', now, -2, source=source(now + 30, user='21'))
    assert not profile(gene)['enabled']
    assert profile(gene)['erased_at_discord'] == pytest.approx(now - 2, abs=1)


def test_gene_forgetme_is_dated_by_its_command_message(gene):
    """Red's "gene forgetme true" is a Discord message too: its ID dates the erasure on Discord's clock (item 4)."""
    now = REAL()
    with gene_clock(-2):
        assert gene.post('/privacy/erase', headers=headers('runtime'),
                         json={'guild_id': '123', 'user_id': '20', 'source_id': source(now)['id']}).status_code == 200
    control(gene, 'enable', now + 0.2, -2)
    assert profile(gene)['erased_at_discord'] == pytest.approx(now, abs=0.01)
    assert saved(gene, now - 0.5) == {'ok': True, 'first_memory': False, 'kept': False}


def test_an_older_controllers_erasure_after_one_with_a_discord_time_is_on_gene_s_clock(gene):
    """Rolled back, an older controller stamps a later erasure in its own table and never touches the Discord times.
    Forward again, that earlier Discord time isn't this erasure's: the cutoff is Gene's stamp, as the older one's."""
    now = REAL()
    control(gene, 'disable', now - 30, 0)
    store = gene.app.state.store
    with store.connection() as db:  # As an older controller's disable writes it.
        db.execute('INSERT INTO personal_erasures VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET at=excluded.at',
                   ('20', now))
    from gene.fri_memory_store import MARGIN
    # Its own stamp, with no Discord time: a few seconds on (HumanMint/fri#368).
    assert profile(gene)['erased_at_discord'] == pytest.approx(now + MARGIN, abs=0.001)
    control(gene, 'enable', now + 0.2, 0)
    assert saved(gene, now - 10) == {'ok': True, 'first_memory': False, 'kept': False}


@pytest.mark.parametrize('offset', [-2, 0, 2])
@pytest.mark.parametrize('delayed', [30, 600])
def test_a_delayed_erasure_never_moves_the_cutoff_earlier(gene, offset, delayed):
    """Sol round 1 of #213, B1: an erasure the member asked for at T-10 is carried out; then one they'd asked for
    earlier, dated T-30 (or T-600, after an outage), arrives late. The cutoff moved back to that earlier time (for
    T-600, the minute before Gene's stamp), so their message from T-10.5, before the first erasure, was kept again. A
    cutoff never moves earlier: it's the latest of every erasure's, whatever order they arrive in."""
    now = REAL()
    control(gene, 'disable', now - 10, offset)
    control(gene, 'disable', now - delayed, offset)
    control(gene, 'enable', now + 0.2, offset)
    asked = discord.utils.snowflake_time(int(source(now - 10)['id'])).timestamp()
    assert profile(gene)['erased_at_discord'] == pytest.approx(asked, abs=0.002)
    assert saved(gene, now - 10.5, revision=3) == {'ok': True, 'first_memory': False, 'kept': False}


def test_an_erasure_with_no_discord_time_never_moves_the_cutoff_earlier_either(gene):
    """With Gene's clock behind, an erasure dated by the member's message has a cutoff after Gene's own stamp. One
    after it without a Discord time (Red's data deletion) keeps that later cutoff, never Gene's earlier stamp."""
    now = REAL()
    control(gene, 'disable', now, -5)
    with gene_clock(-5):
        assert gene.post('/privacy/erase', headers=headers('fri'), json={'guild_id': '123', 'user_id': '20'}).status_code == 200
    control(gene, 'enable', now + 0.2, -5)
    asked = discord.utils.snowflake_time(int(source(now)['id'])).timestamp()
    assert profile(gene)['erased_at_discord'] == pytest.approx(asked, abs=0.002)
    assert saved(gene, now - 0.5, revision=3) == {'ok': True, 'first_memory': False, 'kept': False}


@pytest.mark.parametrize('route', ['control', 'erase'])
def test_a_message_gene_knows_is_someone_elses_never_dates_an_erasure(gene, route):
    """Sol round 1 of #213, S1: a control naming a message Gene has saved as another member's is carried out, on Gene's
    own stamp, never that message's time."""
    now = REAL()
    theirs = source(now - 30, user='30')
    gene.app.state.store.save_chat('123', 'public:456', '30', 0, 'hey fri', 'hey', 'model', 'high', source=theirs)
    if route == 'control':
        control(gene, 'disable', now, 0, source={**theirs, 'user_id': '20'})
    else:
        assert gene.post('/privacy/erase', headers=headers('runtime'),
                         json={'guild_id': '123', 'user_id': '20', 'source_id': theirs['id']}).status_code == 200
    assert not profile(gene)['enabled']
    assert profile(gene)['erased_at_discord'] == pytest.approx(now, abs=1)


def test_an_older_controllers_later_erasure_never_moves_the_cutoff_earlier(gene):
    """With Gene's clock behind, an erasure's cutoff is after Gene's stamp. Rolled back, an older controller stamps a
    later erasure, still before that cutoff: forward again, the cutoff stays."""
    now = REAL()
    control(gene, 'disable', now, -5)
    store = gene.app.state.store
    with store.connection() as db:  # As an older controller's disable writes it, a second after the first, on Gene's clock.
        db.execute('INSERT INTO personal_erasures VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET at=excluded.at',
                   ('20', now - 4))
    asked = discord.utils.snowflake_time(int(source(now)['id'])).timestamp()
    assert profile(gene)['erased_at_discord'] == pytest.approx(asked, abs=0.002)


def remove_a_note(gene, offset, at=None, user='20'):
    """The member removes a saved note through My memory at `at` on Discord's clock (now if None), with Gene's clock
    `offset` seconds off Discord's while it carries it out then."""
    from gene.fri_memory_store import edit, entries, save
    store, at = gene.app.state.store, REAL() if at is None else at
    with store.connection() as db:
        save(db, '123', 'server', user, ('games', 'I like Minecraft'), REAL(), provenance=[])
    found = entries(store, '123', 'private:900', user)['entries']
    if not found:  # Saved before a removal fence an earlier removal left, with Gene's clock ahead: none to remove.
        return
    note = found[0]
    revision = store.chat_context('123', 'private:900', user)['revision']
    asked = {'kind': 'discord_interaction', 'id': source(at, user)['id'], 'user_id': user,
             'display_name': 'friend', 'channel_id': '900', 'guild_id': None}
    with gene_clock(offset + at - REAL()):
        edit(store, '123', 'private:900', user, note['id'], revision, asked, 'remove')


@pytest.mark.parametrize('offset', [-2, 0, 2])
@pytest.mark.parametrize('route', ['control', 'erase'])
@pytest.mark.parametrize('delayed', [30, 600])
def test_a_delayed_erasure_never_drops_a_newer_note_removals_cutoff(gene, offset, route, delayed):
    """Sol round 2 of #213, B2: the member removed a note at T-10, so nothing they said before then is kept again.
    At T, an older erasure of theirs, asked for at T-30 (or T-600), was carried out: it deleted the removal's record
    and kept only its own earlier cutoff, so once memory was on again their message from T-20 was kept, with its
    preference. An erasure keeps every cutoff in force: the latest of its own, an earlier erasure's and the member's
    removals."""
    now = REAL()
    remove_a_note(gene, offset, at=now - 10)
    removal = now - 10 + offset
    if route == 'control':
        control(gene, 'disable', now - delayed, offset)
    else:
        with gene_clock(offset):
            assert gene.post('/privacy/erase', headers=headers('runtime'),
                             json={'guild_id': '123', 'user_id': '20',
                                   'source_id': source(now - delayed)['id']}).status_code == 200
    control(gene, 'enable', now + 0.2, offset)
    answer = profile(gene)
    assert answer['erased_at_discord'] >= removal - 0.01 and answer['removed_at_discord'] >= removal - 0.01
    assert saved(gene, now - 20, revision=answer['revision']) == {'ok': True, 'first_memory': False, 'kept': False}


def cutoffs(gene, user='20'):
    from gene.fri_memory_store import learned_cutoff
    answer = profile(gene, user)
    with gene.app.state.store.connection() as db:
        learned = learned_cutoff(db, '123', 'public:456', user) or 0
    return {key: answer[key] for key in ('erased_at', 'removed_at', 'erased_at_discord', 'removed_at_discord')} | {
        'learned': learned}


@pytest.mark.parametrize('seed', range(12))
def test_no_sequence_of_erasures_removals_and_reenables_lowers_a_cutoff(tmp_path, seed):
    """Sol round 2 of #213: whatever order erasures (by either route, dated by a Discord time or not, delayed or
    not), note removals and re-enables come in, with Gene's clock anywhere within five seconds of Discord's, no
    cutoff a reader uses ever goes back: erased_at and removed_at, both on Discord's clock, and learning's."""
    import random
    rng = random.Random(seed)
    settings = Settings(tmp_path/'invariant.db', '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32,
                        workshop_channel_id='456', fri_token='fri-'+'x'*32, agent_channels='1,456',
                        agent_history_channels='1')
    gene = TestClient(create_app(settings))
    last = cutoffs(gene)
    for _ in range(10):
        offset, op = rng.uniform(-5, 5), rng.choice(['control', 'erase', 'erase plain', 'remove', 'enable'])
        now = REAL()
        if op == 'control':
            control(gene, 'disable', now - rng.choice([0, 1, 10, 30, 90, 600]), offset)
        elif op.startswith('erase'):
            body = {'guild_id': '123', 'user_id': '20'}
            if op == 'erase':
                body['source_id'] = source(now - rng.choice([0, 10, 600]))['id']
            with gene_clock(offset):
                assert gene.post('/privacy/erase', headers=headers('runtime'), json=body).status_code == 200
        elif op == 'remove':
            if profile(gene)['enabled']:
                remove_a_note(gene, offset)
        else:
            control(gene, 'enable', now, offset)
        current = cutoffs(gene)
        assert all(current[key] >= last[key] for key in current), (op, offset, last, current)
        last = current


def test_a_burst_message_gene_knows_is_someone_elses_never_dates_an_erasure(gene):
    """Sol round 2 of #213, S1: a burst's earlier message, saved with another member's turn, is theirs too."""
    now = REAL()
    first, last = source(now - 30, user='30'), source(now - 29, user='30')
    gene.app.state.store.save_chat('123', 'public:456', '30', 0, 'hey fri', 'hey', 'model', 'high', source=last,
                                   burst=[{'source': first, 'text': 'so'}, {'source': last, 'text': 'hey fri'}])
    control(gene, 'disable', now, 0, source={**first, 'user_id': '20'})
    assert not profile(gene)['enabled']
    assert profile(gene)['erased_at_discord'] == pytest.approx(now, abs=1)


def test_a_later_removal_with_gene_s_clock_stepped_back_never_moves_a_cutoff_earlier(gene):
    """A note removal's fence keeps its later time: one after it, with Gene's clock stepped back three seconds, would
    otherwise move removed_at, on both clocks, and learning's cutoff back."""
    remove_a_note(gene, -1)
    before = cutoffs(gene)
    time.sleep(0.01)
    remove_a_note(gene, -4)
    after = cutoffs(gene)
    assert before['removed_at'] > 0 and all(after[key] >= before[key] for key in after), (before, after)


DM = {'guild_id': '123', 'user_id': '20', 'channel_id': '456', 'conversation_id': 'private:900', 'profiles': True}


def my_memory(gene):
    """My memory, as the member opens it again in their DMs: what's kept, and where."""
    response = gene.post('/chat/memory/notes', headers=headers('fri'), json=DM)
    assert response.status_code == 200, response.text
    return response.json()


def pressed(gene, evidence, action, offset, replacement=''):
    """The member's press on one of their notes in My memory, with Gene's clock `offset` seconds off Discord's."""
    listed = my_memory(gene)
    [note] = [entry for entry in listed['entries'] if entry['evidence'] == evidence]
    asked = {'kind': 'discord_interaction', 'id': source(REAL())['id'], 'user_id': '20', 'display_name': 'friend',
             'channel_id': '900', 'guild_id': None}
    with gene_clock(offset):
        return gene.post(f"/chat/memory/notes/{note['id']}", headers=headers('fri'), json={
            **DM, 'revision': listed['revision'], 'source': asked, 'action': action, 'evidence': replacement})


def profile_notes(gene, *notes, at=None):
    from gene.fri_memory_store import save
    with gene.app.state.store.connection() as db:
        for note in notes:
            assert save(db, '123', 'server', '20', note, REAL() if at is None else at, provenance=[])


def test_a_replacement_after_a_removal_with_gene_s_clock_ahead_is_kept(gene):
    """Sol round 3 of #213, B3: the member removes one profile note with Gene's clock two seconds ahead, then replaces
    another after it steps back three. The first removal's fence, kept at its later time, refused the replacement's
    save, unnoticed after the note it replaces had gone: 200, and neither note, even opened again. Their own words now
    are kept: a removal fence holds back what was learned from before it, never what they type in My memory."""
    profile_notes(gene, ('games', 'I like chess'), ('music', 'I love trance'))
    assert pressed(gene, 'I like chess', 'remove', 2).status_code == 200
    replaced = pressed(gene, 'I love trance', 'replace', -3, 'I love techno')
    assert replaced.status_code == 200, replaced.text
    for listed in (replaced.json(), my_memory(gene)):  # As answered, and opened again.
        assert [(entry['scope'], entry['evidence']) for entry in listed['entries']] == [('server', 'I love techno')]


def test_a_note_kept_here_only_after_a_removal_with_gene_s_clock_ahead_moves(gene):
    """The same for "Keep here only": the profile note moves into this private chat. Its save there was refused by the
    other removal's fence, so the move failed ("can't be moved now") where the bases moved it."""
    profile_notes(gene, ('games', 'I like chess'), ('music', 'I love trance'))
    assert pressed(gene, 'I like chess', 'remove', 2).status_code == 200
    kept = pressed(gene, 'I love trance', 'keep', -3)
    assert kept.status_code == 200, kept.text
    for listed in (kept.json(), my_memory(gene)):
        assert [(entry['scope'], entry['evidence']) for entry in listed['entries']] == [('private:900', 'I love trance')]


def test_a_replacement_save_that_fails_restores_the_note_it_was_replacing(gene):
    """Any other refusal of the replacement's save (here: the same words already kept, dated later, from Gene's clock
    running ahead) is an error, and the note being replaced stays: never a 200 with it gone and nothing in its place
    that the member asked for."""
    profile_notes(gene, ('games', 'I like chess'), at=REAL()+5)
    profile_notes(gene, ('games', 'I like checkers'))
    replaced = pressed(gene, 'I like checkers', 'replace', 0, 'I like chess')
    assert replaced.status_code == 400, replaced.text
    assert sorted(entry['evidence'] for entry in my_memory(gene)['entries']) == ['I like checkers', 'I like chess']
