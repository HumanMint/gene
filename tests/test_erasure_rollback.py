"""HumanMint/gene#219: an older Gene's erasure in a rollback window, of a database this one wrote, never lowers a cutoff
this Gene reads once it's back. The deployed release (4ffd668) deletes the member's note removals when they turn
memory off and stamps the erasure with its own clock, behind this one's: re-upgraded, this Gene read that lower stamp
as the cutoff, and a message the removal held back was saved again. The removals it deletes are kept, as a floor, by
the database itself; this Gene, at its start, keeps every erasure at or past them."""

import io
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from gene.fri_memory import TTL
from gene.fri_memory_store import erased, erased_on_discord, last_removal, record_erasure
from gene.store import Store
from tests.test_fri_erasure_clock import REAL, gene, gene_clock, profile, remove_a_note, source  # noqa: F401

DEPLOYED = '4ffd668'  # v1.0.5, the release a rollback goes back to.
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def deployed(tmp_path_factory):
    """The deployed release's own code, from this checkout's history: a shallow clone (CI's) doesn't have it."""
    if subprocess.run(['git', '-C', str(ROOT), 'cat-file', '-e', DEPLOYED + '^{commit}'],
                      capture_output=True).returncode:
        pytest.skip(f'{DEPLOYED} is not in this checkout; the replayed statements below still run')
    where = tmp_path_factory.mktemp('deployed')
    archive = subprocess.run(['git', '-C', str(ROOT), 'archive', DEPLOYED, 'src'], capture_output=True, check=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(where, filter='data')
    return where/'src'


def as_deployed(code, path, offset, *actions):
    """The deployed release starts on this database, with its clock `offset` seconds off, and carries out each memory
    control for member 20, as its own code does."""
    script = ('import sys, time\nreal = time.time\ntime.time = lambda: real() + float(sys.argv[2])\n'
              'from pathlib import Path\nfrom gene.store import Store\nstore = Store(Path(sys.argv[1]))\n'
              'for action in sys.argv[3:]:\n    store.personal_control("20", action)\n')
    env = {**os.environ, 'PYTHONPATH': str(code)}
    done = subprocess.run([sys.executable, '-c', script, str(path), str(offset), *actions], cwd=code, env=env,
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]


def replayed(path, offset):
    """What the deployed release's memory off writes to these tables (4ffd668, src/gene/store.py, personal_control),
    statement for statement, where its code isn't in the checkout."""
    store = Store.__new__(Store)
    store.path = path
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM personal_note_removals WHERE user_id=?', ('20',))
        db.execute('INSERT INTO personal_erasures VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET at=excluded.at',
                   ('20', REAL() + offset))


def cutoffs(path):
    store = Store.__new__(Store)
    store.path = path
    with store.connection() as db:
        return {'removed_at': last_removal(db, '123', '20'), 'removed_at_discord': last_removal(db, '123', '20', True),
                'erased_at': erased(db, '20') or 0, 'erased_at_discord': erased_on_discord(db, '20') or 0}


def path_of(gene):  # noqa: F811
    return gene.app.state.store.path


def kept(gene, at):  # noqa: F811
    """Whether member 20's message sent at `at` on Discord's clock, answered now, keeps its exchange and note."""
    store = gene.app.state.store
    revision = store.chat_context('123', 'public:456', '20')['revision']
    store.save_chat('123', 'public:456', '20', revision, 'i love trance', 'noted', 'model', 'high', source=source(at),
                    notes=[{'category': 'music', 'evidence': 'i love trance'}])
    context = store.chat_context('123', 'public:456', '20')
    return bool(context['turns'] or context['personal_memory']['notes'])


@pytest.mark.parametrize('how', ['its own code', 'its statements'])
def test_an_older_genes_erasure_never_lowers_a_removals_cutoff(gene, deployed_or_none, how):  # noqa: F811
    """The issue's repro: member 20 removes a note with this Gene's clock two seconds ahead. Rolled back, the deployed
    Gene, its clock three seconds behind, turns their memory off and on. Re-upgraded, their message from a second
    before the removal was saved again; the cutoffs this Gene reads had moved back to the deployed one's stamp."""
    removal = REAL()
    remove_a_note(gene, 2, removal)
    before = cutoffs(path_of(gene))
    assert before['removed_at'] >= removal + 1.9
    if how == 'its own code':
        if deployed_or_none is None:
            pytest.skip(f'{DEPLOYED} is not in this checkout')
        as_deployed(deployed_or_none, path_of(gene), -3, 'disable', 'enable')
    else:
        replayed(path_of(gene), -3)
    Store(path_of(gene))  # This Gene starts again.
    after = cutoffs(path_of(gene))
    again = how == 'its own code' and kept(gene, removal - 1)  # Sent a second before the removal.
    assert not again and all(after[key] >= before['removed_at'] for key in after), (before, after, again)
    with gene.app.state.store.connection() as db:  # What it kept is in the erasure now, and no floor is left.
        assert db.execute('SELECT count(*) FROM personal_cutoff_floors').fetchone()[0] == 0


@pytest.mark.parametrize('how', ['its own code', 'its statements'])
def test_an_older_genes_erasure_never_lowers_an_earlier_erasures_stamp(gene, deployed_or_none, how):  # noqa: F811
    """This Gene stamps an erasure; rolled back, the deployed one, its clock three seconds behind, stamps another,
    earlier one over it. Re-upgraded, each cutoff stays at or past the first erasure's."""
    path = path_of(gene)
    store = Store.__new__(Store)
    store.path = path
    with gene_clock(2), store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        record_erasure(db, '20', REAL() + 2, REAL())
    before = cutoffs(path)
    if how == 'its own code':
        if deployed_or_none is None:
            pytest.skip(f'{DEPLOYED} is not in this checkout')
        as_deployed(deployed_or_none, path, -3, 'disable', 'enable')
    else:
        replayed(path, -3)
    Store(path)
    after = cutoffs(path)
    assert after['erased_at'] >= before['erased_at'] and after['erased_at_discord'] >= before['erased_at_discord']
    assert after['removed_at'] >= before['removed_at'], (before, after)


def test_an_erasure_stamp_an_older_gene_moved_back_before_this_release_is_put_back(gene):  # noqa: F811
    """A database an older Gene wrote that way before this release (so before its triggers): this Gene stamped an
    erasure, then the older one stamped an earlier one over it. At its first start, each cutoff is put back at or past
    the first erasure's."""
    path = path_of(gene)
    store = Store.__new__(Store)
    store.path = path
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        record_erasure(db, '20', REAL() + 2, REAL())
    before = cutoffs(path)
    with store.connection() as db:
        db.execute('DROP TRIGGER erasure_kept_when_moved_back')  # As before this release.
    replayed(path, -3)
    assert cutoffs(path)['erased_at'] < before['erased_at']
    Store(path)
    after = cutoffs(path)
    assert after['erased_at'] >= before['erased_at'] and after['erased_at_discord'] >= before['erased_at_discord']


def test_a_removal_that_lapses_raises_no_erasure(gene):  # noqa: F811
    """Guard: a removal deleted once it's past TTL, as this Gene's own expiry does, is kept as no floor: an erasure
    before it stays where it was."""
    path = path_of(gene)
    store = Store.__new__(Store)
    store.path = path
    with store.connection() as db:
        db.execute('BEGIN IMMEDIATE')
        record_erasure(db, '20', REAL() - TTL - 7200)
        db.execute("INSERT INTO personal_note_removals VALUES ('123','server','20','x',?)", (REAL() - TTL - 3600,))
    with store.connection() as db:
        db.execute('DELETE FROM personal_note_removals WHERE at<?', (REAL() - TTL,))
    before = cutoffs(path)
    Store(path)
    assert cutoffs(path) == before


def test_this_genes_own_erasure_leaves_no_floor_behind(gene):  # noqa: F811
    """Guard: this Gene's own memory off carries the removals into the erasure itself, as before, and leaves nothing
    for a later start to reconcile."""
    remove_a_note(gene, 0)
    gene.app.state.store.personal_control('20', 'disable')
    with gene.app.state.store.connection() as db:
        assert db.execute('SELECT count(*) FROM personal_cutoff_floors').fetchone()[0] == 0


@pytest.fixture
def deployed_or_none(request):
    """The deployed release's code, or None where the checkout doesn't have it."""
    try:
        return request.getfixturevalue('deployed')
    except pytest.skip.Exception:
        return None


REMOVES = ('import sys, time\nreal = time.time\ntime.time = lambda: real() + float(sys.argv[2])\n'
           'from pathlib import Path\nfrom gene.store import Store\nfrom gene.fri_memory_store import edit, entries\n'
           'store = Store(Path(sys.argv[1]))\nlisted = entries(store, "123", "private:900", "20")\n'
           'asked = {"kind": "discord_interaction", "id": sys.argv[3], "user_id": "20", "display_name": "friend",\n'
           '         "channel_id": "900", "guild_id": None}\n'
           'edit(store, "123", "private:900", "20", listed["entries"][0]["id"], listed["revision"], asked, "remove")\n')


@pytest.mark.parametrize('how', ['its own code', 'its statements'])
def test_an_older_genes_removal_never_moves_a_later_one_back(gene, deployed_or_none, how):  # noqa: F811
    """This Gene removes one of member 20's profile notes with its clock two seconds ahead. Rolled back, the deployed
    Gene, its clock three seconds behind, removes another: it writes its own time over the first removal's, earlier.
    Re-upgraded, their message from a second before the first removal was saved again."""
    from gene.fri_memory_store import fences, save
    with gene.app.state.store.connection() as db:
        assert save(db, '123', 'server', '20', ('music', 'I love trance'), REAL() - 1, provenance=[])
    removal = REAL()
    remove_a_note(gene, 2, removal)  # Its own note, the newer one.
    before = cutoffs(path_of(gene))
    if how == 'its own code':
        if deployed_or_none is None:
            pytest.skip(f'{DEPLOYED} is not in this checkout')
        env = {**os.environ, 'PYTHONPATH': str(deployed_or_none)}
        done = subprocess.run([sys.executable, '-c', REMOVES, str(path_of(gene)), '-3', source(REAL())['id']],
                              cwd=deployed_or_none, env=env, capture_output=True, text=True)
        assert done.returncode == 0, done.stderr[-2000:]
    else:  # Its removal's fences, as 4ffd668's edit writes them.
        with gene.app.state.store.connection() as db:
            for fence in fences():
                db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) ON CONFLICT(guild_id,channel_id,'
                           'user_id,digest) DO UPDATE SET at=excluded.at', ('123', 'server', '20', fence, REAL() - 3))
    Store(path_of(gene))  # This Gene starts again.
    after = cutoffs(path_of(gene))
    again = how == 'its own code' and kept(gene, removal - 1)
    assert not again and after['removed_at'] >= before['removed_at'], (before, after, again)


@pytest.mark.parametrize('seed, steps, real', [*((seed, 10, False) for seed in range(12)),
                                               *((seed, 6, True) for seed in range(2))])
def test_no_sequence_with_an_older_genes_writes_lowers_a_cutoff_once_this_gene_is_back(tmp_path, deployed_or_none,
                                                                                         seed, steps, real):
    """Round 2's invariant of #213 with the deployed release writing too, its clock anywhere within five seconds of
    Discord's: its memory off and on, and its note removals, each followed by this Gene starting again. No cutoff a
    reader uses goes back once this Gene is running: erased_at and removed_at, both on Discord's clock, and learning's.
    Its statements replayed, and its own code where the checkout has it."""
    import random

    from fastapi.testclient import TestClient

    from gene.api import create_app
    from gene.config import Settings
    from gene.fri_memory_store import fences
    from tests.test_api import headers
    from tests.test_fri_erasure_clock import control
    from tests.test_fri_erasure_clock import cutoffs as profile_cutoffs
    if real and deployed_or_none is None:
        pytest.skip(f'{DEPLOYED} is not in this checkout')
    rng = random.Random(seed)
    settings = Settings(tmp_path/'invariant.db', '123', '10', 'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32,
                        workshop_channel_id='456', fri_token='fri-'+'x'*32, agent_channels='1,456',
                        agent_history_channels='1')
    gene = TestClient(create_app(settings))  # noqa: F811
    path = tmp_path/'invariant.db'
    last = profile_cutoffs(gene)
    for _ in range(steps):
        offset = rng.uniform(-5, 5)
        op = rng.choice(['control', 'erase', 'remove', 'enable', 'older erase', 'older remove', 'older remove'])
        now = REAL()
        if op == 'control':
            control(gene, 'disable', now - rng.choice([0, 1, 10, 600]), offset)
        elif op == 'erase':
            with gene_clock(offset):
                assert gene.post('/privacy/erase', headers=headers('runtime'), json={
                    'guild_id': '123', 'user_id': '20', 'source_id': source(now - rng.choice([0, 10]))['id']}
                ).status_code == 200
        elif op == 'remove':
            if profile(gene)['enabled']:
                remove_a_note(gene, offset)
        elif op == 'enable':
            control(gene, 'enable', now, offset)
        elif op == 'older erase':
            if real:
                as_deployed(deployed_or_none, path, offset, 'disable', 'enable')
            else:
                replayed(path, offset)
            Store(path)
        else:  # An older Gene's removal: its fences at its own time.
            if real and profile(gene)['enabled']:
                from gene.fri_memory_store import save
                with gene.app.state.store.connection() as db:
                    save(db, '123', 'server', '20', ('music', 'I love trance'), REAL(), provenance=[])
                env = {**os.environ, 'PYTHONPATH': str(deployed_or_none)}
                done = subprocess.run([sys.executable, '-c', REMOVES, str(path), str(offset), source(REAL())['id']],
                                      cwd=deployed_or_none, env=env, capture_output=True, text=True)
                assert done.returncode == 0 or 'IndexError' in done.stderr, done.stderr[-2000:]
            elif not real:
                with gene.app.state.store.connection() as db:
                    for fence in fences():
                        db.execute('INSERT INTO personal_note_removals VALUES (?,?,?,?,?) ON CONFLICT(guild_id,'
                                   'channel_id,user_id,digest) DO UPDATE SET at=excluded.at',
                                   ('123', 'server', '20', fence, REAL() + offset))
            Store(path)
        current = profile_cutoffs(gene)
        assert all(current[key] >= last[key] for key in current), (op, offset, last, current)
        last = current
