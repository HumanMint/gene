"""fri #84: Fri's attention checks every author it is about to send in one controller call, not one read each."""
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gene.api import create_app
from gene.config import Settings
from gene.fri_protocol import compatible, contract
from gene.store import Store
from tests.test_api import headers

ALICE, BOB, CAROL = '1001', '1002', '1003'


@pytest.fixture
def fence(tmp_path):
    settings = Settings(Path(tmp_path/'fence.sqlite3'), '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32,
                        'deploy-'+'x'*32, workshop_channel_id='50', fri_token='fri-'+'x'*32,
                        agent_channels='51,52', agent_history_channels='53', meme_channel_ids='54',
                        test_guild_id='777', test_channel_id='78')
    client = TestClient(create_app(settings))
    return client, client.app.state.store


def check(client, authors, channel='51', guild='123', role='fri'):
    return client.post('/agent/authors/check', headers=headers(role),
                       json={'guild_id': guild, 'channel_id': channel, 'authors': authors})


def at(*revisions):
    return [{'user_id': user, 'revision': revision} for user, revision in revisions]


def test_every_author_still_at_their_revision_passes(fence):
    client, store = fence
    store.clear_chats(BOB)  # Bob is at revision 1 now, and was read there.
    response = check(client, at((ALICE, 0), (BOB, 1), (ALICE, 0)))
    assert response.status_code == 200 and response.json() == {'ok': True}
    assert check(client, []).json() == {'ok': True}  # Nothing to fence: only Gene errors in the batch, say.


@pytest.mark.parametrize('erase', [
    lambda client, store: client.post('/privacy/erase', headers=headers('runtime'),  # gene forgetme, through Red
                                      json={'guild_id': '123', 'user_id': ALICE}),
    lambda client, store: store.personal_control(ALICE, 'disable'),
    lambda client, store: store.clear_chats(ALICE)])
def test_one_author_erasing_through_gene_refuses_the_whole_batch_and_names_nobody(fence, erase):
    client, store = fence
    erase(client, store)
    response = check(client, at((BOB, 0), (ALICE, 0), (CAROL, 0)))
    assert response.status_code == 409
    assert ALICE not in response.text and BOB not in response.text and CAROL not in response.text


def test_memory_off_refuses_even_at_the_same_revision(fence):
    client, store = fence
    with store.connection() as db:
        db.execute('INSERT INTO personal_preferences VALUES (?,0)', (ALICE,))
    assert check(client, at((ALICE, 0))).status_code == 409


def test_an_author_collected_under_two_revisions_is_stale_in_one_of_them(fence):
    client, _ = fence
    assert check(client, at((ALICE, 0), (ALICE, 1))).status_code == 409


def test_no_erasure_can_land_between_two_authors_reads(fence, monkeypatch):
    client, store = fence
    seen, original = [], Store._privacy_state

    def read(db, user_id):
        if not seen:  # After the first author's read and before the next, another writer tries to erase.
            writer = sqlite3.connect(store.path, timeout=0)
            try:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    writer.execute('BEGIN IMMEDIATE')
            finally:
                writer.close()
        seen.append(user_id)
        return original(db, user_id)

    monkeypatch.setattr(Store, '_privacy_state', staticmethod(read))
    assert check(client, at((ALICE, 0), (BOB, 0))).json() == {'ok': True}
    assert seen == [ALICE, BOB]


def test_scope_matches_the_profile_read_it_replaces(fence):
    client, _ = fence
    for channel in ('50', '52', '53', '54'):  # Workshop, chat, history and meme channels, as /agent/context.
        assert check(client, at((ALICE, 0)), channel=channel).status_code == 200
    assert check(client, at((ALICE, 0)), channel='78', guild='777').status_code == 200  # The lab room.
    for guild, channel in (('123', '99'), ('123', ''), ('777', '51'), ('456', '51')):
        assert check(client, at((ALICE, 0)), channel=channel, guild=guild).status_code == 403
    for role in ('runtime', 'worker', 'deploy'):
        assert check(client, at((ALICE, 0)), role=role).status_code == 401


@pytest.mark.parametrize('authors', [
    [{'user_id': 'alice', 'revision': 0}], [{'user_id': ALICE, 'revision': -1}], [{'user_id': ALICE}],
    at(*((str(1000+n), 0) for n in range(201)))])
def test_malformed_or_oversized_batches_are_rejected(fence, authors):
    client, _ = fence
    assert check(client, authors).status_code == 422


def test_the_contract_offers_it_as_an_optional_capability():
    advertised = contract()
    assert 'author-fence' in advertised['capabilities']
    assert compatible({**advertised, 'capabilities': [c for c in advertised['capabilities'] if c != 'author-fence']})
