"""HumanMint/gene#223: My memory's edits check the source's claimed author and channel, but not a message Gene has
recorded as another member's. A Fri request carrying member 30's saved message, relabelled as member 20's, edited member
20's notes. Gene rejects a source whose recorded author is someone else, as an erasure ignores one (Sol round 1 of
#213), and a member's own edit still goes through."""

import pytest

from tests.test_api import headers
from tests.test_fri_erasure_clock import DM, REAL, gene, my_memory, profile_notes, source  # noqa: F401


def pressed_with(gene, evidence, action, message_id, replacement=''):  # noqa: F811
    """Member 20's press on one of their notes in My memory, its source the event `message_id`, labelled as theirs."""
    listed = my_memory(gene)
    [note] = [entry for entry in listed['entries'] if entry['evidence'] == evidence]
    asked = {'kind': 'discord_interaction', 'id': message_id, 'user_id': '20', 'display_name': 'friend',
             'channel_id': '900', 'guild_id': None}
    return gene.post(f"/chat/memory/notes/{note['id']}", headers=headers('fri'), json={
        **DM, 'revision': listed['revision'], 'source': asked, 'action': action, 'evidence': replacement})


def saved_message(gene, user, at=None):  # noqa: F811
    """A message of `user`'s that Gene saved an exchange for; its ID."""
    store = gene.app.state.store
    said = source(REAL() if at is None else at, user=user)
    revision = store.chat_context('123', 'public:456', user)['revision']
    store.save_chat('123', 'public:456', user, revision, 'hello there', 'hi', 'model', 'high', source=said)
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM chat_turns WHERE json_extract(source,'$.id')=?",
                          (said['id'],)).fetchone()[0] == 1
    return said['id']


def notes_now(gene):  # noqa: F811
    return sorted((entry['scope'], entry['evidence']) for entry in my_memory(gene)['entries'])


@pytest.mark.parametrize('action, replacement', [('replace', 'I love techno'), ('keep', ''), ('remove', '')])
def test_an_edit_with_a_message_gene_has_as_someone_elses_is_refused(gene, action, replacement):  # noqa: F811
    """The issue's repro: member 30's saved message M, relabelled as member 20's, in member 20's Replace or Keep here
    only (and Remove, which the same source dates). Each returned 200. Now each is refused and nothing changes."""
    theirs = saved_message(gene, '30')
    profile_notes(gene, ('music', 'I love trance'))
    before = notes_now(gene)
    answer = pressed_with(gene, 'I love trance', action, theirs, replacement)
    assert answer.status_code == 400, answer.text
    assert notes_now(gene) == before


@pytest.mark.parametrize('action, replacement, after', [
    ('replace', 'I love techno', [('server', 'I love techno')]),
    ('keep', '', [('private:900', 'I love trance')]),
    ('remove', '', [])])
def test_an_edit_with_the_members_own_message_or_a_fresh_press_goes_through(gene, action, replacement, after):  # noqa: F811
    """Guard (round 3 of #213's lesson): a source Gene has as the member's own message, or one it has never seen (a
    fresh panel press), still carries out their edit."""
    for message_id in (saved_message(gene, '20'), source(REAL())['id']):
        profile_notes(gene, ('music', 'I love trance'))
        answer = pressed_with(gene, 'I love trance', action, message_id, replacement)
        assert answer.status_code == 200, answer.text
        assert notes_now(gene) == after
        if after:
            pressed_with(gene, after[0][1], 'remove', source(REAL())['id'])  # Back to none, for the next source.
        assert notes_now(gene) == []
