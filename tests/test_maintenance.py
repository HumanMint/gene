import datetime as dt
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from gene import fri_release, maintenance
from gene.deploy import Deployer
from gene.store import Store
from tests.conftest import proposed

SCRIPT = Path(__file__).parents[1] / 'ops/maintainer/batch-release.sh'
G, F = 'b' * 40, 'c' * 40
OLD_G, OLD_F = 'a' * 40, 'd' * 40


def at(text):
    return dt.datetime.fromisoformat(text).timestamp()


def clock(monkeypatch, text):
    monkeypatch.setattr(maintenance, 'clock', lambda: at(text))


BOUNDARIES = [
    ('2026-10-28T03:59:59-07:00', False),
    ('2026-10-28T04:00:00-07:00', True),
    ('2026-10-28T04:59:59-07:00', True),
    ('2026-10-28T05:00:00-07:00', False),
    ('2026-10-28T16:30:00-07:00', False),  # an overdue wake is no daytime catch-up
    # Fall back (2026-11-01 02:00 PDT -> 01:00 PST): the window opens at 12:00Z, not 11:00Z.
    ('2026-11-01T11:30:00+00:00', False),
    ('2026-11-01T04:00:00-08:00', True),
    ('2026-11-01T04:59:59-08:00', True),
    ('2026-11-01T05:00:00-08:00', False),
    # Spring forward (2027-03-14 02:00 PST -> 03:00 PDT): the window opens at 11:00Z, not 12:00Z.
    ('2027-03-13T04:30:00-08:00', True),
    ('2027-03-14T11:00:00+00:00', True),
    ('2027-03-14T12:30:00+00:00', False),
]


def bash_major():
    try:
        out = subprocess.run(['bash', '-c', 'echo ${BASH_VERSINFO[0]}'], capture_output=True, text=True, check=True)
        return int(out.stdout)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return 0


needs_bash4 = pytest.mark.skipif(bash_major() < 4, reason='batch-release.sh needs bash 4 or later')


def functions(*names):
    """The named shell functions from batch-release.sh, to run on their own."""
    out, keep = [], False
    for line in SCRIPT.read_text().splitlines():
        starts = any(line.startswith(name+'() {') for name in names)
        if starts and line.split('  #')[0].rstrip().endswith('}'):
            out.append(line)  # a one-line function
            continue
        keep = keep or starts
        if keep:
            out.append(line)
            keep = line != '}'
    return '\n'.join(out)


def fake_date(tmp_path, epoch):
    """A date that answers the window's clock (TZ=UTC date +%s) with epoch; everything else is real."""
    bin_dir = tmp_path/'bin'
    bin_dir.mkdir(exist_ok=True)
    real = next(p for p in ('/bin/date', '/usr/bin/date') if os.path.exists(p))
    (bin_dir/'date').write_text(f'#!/usr/bin/env bash\nif [[ ${{TZ:-}} == UTC && $* == +%s ]]; then echo {epoch}; '
                                f'else exec {real} "$@"; fi\n')
    (bin_dir/'date').chmod(0o755)
    return {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}


@pytest.mark.parametrize('when,inside', BOUNDARIES)
def test_window_boundaries_and_daylight_saving(when, inside):
    assert maintenance.is_open(at(when)) is inside


@needs_bash4
@pytest.mark.parametrize('when,inside', BOUNDARIES)
def test_the_release_script_keeps_the_same_window(when, inside):
    out = subprocess.run(['bash', '-c', functions('window') + f'\nwindow {int(at(when))}'],
                         capture_output=True, text=True, check=True)
    assert out.stdout.split()[0] == ('open' if inside else 'closed')


def test_an_unknown_clock_or_zone_is_closed(monkeypatch):
    assert not maintenance.is_open(float('nan'))
    assert not maintenance.is_open(1_000_000_000)  # a reset clock reads 2001
    monkeypatch.setattr(maintenance, 'clock', Mock(side_effect=OSError))
    assert not maintenance.is_open()
    clock(monkeypatch, '2026-10-28T04:30:00-07:00')
    assert maintenance.is_open()
    monkeypatch.setattr(maintenance, 'ZONE', 'Nowhere/Missing')
    assert not maintenance.is_open()


@needs_bash4
def test_the_release_script_refuses_a_clock_it_cannot_trust(tmp_path):
    out = subprocess.run(['bash', '-c', functions('window') + '\nwindow 1000000000'], capture_output=True, text=True)
    assert out.returncode != 0
    out = subprocess.run(['bash', '-c', functions('now_epoch', 'window', 'window_open') + '\nwindow_open'],
                         capture_output=True, text=True, env=fake_date(tmp_path, 'not-a-number'))
    assert out.returncode != 0


# A running controller's SQLite creates and removes the DB's journal files at any moment. On 2026-10-28
# one vanished between repair_sidecars' look and its stat, and the chown that followed failed the Gene switch.
FAKE_STAT = '''#!/usr/bin/env bash
for file; do :; done  # stat -c %u:%g FILE; owners come from $CTL/<name>.owner
if [[ $file == "$(cat "$CTL/vanish-at-stat" 2>/dev/null)" ]]; then rm -f "$file"; fi
[[ -e $file ]] || { echo "stat: cannot statx '$file': No such file or directory" >&2; exit 1; }
cat "$CTL/${file##*/}.owner" 2>/dev/null || echo 10001:10001
'''
FAKE_CHOWN = '''#!/usr/bin/env bash
echo "$1 ${2##*/}" >> "$CTL/chowned"
if [[ $2 == "$(cat "$CTL/vanish-at-chown" 2>/dev/null)" ]]; then rm -f "$2"; fi
[[ -e $2 ]] || { echo "chown: cannot dereference '$2': No such file or directory" >&2; exit 1; }
[[ ! -e $CTL/chown-denied ]] || { echo "chown: changing ownership of '$2': Operation not permitted" >&2; exit 1; }
echo "$1" > "$CTL/${2##*/}.owner"
'''


def sidecar_shell(tmp_path, wal_owner, vanish=None, denied=False):
    """repair_sidecars on a root-owned or owner-owned WAL that SQLite removes at the chosen moment."""
    ctl, data, bin_dir = tmp_path/'ctl', tmp_path/'data', tmp_path/'bin'
    for path in ctl, data, bin_dir:
        path.mkdir()
    db = data/'gene.sqlite3'
    db.touch()
    (data/'gene.sqlite3-wal').touch()
    (ctl/'gene.sqlite3-wal.owner').write_text(wal_owner+'\n')
    if vanish:
        (ctl/f'vanish-at-{vanish}').write_text(f'{db}-wal')
    if denied:
        (ctl/'chown-denied').touch()
    for name, body in ('stat', FAKE_STAT), ('chown', FAKE_CHOWN):
        (bin_dir/name).write_text(body)
        (bin_dir/name).chmod(0o755)
    shell = (f'set -Eeuo pipefail\nPHASE=gene DB={db} SWITCHING=0\n'
             + functions('log', 'die', 'db_owner', 'sidecar_owner', 'repair_sidecars')
             + '\nrepair_sidecars\necho repaired\n')
    out = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30,
                         env={**os.environ, 'CTL': str(ctl), 'PATH': f'{bin_dir}:{os.environ["PATH"]}'})
    chowned = (ctl/'chowned').read_text().splitlines() if (ctl/'chowned').exists() else []
    return out, chowned, data/'gene.sqlite3-wal'


@pytest.mark.parametrize('wal_owner,vanish', [('0:0', 'stat'), ('0:0', 'chown'), ('10001:10001', 'stat')])
def test_a_journal_file_that_vanishes_mid_repair_needs_nothing(tmp_path, wal_owner, vanish):
    out, chowned, wal = sidecar_shell(tmp_path, wal_owner, vanish)
    assert out.returncode == 0 and out.stdout.split() == ['repaired'], out.stderr
    assert not wal.exists()
    assert chowned == (['10001:10001 gene.sqlite3-wal'] if vanish == 'chown' else [])


def test_a_root_owned_journal_file_goes_back_to_the_db_owner(tmp_path):
    out, chowned, wal = sidecar_shell(tmp_path, '0:0')
    assert out.returncode == 0 and 'giving gene.sqlite3-wal back to the database owner' in out.stderr
    assert chowned == ['10001:10001 gene.sqlite3-wal'] and wal.exists()


def test_a_chown_that_fails_on_a_journal_file_still_there_stops_the_script(tmp_path):
    out, chowned, wal = sidecar_shell(tmp_path, '0:0', denied=True)
    assert out.returncode != 0 and 'repaired' not in out.stdout
    assert 'FAIL: could not give gene.sqlite3-wal back to the database owner' in out.stderr
    assert wal.exists()


@pytest.mark.parametrize('printed,stops', [('4096', False), ('', True)])
def test_sizing_a_tree_counts_a_file_that_vanishes_as_gone(tmp_path, printed, stops):
    bin_dir = tmp_path/'bin'
    bin_dir.mkdir()
    # GNU du prints the total, then fails, when a file goes between its readdir and its stat.
    (bin_dir/'du').write_text('#!/usr/bin/env bash\n'
                              f'[[ -z "{printed}" ]] || printf "%s\\t%s\\n" "{printed}" "$2"\n'
                              'echo "du: cannot access \'$2/gene.sqlite3-wal\': No such file or directory" >&2\n'
                              'exit 1\n')
    (bin_dir/'du').chmod(0o755)
    shell = ('set -Eeuo pipefail\nSWITCHING=0\n' + functions('log', 'die', 'du_bytes')
             + f'\nsize=$(du_bytes {tmp_path})\necho "size=$size"\n')
    out = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30,
                         env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'})
    if stops:
        assert out.returncode != 0 and 'size=' not in out.stdout
    else:
        assert out.returncode == 0 and out.stdout.split() == ['size=4096'], out.stderr


def test_claims_wait_for_the_window_and_an_owner_rollback_does_not(store, monkeypatch):
    job = proposed(store)
    store.approve(job['id'], G, 'owner')
    for when in ('2026-10-28T03:59:59-07:00', '2026-10-28T04:50:00-07:00', '2026-10-28T05:20:00-07:00',
                 '2026-10-28T16:00:00-07:00'):
        clock(monkeypatch, when)
        assert store.claim_deploy() is None
    assert store.get(job['id'])['state'] == 'approved'  # deferred, untouched
    clock(monkeypatch, '2026-10-29T04:49:59-07:00')  # the last claim leaves time for the switch
    release = store.claim_deploy()
    assert release['id'] == job['id']
    store.finish_deploy(release['id'], release['lease'], True, 'Healthy')
    clock(monkeypatch, '2026-10-29T13:00:00-07:00')
    rollback = store.rollback('guild', 'owner')
    assert store.claim_deploy()['id'] == rollback['id']


def test_an_unknown_zone_defers_every_claim_but_a_rollback(store, monkeypatch):
    job = proposed(store)
    store.approve(job['id'], G, 'owner')
    monkeypatch.setattr(maintenance, 'ZONE', 'Nowhere/Missing')
    assert store.claim_deploy() is None
    assert store.get(job['id'])['state'] == 'approved'


def clocks(monkeypatch, text):
    """The window's clock and the controller's record clock, stopped together as on the host."""
    clock(monkeypatch, text)
    monkeypatch.setattr(time, 'time', lambda: at(text))


def drained(store, when):
    """The audit row batch-release.sh's drain leaves in the controller DB."""
    with store.connection() as db:
        db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
                   (at(when), 'maintainer:claude', maintenance.DRAINED, 'batch-x', ''))


def approved(store, sha, source):
    job = proposed(store, sha, source)
    store.approve(job['id'], sha, 'owner')
    return job


@pytest.mark.parametrize('success', [True, False])
def test_an_automatic_deploy_uses_the_days_batch_whatever_its_outcome(store, monkeypatch, success):
    clocks(monkeypatch, '2026-10-28T04:05:00-07:00')
    first = approved(store, G, 'message-1')
    claimed = store.claim_deploy()
    assert claimed['id'] == first['id']
    store.finish_deploy(claimed['id'], claimed['lease'], success, 'Healthy' if success else 'Failed')
    second = approved(store, 'e'*40, 'message-2')
    clocks(monkeypatch, '2026-10-28T04:30:00-07:00')
    assert store.claim_deploy() is None
    assert store.get(second['id'])['state'] == 'approved'  # deferred, untouched
    clocks(monkeypatch, '2026-10-29T04:05:00-07:00')  # the next day's window
    assert store.claim_deploy()['id'] == second['id']


def test_a_maintainer_drain_today_defers_automatic_claims(store, monkeypatch):
    job = approved(store, G, 'message-1')
    drained(store, '2026-10-27T04:10:00-07:00')  # yesterday's batch leaves today alone
    drained(store, '2026-10-28T04:02:00-07:00')
    clocks(monkeypatch, '2026-10-28T04:30:00-07:00')
    assert store.claim_deploy() is None
    assert store.get(job['id'])['state'] == 'approved'
    clocks(monkeypatch, '2026-10-29T04:05:00-07:00')
    assert store.claim_deploy()['id'] == job['id']


def test_rollbacks_neither_wait_for_nor_use_the_days_batch(store, monkeypatch):
    clocks(monkeypatch, '2026-10-28T04:05:00-07:00')
    first = approved(store, G, 'message-1')
    claimed = store.claim_deploy()
    store.finish_deploy(claimed['id'], claimed['lease'], True, 'Healthy')
    clocks(monkeypatch, '2026-10-28T13:00:00-07:00')  # today's batch is used; restoring service isn't held
    rollback = store.rollback('guild', 'owner')
    claimed = store.claim_deploy()
    assert claimed['id'] == rollback['id'] != first['id']
    store.finish_deploy(claimed['id'], claimed['lease'], True, 'Healthy')
    clocks(monkeypatch, '2026-10-29T01:00:00-07:00')
    drained(store, '2026-10-28T23:59:59-07:00')
    rollback = store.rollback('guild', 'owner')
    claimed = store.claim_deploy()
    assert claimed['id'] == rollback['id']
    store.finish_deploy(claimed['id'], claimed['lease'], True, 'Healthy')
    job = approved(store, 'e'*40, 'message-2')
    clocks(monkeypatch, '2026-10-29T04:10:00-07:00')  # the night's rollback left the day's batch unused
    assert store.claim_deploy()['id'] == job['id']


def test_a_critical_fix_skips_the_cap_but_not_the_window(store, monkeypatch, tmp_path):
    job = approved(store, G, 'message-1')
    drained(store, '2026-10-28T04:02:00-07:00')
    clocks(monkeypatch, '2026-10-28T04:20:00-07:00')
    assert store.claim_deploy() is None
    assert maintenance.mark_critical(store.path, job['id'], 'P0: replies leak a DM', 'maintainer:test') == G
    clocks(monkeypatch, '2026-10-28T05:20:00-07:00')
    assert store.claim_deploy() is None  # still only inside the window
    clocks(monkeypatch, '2026-10-28T04:30:00-07:00')
    assert store.claim_deploy()['id'] == job['id']
    with store.connection() as db:
        assert tuple(db.execute('SELECT actor,detail FROM audit WHERE action="critical_fix" AND target=?',
                                (job['id'],)).fetchone()) == ('maintainer:test', f'{G}: P0: replies leak a DM')


def test_a_critical_mark_holds_only_for_the_commit_it_named(store, monkeypatch):
    job = approved(store, G, 'message-1')
    with store.connection() as db:
        db.execute('INSERT INTO critical_fixes VALUES (?,?,?,?,?)', (job['id'], 'e'*40, 'P0', 'test', 0))
    drained(store, '2026-10-28T04:02:00-07:00')
    clocks(monkeypatch, '2026-10-28T04:20:00-07:00')
    assert store.claim_deploy() is None


def test_an_unknown_clock_zone_or_history_counts_as_a_used_batch(store, monkeypatch):
    job = store.get(approved(store, G, 'message-1')['id'])
    clocks(monkeypatch, '2026-10-28T04:20:00-07:00')
    with store.connection() as db:
        assert maintenance.batch_used(db, job) is False
        monkeypatch.setattr(maintenance, 'clock', lambda: float('nan'))
        assert maintenance.batch_used(db, job) is True
        clock(monkeypatch, '2026-10-28T04:20:00-07:00')
        monkeypatch.setattr(maintenance, 'ZONE', 'Nowhere/Missing')
        assert maintenance.batch_used(db, job) is True
        monkeypatch.setattr(maintenance, 'ZONE', 'America/Los_Angeles')
        # A deployment dated after now (the clock went back), or one whose job is gone, still counts.
        db.execute('INSERT INTO deployments VALUES (?,?,?,?,?)', ('gone', G, OLD_G, 'deployed', at('2026-10-30T04:00:00-07:00')))
        assert maintenance.batch_used(db, job) is True


@pytest.mark.parametrize('reason,state,kind,error', [
    ('', 'approved', 'feature', 'P0 reason'),
    ('   ', 'approved', 'feature', 'P0 reason'),
    ('P0: crash', 'review', 'feature', 'approved feature or repair'),
    ('P0: crash', 'approved', 'rollback', 'approved feature or repair'),
    ('P0: crash', 'deployed', 'repair', 'approved feature or repair'),
])
def test_marking_a_critical_fix_refuses_bad_input(store, reason, state, kind, error):
    job = approved(store, G, 'message-1')
    with store.connection() as db:
        db.execute('UPDATE jobs SET state=?,kind=? WHERE id=?', (state, kind, job['id']))
    with pytest.raises(ValueError, match=error):
        maintenance.mark_critical(store.path, job['id'], reason, 'maintainer:test')
    with pytest.raises(ValueError, match='approved feature or repair'):
        maintenance.mark_critical(store.path, 'f'*12, 'P0: crash', 'maintainer:test')
    with store.connection() as db:
        assert not db.execute('SELECT 1 FROM critical_fixes').fetchone()
        assert not db.execute('SELECT 1 FROM audit WHERE action="critical_fix"').fetchone()


def test_the_critical_command_logs_its_reason_and_lists_marks_without_member_content(tmp_path, monkeypatch, capsys):
    root = tmp_path/'srv/gene'
    store = Store(root/'data/controller/gene.sqlite3')
    store.bootstrap(OLD_G)
    store.update_models([{'id': 'm', 'name': 'M', 'efforts': ['high'], 'default_effort': 'high'}])
    store.select_model('m', 'high', 'owner')
    store.set_paused(False, 'owner')
    job = approved(store, G, 'message-1')
    clocks(monkeypatch, '2026-10-28T10:00:00-07:00')
    monkeypatch.setenv('SUDO_USER', 'mint')
    maintenance.main(['--root', str(root), 'critical', job['id'], '--reason', 'P0: a crash on every reply'])
    row = json.loads((root/'production-actions.jsonl').read_text().splitlines()[-1])
    assert row['action'] == f"mark critical fix job {job['id']}" and row['outcome'] == 'ok'
    assert row['exception'] == 'P0: a crash on every reply' and row['actor'] == 'mint'
    assert row['at'].startswith('2026-10-28T10:00:00-07:00')
    capsys.readouterr()
    maintenance.main(['--root', str(root), 'critical-list'])
    listed = json.loads(capsys.readouterr().out)
    assert listed == [{'job_id': job['id'], 'kind': 'feature', 'state': 'approved', 'sha': G, 'current': True,
                       'reason': 'P0: a crash on every reply', 'actor': 'maintainer:mint',
                       'marked_at': '2026-10-28T10:00:00-07:00'}]
    with pytest.raises(SystemExit, match='P0 reason'):
        maintenance.main(['--root', str(root), 'critical', job['id'], '--reason', ' '])
    assert json.loads((root/'production-actions.jsonl').read_text().splitlines()[-1])['outcome'].startswith('refused')
    with pytest.raises(SystemExit):  # a wrong root never creates a database
        maintenance.main(['--root', str(tmp_path/'nowhere'), 'critical', job['id'], '--reason', 'P0: crash'])
    assert not (tmp_path/'nowhere/data/controller/gene.sqlite3').exists()


def deployer(tmp_path):
    Store(tmp_path/'data/controller/gene.sqlite3').bootstrap(OLD_G)
    (tmp_path/'data/red').mkdir(parents=True)
    (tmp_path/'release.env').write_text(f'GENE_RELEASE_SHA={OLD_G}\nGENE_RUNTIME_IMAGE=gene-runtime:x\n')
    result = Deployer.__new__(Deployer)
    result.root, result.restart_s = tmp_path, None
    result.prepare, result.compose, result.healthy = Mock(), Mock(), Mock(return_value=True)
    result.activate = Mock(side_effect=lambda sha: (tmp_path/'release.env').write_text(
        f'GENE_RELEASE_SHA={sha}\nGENE_RUNTIME_IMAGE=gene-runtime:x\n'))
    return result


def test_the_deployer_never_starts_a_feature_switch_outside_the_window(tmp_path, monkeypatch):
    clock(monkeypatch, '2026-10-28T05:00:00-07:00')
    worker = deployer(tmp_path)
    job = {'id': 'job', 'kind': 'feature', 'proposal': {'sha': G, 'base_sha': OLD_G}}
    with pytest.raises(RuntimeError, match='Outside the maintenance window'):
        worker.process(job)
    worker.prepare.assert_not_called()
    worker.compose.assert_not_called()
    worker.process(job | {'kind': 'rollback'})  # restoring service runs at any hour
    worker.compose.assert_called_once_with('stop', 'red')


def test_each_deployment_leaves_a_bounded_record_without_member_content(tmp_path, monkeypatch):
    worker = deployer(tmp_path)
    job = {'id': 'a1b2c3d4e5f6', 'kind': 'feature', 'lease': 'f'*32, 'body': 'a member said something private',
           'proposal': {'sha': G, 'base_sha': OLD_G}}
    worker.api = Mock(post=Mock(side_effect=lambda path, data=None: job if path == '/deploy/claim' else {'ok': True}))
    assert worker.tick()
    row = json.loads((tmp_path/'production-actions.jsonl').read_text().splitlines()[-1])
    assert row['action'] == 'deploy feature job a1b2c3d4e5f6' and row['outcome'] == 'ok'
    assert row['before'] == {'gene': OLD_G} and row['after'] == {'gene': G}
    assert row['tz'] == 'America/Los_Angeles' and row['at'].startswith('2026-10-28T04:05:00-07:00')
    assert row['restart_s'] is not None and row['actor'] == 'gene-deployer' and row['exception'] is None
    assert 'private' not in (tmp_path/'production-actions.jsonl').read_text()
    assert stat.S_IMODE((tmp_path/'production-actions.jsonl').stat().st_mode) == 0o600


def test_the_log_keeps_its_newest_lines_within_its_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, 'LOG_MAX', 3000)
    path = tmp_path/'production-actions.jsonl'
    for index in range(100):
        maintenance.record(path, f'action-{index}', actor='test', trigger='test', reason='test', outcome='ok')
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert path.stat().st_size <= 3000 and rows[-1]['action'] == 'action-99'
    assert [row['action'] for row in rows] == [f'action-{i}' for i in range(100-len(rows), 100)]


def fri_root(tmp_path, record):
    root = tmp_path/'fri'
    root.mkdir()
    (root/'release.json').write_text(json.dumps(record))
    return root


@pytest.mark.parametrize('when,sha,exception,allowed', [
    ('2026-10-28T04:30:00-07:00', F, '', True),
    ('2026-10-28T05:00:00-07:00', F, '', False),
    ('2026-10-28T05:00:00-07:00', OLD_F, '', True),  # rolling back to the Fri this one replaced
    ('2026-10-28T05:00:00-07:00', F, 'owner: emergency fix, 2026-10-28', True),
])
def test_fri_releases_start_only_in_the_window(tmp_path, monkeypatch, when, sha, exception, allowed):
    clock(monkeypatch, when)
    assert fri_release.permitted(sha, fri_root(tmp_path, {'sha': 'e'*40, 'previous': OLD_F}), exception) is allowed


def test_a_refused_fri_release_changes_nothing_and_is_recorded(tmp_path, monkeypatch):
    clock(monkeypatch, '2026-10-28T05:10:00-07:00')
    root, gene_root = fri_root(tmp_path, {'sha': OLD_F, 'previous': None}), tmp_path/'gene'
    gene_root.mkdir()
    monkeypatch.setattr(sys, 'argv', ['fri_release', F, '--root', str(root), '--gene-root', str(gene_root)])
    monkeypatch.delenv('MAINTENANCE_EXCEPTION', raising=False)
    monkeypatch.setattr(fri_release, 'FriRelease', Mock(side_effect=AssertionError('no release object')))
    with pytest.raises(SystemExit, match='Outside the maintenance window'):
        fri_release.main()
    row = json.loads((gene_root/'production-actions.jsonl').read_text())
    assert row['outcome'].startswith('refused') and row['before'] == {'fri': OLD_F} == row['after']
    assert json.loads((root/'release.json').read_text())['sha'] == OLD_F


# The release script itself, run against an empty root: the gate runs before its lock, staging or
# anything else, so a refusal leaves only its log line.
def script(tmp_path, phase, when, journal=(), extra_env=None, g=G):
    root = tmp_path/'srv/gene'
    root.mkdir(parents=True, exist_ok=True)
    if journal:
        stage = root/f'staging/batch-{g}-{F}'
        stage.mkdir(parents=True, exist_ok=True)
        (stage/'journal').write_text(''.join(line+'\n' for line in journal))
    env = fake_date(tmp_path, int(at(when))) | {'BATCH_ROOT': str(root), 'BATCH_FRI_ROOT': str(tmp_path/'srv/fri')}
    env.pop('MAINTENANCE_EXCEPTION', None)
    env.update(extra_env or {})
    out = subprocess.run(['bash', str(SCRIPT), phase, g, F], capture_output=True, text=True, env=env, timeout=60)
    log = root/'production-actions.jsonl'
    rows = []
    for line in log.read_text().splitlines() if log.exists() else []:
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass  # a test's own corrupt line
    return out, root, rows


@needs_bash4
@pytest.mark.parametrize('when', ['2026-10-28T03:59:59-07:00', '2026-10-28T05:00:00-07:00',
                                  '2026-10-28T13:00:00-07:00'])
def test_baseline_outside_the_window_takes_no_lock_and_writes_no_staging(tmp_path, when):
    out, root, rows = script(tmp_path, 'baseline', when)
    assert out.returncode == 4 and 'REFUSED: outside the maintenance window' in out.stderr
    assert not (root/'batch-release.lock').exists() and not (root/'staging').exists()
    assert rows[-1]['action'] == 'batch-release baseline' and rows[-1]['outcome'].startswith('refused')
    assert rows[-1]['tz'] == 'America/Los_Angeles' and rows[-1]['at'].startswith(when[:16])


@needs_bash4
@pytest.mark.parametrize('phase,journal,gated', [
    ('status', (), True),
    ('status', ('baseline', 'drained'), False),
    ('verify-off', ('baseline', 'drained'), False),
    ('resume', ('baseline', 'drained', 'gene_recorded', 'fri_switched'), True),
    ('resume', ('baseline', 'drained', 'gene_recorded', 'fri_switched', 'evaluated'), True),
    ('resume', ('baseline', 'drained', 'gene_recorded', 'fri_switched', 'evals_ran', 'verified_off'), True),
    ('resume', ('baseline', 'drained', 'gene_recorded', 'fri_switched', 'evaluated', 'verified_off'), False),
    ('status', ('baseline', 'intent:pause'), False),
    ('verify-off', ('baseline', 'intent:pause'), False),
    ('accept-evals', ('baseline', 'drained', 'evals_ran'), False),
    ('accept-evals', ('baseline', 'drained'), True),
    ('gene', ('baseline', 'drained'), True),
    ('evals', ('baseline', 'drained', 'gene_recorded', 'fri_switched'), True),
    ('cleanup', ('baseline', 'drained', 'resumed'), True),
    ('rollback-gene', ('baseline', 'drained', 'gene_attempted'), False),
    ('rollback-fri', ('baseline', 'drained'), False),
    ('resume-old', ('baseline', 'drained'), False),
])
def test_after_the_close_only_the_restore_path_runs(tmp_path, phase, journal, gated):
    out, _, rows = script(tmp_path, phase, '2026-10-28T05:20:00-07:00', journal)
    if gated:
        assert out.returncode == 4 and 'REFUSED' in out.stderr
    else:
        assert 'REFUSED' not in out.stderr  # past the gate; this empty root fails later on its own
    if phase != 'status':
        assert rows[-1]['action'] == 'batch-release '+phase


@needs_bash4
def test_a_refusal_names_the_batchs_safe_path(tmp_path):
    out, _, _ = script(tmp_path, 'gene', '2026-10-28T05:20:00-07:00', ('baseline', 'drained'))
    assert 'Run resume-old.' in out.stderr
    out, _, _ = script(tmp_path, 'evals', '2026-10-28T05:20:00-07:00', ('baseline', 'drained', 'intent:fri'))
    assert 'Run rollback-fri, then resume-old.' in out.stderr


@needs_bash4
def test_an_owner_exception_is_let_through_and_recorded(tmp_path):
    out, _, rows = script(tmp_path, 'baseline', '2026-10-28T13:00:00-07:00',
                          extra_env={'MAINTENANCE_EXCEPTION': 'owner, 2026-10-28: emergency fix'})
    assert 'REFUSED' not in out.stderr and 'maintenance exception: owner, 2026-10-28' in out.stdout
    assert rows[-1]['exception'] == 'owner, 2026-10-28: emergency fix'


@needs_bash4
def test_one_batch_drains_per_local_day(tmp_path):
    root = tmp_path/'srv/gene'
    other = root/f'staging/batch-{"e"*40}-{F}'
    other.mkdir(parents=True)
    (other/'journal').write_text('baseline\ndrained\nresumed\n')
    (root/'production-actions.jsonl').write_text('not json\n' + json.dumps(
        {'action': 'batch-release drain', 'outcome': 'ok', 'stage': other.name, 'local_date': '2026-10-28'})+'\n')
    out, _, _ = script(tmp_path, 'baseline', '2026-10-28T04:40:00-07:00')
    assert out.returncode == 4 and 'already drained today' in out.stderr
    out, _, _ = script(tmp_path, 'baseline', '2026-10-29T04:05:00-07:00')  # the next day's window
    assert 'REFUSED' not in out.stderr


def deployed(when, kind='feature', job='a1b2c3d4e5f6', outcome='ok'):
    """gene.deploy's line in the production-action log."""
    return json.dumps({'action': f'deploy {kind} job {job}', 'at': when, 'actor': 'gene-deployer',
                       'outcome': outcome})+'\n'


@needs_bash4
def test_the_daily_cap_counts_an_automatic_deployment(tmp_path):
    root = tmp_path/'srv/gene'
    root.mkdir(parents=True)
    log = root/'production-actions.jsonl'
    log.write_text(deployed('2026-10-28T04:02:00-07:00', kind='rollback')  # restoring service never counts
                   + deployed('2026-10-27T04:10:00.250000-07:00'))  # yesterday's
    out, _, _ = script(tmp_path, 'baseline', '2026-10-28T04:40:00-07:00')
    assert 'REFUSED' not in out.stderr
    with log.open('a') as lines:  # failed, and logged in UTC by a deployer without the zone: still today's
        lines.write(deployed('2026-10-28T11:05:00+00:00', kind='repair', outcome='failed'))
    out, root, rows = script(tmp_path, 'stage', '2026-10-28T04:40:00-07:00')
    assert out.returncode == 4 and 'an automatic deployment ran today: deploy repair job a1b2c3d4e5f6' in out.stderr
    assert "CRITICAL_FIX='<reason>'" in out.stderr
    assert rows[-1]['action'] == 'batch-release stage' and rows[-1]['outcome'].startswith('refused')
    out, _, _ = script(tmp_path, 'baseline', '2026-10-29T04:05:00-07:00')  # the next day's window
    assert 'REFUSED' not in out.stderr


@needs_bash4
def test_a_critical_fix_lifts_only_the_daily_cap_and_is_logged(tmp_path):
    root = tmp_path/'srv/gene'
    other = root/f'staging/batch-{"e"*40}-{F}'
    other.mkdir(parents=True)
    (other/'journal').write_text('baseline\ndrained\nresumed\n')
    (root/'production-actions.jsonl').write_text(json.dumps(
        {'action': 'batch-release drain', 'outcome': 'ok', 'stage': other.name, 'local_date': '2026-10-28'})+'\n')
    out, _, rows = script(tmp_path, 'baseline', '2026-10-28T04:40:00-07:00', extra_env={'CRITICAL_FIX': '  '})
    assert out.returncode == 4 and 'a batch already drained today' in out.stderr  # a blank reason is none
    out, _, rows = script(tmp_path, 'baseline', '2026-10-28T04:40:00-07:00',
                          extra_env={'CRITICAL_FIX': 'P0: replies leak DMs'})
    assert 'REFUSED' not in out.stderr and 'critical fix: P0: replies leak DMs (a batch already drained' in out.stdout
    assert rows[-1]['action'] == 'batch-release baseline'
    assert rows[-1]['exception'] == 'critical fix: P0: replies leak DMs'
    out, _, rows = script(tmp_path, 'baseline', '2026-10-28T13:00:00-07:00',
                          extra_env={'CRITICAL_FIX': 'P0: replies leak DMs'})
    assert out.returncode == 4 and 'outside the maintenance window' in out.stderr  # never the window
    out, _, rows = script(tmp_path, 'baseline', '2026-10-28T13:00:00-07:00',
                          extra_env={'CRITICAL_FIX': 'P0: replies leak DMs', 'MAINTENANCE_EXCEPTION': 'owner, today'})
    assert 'REFUSED' not in out.stderr
    assert rows[-1]['exception'] == 'owner, today; critical fix: P0: replies leak DMs'


@needs_bash4
def test_unreadable_history_counts_as_a_used_batch(tmp_path):
    root = tmp_path/'srv/gene'
    other = root/f'staging/batch-{"e"*40}-{F}'
    other.mkdir(parents=True)
    (other/'journal').write_text('baseline\n')
    (other/'journal').chmod(0)
    try:
        out, _, _ = script(tmp_path, 'baseline', '2026-10-28T04:40:00-07:00')
    finally:
        (other/'journal').chmod(0o600)
    if os.geteuid() != 0:  # root reads it anyway
        assert out.returncode == 4 and "today's batches could not be read" in out.stderr


@needs_bash4
def test_a_batch_drained_before_the_log_existed_counts_by_its_journal(tmp_path):
    root = tmp_path/'srv/gene'
    legacy = root/f'staging/batch-{"e"*40}-{F}'
    legacy.mkdir(parents=True)
    (legacy/'journal').write_text('baseline\ndrained\nresumed\n')
    stamp = at('2026-10-28T03:30:00-07:00')
    os.utime(legacy/'journal', (stamp, stamp))
    out, _, _ = script(tmp_path, 'baseline', '2026-10-28T04:10:00-07:00')
    assert out.returncode == 4 and 'already drained today' in out.stderr
    stamp = at('2026-10-27T03:30:00-07:00')
    os.utime(legacy/'journal', (stamp, stamp))
    out, _, _ = script(tmp_path, 'baseline', '2026-10-28T04:10:00-07:00')
    assert 'REFUSED' not in out.stderr


@needs_bash4
def test_a_batch_that_resumed_the_old_release_does_not_go_forward_again(tmp_path):
    out, _, _ = script(tmp_path, 'gene', '2026-10-29T04:10:00-07:00', ('baseline', 'drained', 'resumed_old'))
    assert out.returncode == 4 and 'already resumed the old release' in out.stderr


@needs_bash4
def test_the_scripts_log_stays_within_its_bound(tmp_path):
    root = tmp_path/'srv/gene'
    root.mkdir(parents=True)
    filler = json.dumps({'action': 'old', 'pad': 'x'*200})+'\n'
    (root/'production-actions.jsonl').write_text(filler * (((1 << 20) // len(filler)) + 50))
    script(tmp_path, 'baseline', '2026-10-28T13:00:00-07:00')
    log = root/'production-actions.jsonl'
    assert log.stat().st_size <= 1 << 20 and stat.S_IMODE(log.stat().st_mode) == 0o600
    assert json.loads(log.read_text().splitlines()[-1])['action'] == 'batch-release baseline'


@needs_bash4
def test_an_unreadable_clock_never_blocks_recovery(tmp_path):
    bin_dir = tmp_path/'bin'
    bin_dir.mkdir()
    (bin_dir/'date').write_text('#!/usr/bin/env bash\nexit 1\n')
    (bin_dir/'date').chmod(0o755)
    root = tmp_path/'srv/gene'
    root.mkdir(parents=True)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'BATCH_ROOT': str(root),
           'BATCH_FRI_ROOT': str(tmp_path/'srv/fri')}
    env.pop('MAINTENANCE_EXCEPTION', None)
    out = subprocess.run(['bash', str(SCRIPT), 'resume-old', G, F], capture_output=True, text=True, env=env, timeout=60)
    assert 'REFUSED' not in out.stderr  # past the gate; the empty root then stops it on its own
    rows = [json.loads(line) for line in (root/'production-actions.jsonl').read_text().splitlines()]
    assert rows[-1]['action'] == 'batch-release resume-old' and rows[-1]['duration_s'] is None
    out = subprocess.run(['bash', str(SCRIPT), 'baseline', G, F], capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 4 and not (root/'staging').exists()


@needs_bash4
def test_the_eval_rules_decision_is_recorded_with_its_reason(tmp_path):
    journal = ('baseline', 'drained', 'gene_recorded', 'fri_switched', 'evals_ran')
    out, root, _ = script(tmp_path, 'accept-evals', '2026-10-28T05:20:00-07:00', journal)
    assert out.returncode == 1 and 'set EVALS_ACCEPTED' in out.stderr
    out, root, rows = script(tmp_path, 'accept-evals', '2026-10-28T05:20:00-07:00', journal,
                             {'EVALS_ACCEPTED': 'known sass miss; rerun passed'})
    assert out.returncode == 0 and 'evals accepted: known sass miss' in out.stdout
    assert (root/f'staging/batch-{G}-{F}/journal').read_text().splitlines()[-1] == 'evaluated'


def test_the_deployer_takes_back_a_feature_whose_preparation_ran_past_the_close(tmp_path, monkeypatch):
    times = iter([at('2026-10-28T04:49:00-07:00'), at('2026-10-28T05:00:30-07:00')])
    monkeypatch.setattr(maintenance, 'clock', lambda: next(times))
    worker = deployer(tmp_path)
    worker.prepare = Mock(side_effect=lambda job: (tmp_path/'releases'/G/'cogs').mkdir(parents=True))
    with pytest.raises(RuntimeError, match='closed before the switch; nothing was changed'):
        worker.process({'id': 'job', 'kind': 'feature', 'proposal': {'sha': G, 'base_sha': OLD_G}})
    worker.compose.assert_not_called()
    assert not (tmp_path/'releases'/G).exists()


def test_fri_release_rechecks_the_window_right_before_its_switch(tmp_path):
    from tests.test_fri_split import installation
    release, store, calls = installation(tmp_path)
    release.permitted = lambda: False
    with pytest.raises(RuntimeError, match='closed before the Fri switch'):
        release.deploy('c'*40)
    assert not [command for command in calls if command[:2] == ['docker', 'compose']]
    assert json.loads(release.record.read_text())['sha'] == 'b'*40


@needs_bash4
@pytest.mark.parametrize('journal, gated', [
    (('baseline', 'drained', 'evals_started', 'evals_ran', 'evaluated', 'verified_off', 'evals_started', 'evals_ran'), True),
    (('baseline', 'drained', 'evals_started', 'evals_ran', 'evaluated', 'verified_off', 'evals_started', 'evals_ran',
      'evaluated'), False),
    (('baseline', 'drained', 'evals_started', 'evals_ran', 'verified_off', 'evals_started'), True),
])
def test_only_the_last_evals_runs_result_admits_resume_after_the_close(tmp_path, journal, gated):
    out, _, _ = script(tmp_path, 'resume', '2026-10-28T05:20:00-07:00', journal)
    assert ('REFUSED' in out.stderr) is gated


@needs_bash4
def test_accepting_needs_the_last_evals_run_finished(tmp_path):
    journal = ('baseline', 'drained', 'evals_started', 'evals_ran', 'evals_started')
    out, _, _ = script(tmp_path, 'accept-evals', '2026-10-28T05:20:00-07:00', journal, {'EVALS_ACCEPTED': 'why'})
    assert out.returncode == 4
