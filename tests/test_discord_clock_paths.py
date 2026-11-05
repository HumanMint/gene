"""HumanMint/fri#368: the cutoffs still stamped on Gene's clock, as HumanMint/gene#213 left them. A note removal, dated
by Gene's clock, was compared with messages' snowflakes, on Discord's: with Gene's clock behind, a message sent just
before the removal counted as after it and was kept or learned; ahead, one sent just after it was refused. A removal is
dated by the panel interaction that asked, within a minute of Gene's stamp, as an erasure is, and never moves a cutoff
earlier. A cutoff known only on Gene's clock (Red's own data deletion, an older Fri's control, a removal no interaction
dates) holds a few seconds past it (MARGIN). And a history channel's narrower answer says the removal cutoff on
Discord's clock to a Fri that asks for it."""

import pytest

import gene.fri_memory_store as memory
from tests.test_api import headers
from tests.test_fri_erasure_clock import (  # noqa: F401
    REAL,
    control,
    gene,
    gene_clock,
    profile,
    remove_a_note,
    source,
)


def kept(gene, at, user='20', conversation='public:456', text='i love trance'):  # noqa: F811
    """Whether member 20's message sent at `at` on Discord's clock, answered now, keeps its exchange and note."""
    store = gene.app.state.store
    revision = store.chat_context('123', conversation, user)['revision']
    saved = store.save_chat('123', conversation, user, revision, text, 'noted', 'model', 'high',
                            source=source(at, user=user), notes=[{'category': 'music', 'evidence': text}])
    return saved.get('kept') is not False


def history(gene, flag=None):  # noqa: F811
    body = {'guild_id': '123', 'user_id': '20', 'channel_id': '3', **({'discord_cutoffs': flag} if flag is not None else {})}
    response = gene.post('/agent/context', headers=headers('fri'), json=body)
    assert response.status_code == 200, response.text
    return response.json()


def test_gene_behind_a_message_sent_just_before_a_removal_keeps_nothing(gene):  # noqa: F811
    """Gene's clock two seconds behind Discord's: the member removes a profile note at T (the panel press), and Gene
    stamped the removal T-2. Their message from T-0.5, answered after it, counted as after the removal and kept its
    exchange and note. On Discord's clock it's before: it keeps nothing, and the profile says T on Discord's clock."""
    removal = REAL()
    remove_a_note(gene, -2, removal)
    answer = profile(gene)
    assert answer['removed_at'] == pytest.approx(removal - 2, abs=0.5)
    assert answer['removed_at_discord'] == pytest.approx(removal, abs=0.01)
    assert not kept(gene, removal - 0.5)


def test_gene_ahead_a_message_sent_just_after_a_removal_is_kept(gene):  # noqa: F811
    """Gene's clock two seconds ahead: the removal at T was stamped T+2, and the member's message from T+0.5 counted as
    before it and kept nothing. On Discord's clock it's after: it's kept."""
    removal = REAL()
    remove_a_note(gene, 2, removal)
    assert profile(gene)['removed_at_discord'] == pytest.approx(removal, abs=0.01)
    assert kept(gene, removal + 0.5)


def test_learning_holds_a_member_to_a_removal_on_discords_clock(gene):  # noqa: F811
    """What learning from history holds a member's words to is the removal on Discord's clock too."""
    from gene.fri_memory_store import learned_cutoff
    removal = REAL()
    remove_a_note(gene, -2, removal)
    with gene.app.state.store.connection() as db:
        assert learned_cutoff(db, '123', 'public:1', '20') == pytest.approx(removal, abs=0.01)


@pytest.mark.parametrize('delayed', [1, 200])
def test_a_removals_discord_time_far_from_gene_s_is_held_to_a_minute_before_it(gene, delayed):  # noqa: F811
    """As an erasure's: a press dated more than a minute before Gene carried it out (it waited, within the five
    minutes a memory control stays fresh) holds back to a minute before Gene's stamp, never further back; one within
    the minute is its own time."""
    from gene.fri_memory_store import SKEW
    asked = REAL() - delayed
    remove_a_note(gene, delayed, asked)  # Gene's clock right; the press sent `delayed` seconds before.
    stamp = profile(gene)['removed_at']
    expected = asked if delayed < SKEW else stamp - SKEW
    assert profile(gene)['removed_at_discord'] == pytest.approx(expected, abs=0.01)


def test_a_later_removal_never_moves_the_discord_cutoff_earlier(gene):  # noqa: F811
    """A removal pressed later but dated earlier on Discord's clock (Gene's clock stepped back meanwhile, or a delayed
    press) never moves the profile's cutoff on Discord's clock back."""
    first = REAL()
    remove_a_note(gene, -2, first)
    before = profile(gene)['removed_at_discord']
    remove_a_note(gene, 2, first - 1)
    assert profile(gene)['removed_at_discord'] >= before


def test_a_removal_no_interaction_dates_is_read_on_gene_s_clock_never_earlier_than_one_recorded(gene):  # noqa: F811
    """A removal written with no Discord time (a summary edit's, or an older controller's) is read on Discord's clock
    as its Gene stamp, as before, never earlier than one recorded before it."""
    from gene.fri_memory_store import fences
    first = REAL()
    remove_a_note(gene, 0, first)
    recorded = profile(gene)['removed_at_discord']
    later = REAL() + 1
    with gene.app.state.store.connection() as db:  # As a summary edit writes its fences: Gene's clock alone.
        for fence in fences():
            db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) ON CONFLICT(guild_id,channel_id,user_id,'
                       'digest) DO UPDATE SET at=max(at,excluded.at)', ('123', 'server', '20', fence, later))
    assert profile(gene)['removed_at_discord'] == pytest.approx(max(later, recorded), abs=0.01)


def test_an_erasure_keeps_a_removals_cutoff_on_discords_clock(gene):  # noqa: F811
    """With Gene's clock behind, a removal's cutoff on Discord's clock is later than its fence; an erasure that deletes
    the removal keeps it, whenever its own request was sent."""
    removal = REAL()
    remove_a_note(gene, -2, removal)
    before = profile(gene)['removed_at_discord']
    control(gene, 'disable', removal - 30, -2)  # A late request: dated before the removal.
    after = profile(gene)
    assert after['erased_at_discord'] >= before and after['removed_at_discord'] >= before


def test_a_history_channels_answer_says_the_discord_cutoff_to_a_fri_that_asks(gene):  # noqa: F811
    """A history channel's narrower answer, which older Fris check field for field, is as before; to a Fri that asks
    (discord_cutoffs, with the history-cutoffs capability), it adds removed_at_discord, so a link target there is held
    to the removal on Discord's clock."""
    from gene.fri_protocol import contract
    assert 'history-cutoffs' in contract()['capabilities']
    removal = REAL()
    remove_a_note(gene, -2, removal)
    assert set(history(gene)) == set(history(gene, False)) == {'enabled', 'revision', 'removed_at'}
    answer = history(gene, True)
    assert set(answer) == {'enabled', 'revision', 'removed_at', 'removed_at_discord'}
    assert answer['removed_at_discord'] == pytest.approx(removal, abs=0.01) == profile(gene)['removed_at_discord']


def test_red_s_own_data_deletion_holds_a_few_seconds_past_gene_s_stamp(gene):  # noqa: F811
    """Red's data deletion (the cog's red_delete_data_for_user, with no message of the member's to date it by) asks
    /privacy/erase with no source: with Gene's clock behind, the member's message from just before it is held back."""
    asked = REAL()
    with gene_clock(-2):
        assert gene.post('/privacy/erase', headers=headers('runtime'),
                         json={'guild_id': '123', 'user_id': '20'}).status_code == 200
    control(gene, 'enable', asked + 0.2, -2)
    assert not kept(gene, asked - 0.5)
    assert profile(gene)['erased_at_discord'] == pytest.approx(profile(gene)['erased_at'] + memory.MARGIN, abs=0.001)
