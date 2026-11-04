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


def test_an_erasure_with_no_discord_event_keeps_gene_s_clock(gene):
    """Red's data deletion request carries no Discord message: its erasure stays on Gene's clock, as today, and the
    profile's Discord-clock cutoff is that same time, so it never admits more than before."""
    now = REAL()
    with gene_clock(-2):
        assert gene.post('/privacy/erase', headers=headers('fri'), json={'guild_id': '123', 'user_id': '20'}).status_code == 200
    control(gene, 'enable', now + 0.2, -2)
    answer = profile(gene)
    assert answer['erased_at_discord'] == answer['erased_at'] == pytest.approx(now - 2, abs=1)
    assert saved(gene, now - 0.5).get('kept') is not False  # As today: Gene's stamp is before it.
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
    assert profile(gene)['erased_at_discord'] == pytest.approx(now, abs=0.001)
    control(gene, 'enable', now + 0.2, 0)
    assert saved(gene, now - 10) == {'ok': True, 'first_memory': False, 'kept': False}
