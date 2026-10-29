import datetime as dt
import json
import os
import re
import sqlite3
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


# Owner policy, 2026-10-28: [03:30, 07:30) America/Los_Angeles.
BOUNDARIES = [
    ('2026-10-28T03:29:59-07:00', False),
    ('2026-10-28T03:30:00-07:00', True),
    ('2026-10-28T07:29:59-07:00', True),
    ('2026-10-28T07:30:00-07:00', False),
    ('2026-10-28T16:30:00-07:00', False),  # an overdue wake is no daytime catch-up
    # Fall back (2026-11-01 02:00 PDT -> 01:00 PST): the window opens at 11:30Z, not 10:30Z.
    ('2026-11-01T10:30:00+00:00', False),
    ('2026-11-01T11:29:59+00:00', False),
    ('2026-11-01T03:30:00-08:00', True),
    ('2026-11-01T07:29:59-08:00', True),
    ('2026-11-01T07:30:00-08:00', False),
    # Spring forward (2027-03-14 02:00 PST -> 03:00 PDT): the window opens at 10:30Z, not 11:30Z.
    ('2027-03-13T03:30:00-08:00', True),
    ('2027-03-14T10:29:59+00:00', False),
    ('2027-03-14T10:30:00+00:00', True),
    ('2027-03-14T14:29:59+00:00', True),
    ('2027-03-14T14:30:00+00:00', False),
]


def bash_major():
    try:
        out = subprocess.run(['bash', '-c', 'echo ${BASH_VERSINFO[0]}'], capture_output=True, text=True, check=True)
        return int(out.stdout)
    except (OSError, ValueError, subprocess.CalledProcessError):
        return 0


needs_bash4 = pytest.mark.skipif(bash_major() < 4, reason='batch-release.sh needs bash 4 or later')


def window_settings():
    """The release script's own copy of the window: the lines that set it, which window() reads."""
    return '\n'.join(line for line in SCRIPT.read_text().splitlines() if re.match(r'WINDOW(_OPENS|_CLOSES)?=', line))


def functions(*names):
    """The named shell functions from batch-release.sh, to run on their own."""
    out, keep, heredoc = [], False, None
    for line in SCRIPT.read_text().splitlines():
        starts = any(line.startswith(name+'() {') for name in names)
        if starts and line.split('  #')[0].rstrip().endswith('}'):
            out.append(line)  # a one-line function
            continue
        keep = keep or starts
        if keep:
            out.append(line)
            if heredoc:  # A heredoc's own lines, a Python dict's closing brace included, never end the function.
                heredoc = None if line == heredoc else heredoc
            elif match := re.search(r"<<'(\w+)'", line):
                heredoc = match.group(1)
            else:
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
    out = subprocess.run(['bash', '-c', window_settings() + '\n' + functions('window') + f'\nwindow {int(at(when))}'],
                         capture_output=True, text=True, check=True)
    assert out.stdout.split()[0] == ('open' if inside else 'closed')


def test_every_copy_of_the_window_is_the_same():
    """The release script and image retention run without this package, so each keeps its own copy."""
    from scripts import gene_image_retention as retention
    assert (maintenance.OPENS, maintenance.CLOSES) == (dt.time(3, 30), dt.time(7, 30))  # owner policy, 2026-10-28
    assert maintenance.WINDOW == '[03:30, 07:30) America/Los_Angeles'
    settings = dict(line.split('  #')[0].split('=', 1) for line in window_settings().splitlines())
    assert settings['WINDOW_OPENS'] == f'{maintenance.OPENS:%H:%M}'
    assert settings['WINDOW_CLOSES'] == f'{maintenance.CLOSES:%H:%M}'
    assert (retention.OPENS, retention.CLOSES, retention.ZONE) == (maintenance.OPENS, maintenance.CLOSES, maintenance.ZONE)
    assert retention.WINDOW == maintenance.WINDOW


def test_the_docs_and_messages_name_the_window_the_code_keeps():
    """Every [hh:mm, hh:mm) in the docs, scripts and code is the window or the deployer's claim window."""
    root = SCRIPT.parents[2]
    window = f'[{maintenance.OPENS:%H:%M}, {maintenance.CLOSES:%H:%M})'
    claims = f'[{maintenance.OPENS:%H:%M}, {maintenance.closes(maintenance.CLAIM_MARGIN):%H:%M})'
    assert claims == '[03:30, 07:20)'
    found = {}
    for path in [root/'AGENTS.md', root/'README.md', *(root/'docs').rglob('*.md'), *(root/'ops').rglob('*'),
                 *(root/'src').rglob('*.py'), *(root/'scripts').rglob('*.py'), *(root/'tests').rglob('*.py')]:
        if path.is_file():
            for text in re.findall(r'\[\d\d:\d\d, \d\d:\d\d\)', path.read_text(errors='replace')):
                found.setdefault(text, set()).add(str(path.relative_to(root)))
    assert set(found) <= {window, claims}, found
    assert 'ops/maintainer/batch-release.md' in found[claims]


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
    out = subprocess.run(['bash', '-c', window_settings() + '\n' + functions('window') + '\nwindow 1000000000'],
                         capture_output=True, text=True)
    assert out.returncode != 0
    out = subprocess.run(['bash', '-c', window_settings() + '\n' + functions('now_epoch', 'window', 'window_open') + '\nwindow_open'],
                         capture_output=True, text=True, env=fake_date(tmp_path, 'not-a-number'))
    assert out.returncode != 0


# A running controller's SQLite creates and removes the DB's journal files at any moment. On 2026-10-28
# one vanished between repair_sidecars' look and its stat, and the chown that followed failed the Gene switch.
FAKE_STAT = '''#!/usr/bin/env bash
for file; do :; done  # stat -c %u:%g FILE; owners come from $CTL/<name>.owner
if [[ $file == "$(cat "$CTL/vanish-at-stat" 2>/dev/null)" ]]; then rm -f "$file"; fi
if [[ $file == "$(cat "$CTL/stat-eio-hidden" 2>/dev/null)" ]]; then  # An I/O error the shell's -e fails on too.
  echo "stat: cannot statx '$file': Input/output error" >&2; exit 1
fi
[[ -e $file ]] || { echo "stat: cannot statx '$file': No such file or directory" >&2; exit 1; }
if [[ $file == "$(cat "$CTL/stat-eio" 2>/dev/null)" ]]; then echo "stat: cannot statx '$file': Input/output error" >&2; exit 1; fi
if [[ $file == "$(cat "$CTL/reappear-at-stat" 2>/dev/null)" ]]; then  # Gone at stat, a new one there after.
  echo "stat: cannot statx '$file': No such file or directory" >&2; exit 1
fi
cat "$CTL/${file##*/}.owner" 2>/dev/null || echo 10001:10001
'''
FAKE_CHOWN = '''#!/usr/bin/env bash
echo "$1 ${2##*/}" >> "$CTL/chowned"
if [[ $2 == "$(cat "$CTL/vanish-at-chown" 2>/dev/null)" ]]; then rm -f "$2"; fi
[[ -e $2 ]] || { echo "chown: cannot dereference '$2': No such file or directory" >&2; exit 1; }
[[ ! -e $CTL/chown-denied ]] || { echo "chown: changing ownership of '$2': Operation not permitted" >&2; exit 1; }
echo "$1" > "$CTL/${2##*/}.owner"
'''


def sidecar_shell(tmp_path, wal_owner, vanish=None, denied=False, eio=False, reappear=False, eio_hidden=False):
    """repair_sidecars on a root-owned or owner-owned WAL that SQLite removes at the chosen moment."""
    ctl, data, bin_dir = tmp_path/'ctl', tmp_path/'data', tmp_path/'bin'
    for path in ctl, data, bin_dir:
        path.mkdir()
    db = data/'gene.sqlite3'
    db.touch()
    if eio_hidden:  # The file is there, but every lookup fails with an I/O error: -e and -L see nothing either.
        (ctl/'stat-eio-hidden').write_text(f'{db}-wal')
    else:
        (data/'gene.sqlite3-wal').touch()
    (ctl/'gene.sqlite3-wal.owner').write_text(wal_owner+'\n')
    if vanish:
        (ctl/f'vanish-at-{vanish}').write_text(f'{db}-wal')
    if denied:
        (ctl/'chown-denied').touch()
    if eio:
        (ctl/'stat-eio').write_text(f'{db}-wal')
    if reappear:
        (ctl/'reappear-at-stat').write_text(f'{db}-wal')
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


def test_a_journal_file_that_cant_be_read_but_is_there_stops_the_script(tmp_path):
    """Review: only a file that's gone counts as gone; an I/O error on one still there isn't skipped."""
    out, chowned, wal = sidecar_shell(tmp_path, '0:0', eio=True)
    assert out.returncode != 0 and 'repaired' not in out.stdout
    assert 'FAIL: could not read the owner of gene.sqlite3-wal' in out.stderr
    assert chowned == [] and wal.exists()


def test_an_io_error_that_hides_a_journal_file_from_the_shell_too_stops_the_script(tmp_path):
    """gene #101: a persistent I/O error fails stat and the shell's -e and -L checks alike. Only stat's own
    'no such file' counts as gone, so the file isn't skipped as if it had vanished."""
    out, chowned, _ = sidecar_shell(tmp_path, '0:0', eio_hidden=True)
    assert out.returncode != 0 and 'repaired' not in out.stdout
    assert 'FAIL: could not read the owner of gene.sqlite3-wal' in out.stderr and chowned == []


def test_a_journal_file_gone_at_stat_and_back_after_needs_nothing(tmp_path):
    """2026-10-28, second release: SQLite removed -shm at stat and made a new one before a recheck. Gone at
    stat is gone: the new one is the controller's own, so the repair skips it and the switch goes on."""
    out, chowned, wal = sidecar_shell(tmp_path, '0:0', reappear=True)
    assert out.returncode == 0 and out.stdout.split() == ['repaired'], out.stderr
    assert chowned == [] and wal.exists()


@pytest.mark.parametrize('error', ["du: cannot read directory '{root}/sub': Permission denied",
                                   "du: fts_read failed: Input/output error"])
def test_sizing_a_tree_stops_on_any_error_but_a_file_gone(tmp_path, error):
    """Review: a partial total after a permission or I/O error would undercount; it stops instead."""
    bin_dir = tmp_path/'bin'
    bin_dir.mkdir()
    message = error.format(root=tmp_path).replace("'", "'\\''")
    (bin_dir/'du').write_text('#!/usr/bin/env bash\n'
                              'printf "%s\\t%s\\n" 4096 "$2"\n'
                              f"echo '{message}' >&2\n"
                              'exit 1\n')
    (bin_dir/'du').chmod(0o755)
    shell = ('set -Eeuo pipefail\nSWITCHING=0\n' + functions('log', 'die', 'du_bytes')
             + f'\nsize=$(du_bytes {tmp_path})\necho "size=$size"\n')
    out = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30,
                         env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'})
    assert out.returncode != 0 and 'size=' not in out.stdout
    assert 'FAIL: could not measure' in out.stderr


@pytest.mark.parametrize('printed,stops,back', [('4096', False, False), ('', True, False), ('4096', False, True)])
def test_sizing_a_tree_counts_a_file_that_vanishes_as_gone(tmp_path, printed, stops, back):
    bin_dir = tmp_path/'bin'
    bin_dir.mkdir()
    if back:  # A new one there by the time anyone looks again: still counted as gone.
        (tmp_path/'gene.sqlite3-wal').touch()
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


@needs_bash4
@pytest.mark.parametrize('switching', ['0', '1'])
@pytest.mark.parametrize('failing', ['data/controller', 'data/red', 'source'])  # first, middle and last in the sum
def test_a_size_that_cant_be_measured_stops_the_switchs_disk_check(tmp_path, switching, failing):
    """gene #100: a measurement that fails inside an arithmetic sum used to drop out of it (or, mid-switch, stop only
    its own subshell), so disk admission went on without it. It stops the switch, and mid-switch rolls back."""
    bin_dir, root = tmp_path/'bin', tmp_path/'srv'
    bin_dir.mkdir()
    (bin_dir/'du').write_text('#!/usr/bin/env bash\nfor path; do :; done\n'
                              f'if [[ $path == {root}/{failing} ]]; then printf "4096\\t%s\\n" "$path"\n'
                              '  echo "du: cannot read directory \'$path/x\': Input/output error" >&2; exit 1; fi\n'
                              'printf "1000\\t%s\\n" "$path"\n')
    (bin_dir/'du').chmod(0o755)
    shell = (f'set -Eeuo pipefail\nROOT={root} STAGE={root}/staging SWITCHING={switching}\n'
             + functions('log', 'die', 'du_bytes', 'measure', 'gene_disk', 'on_gene_error')
             + '\nrollback_gene() { if [[ $BASHPID == "$$" ]]; then echo rolled-back; fi; }\n'
             'disk_admission() { echo "admitted $*"; }\n'
             + ('trap on_gene_error ERR\n' if switching == '1' else '') + 'gene_disk\necho switching-on\n')
    out = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30,
                         env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'})
    assert out.returncode != 0 and 'admitted' not in out.stdout and 'switching-on' not in out.stdout, out.stdout
    assert f'FAIL: could not measure {root}/{failing}' in out.stderr
    assert ('rolled-back' in out.stdout.split()) is (switching == '1')  # the main shell's rollback, not a subshell's


def test_sizing_a_tree_stops_on_an_io_error_for_a_path_du_cant_reach(tmp_path):
    """gene #101: an I/O error on a path is never read as that path being gone, whatever the shell sees there."""
    bin_dir = tmp_path/'bin'
    bin_dir.mkdir()
    (bin_dir/'du').write_text('#!/usr/bin/env bash\nprintf "%s\\t%s\\n" 4096 "$2"\n'
                              'echo "du: cannot access \'$2/gene.sqlite3-wal\': Input/output error" >&2\nexit 1\n')
    (bin_dir/'du').chmod(0o755)
    shell = ('set -Eeuo pipefail\nSWITCHING=0\n' + functions('log', 'die', 'du_bytes')
             + f'\nsize=$(du_bytes {tmp_path})\necho "size=$size"\n')
    out = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30,
                         env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'})
    assert out.returncode != 0 and 'size=' not in out.stdout
    assert 'FAIL: could not measure' in out.stderr and 'Input/output error' in out.stderr


@pytest.mark.parametrize('journal,mode', [
    (['baseline', 'drained'], 'checked'),
    (['baseline', 'intent:pause', 'intent:abort'], 'replays'),  # the drain's stop, its restore unfinished
    (['baseline', 'intent:pause', 'intent:abort', 'resumed_old'], 'as-is'),  # gene #95: and once restored
    # gene #93: rollback-gene's stop after its fence, restored, and then not
    (['baseline', 'drained', 'resumed', 'intent:rollback', 'intent:abort', 'resumed_old'], 'as-is'),
    (['baseline', 'drained', 'resumed', 'intent:rollback', 'intent:abort'], 'replays'),
    # A rollback that ran after such a stop: what it restored is checked again.
    (['baseline', 'drained', 'intent:abort', 'resumed_old', 'intent:rollback', 'fri_rolled_back'], 'checked'),
    # A rollback's restore steps before its stop are older than the stop, and stay as it is.
    (['baseline', 'drained', 'resumed', 'intent:rollback', 'intent:restore', 'intent:fri_restore', 'fri_restored',
      'intent:rollback', 'intent:pause', 'intent:abort', 'resumed_old'], 'as-is'),
    # A later rollback that only fenced, killed before it restored anything: still as it is.
    (['baseline', 'drained', 'intent:abort', 'resumed_old', 'intent:pause', 'paused:7'], 'as-is'),
    # gene #121 round 1: journals from the script before it, which journaled intent:rollback once per batch. A
    # rollback-fri after the stop was killed once it stopped Fri: its restore steps, not a new intent:rollback,
    # say a rollback ran, so the old pair is checked again rather than resumed as it is with Fri stopped.
    (['baseline', 'drained', 'fri_backup', 'gene_recorded', 'intent:fri', 'fri_switched', 'resumed', 'intent:pause',
      'intent:rollback', 'intent:fri_restore', 'fri_restored', 'fri_rolled_back', 'intent:services_old', 'done:unpause',
      'resumed_old', 'intent:pause', 'intent:abort', 'done:unpause', 'resumed_old', 'intent:pause',
      'intent:fri_restore'], 'checked'),
    (['baseline', 'drained', 'intent:rollback', 'intent:abort', 'resumed_old', 'intent:fri_restore', 'fri_restored'],
     'checked'),
    (['baseline', 'drained', 'intent:rollback', 'intent:abort', 'resumed_old', 'done:downgrade'], 'checked'),
    (['baseline', 'drained', 'intent:rollback', 'intent:abort', 'resumed_old', 'gene_rolled_back'], 'checked'),
])
def test_resume_old_stays_as_it_is_after_a_stop_until_a_rollback_runs(tmp_path, journal, mode):
    (tmp_path/'journal').write_text(''.join(line+'\n' for line in journal))
    shell = (f'set -Eeuo pipefail\nJOURNAL={tmp_path}/journal\n' + functions('has', 'aborting', 'as_is')
             + '\nif aborting; then echo replays; elif as_is; then echo as-is; else echo checked; fi\n')
    out = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.split() == [mode], out.stderr


@pytest.mark.parametrize('journal,fenced', [
    (['baseline', 'drained', 'resumed'], False),  # no rollback has started since development resumed
    (['baseline', 'drained', 'resumed', 'intent:rollback', 'intent:pause', 'paused:7'], True),  # its fence held
    (['baseline', 'drained', 'resumed', 'intent:rollback', 'intent:pause', 'paused:7', 'intent:restore'], False),
    (['baseline', 'drained', 'resumed', 'intent:rollback', 'intent:fri_restore'], False),
    (['baseline', 'drained', 'resumed', 'intent:rollback', 'intent:abort', 'resumed_old'], False),  # resumed as it is
    (['baseline', 'drained', 'gene_attempted', 'intent:rollback', 'intent:pause'], False),  # never resumed: mid-switch
    # A second rollback after the first one finished and development resumed at the old release.
    (['baseline', 'drained', 'resumed', 'intent:rollback', 'intent:restore', 'gene_rolled_back', 'resumed_old',
      'intent:rollback', 'intent:pause', 'paused:9'], True),
])
def test_a_rollback_that_only_fenced_is_known_from_the_journal(tmp_path, journal, fenced):
    (tmp_path/'journal').write_text(''.join(line+'\n' for line in journal))
    shell = (f'set -Eeuo pipefail\nJOURNAL={tmp_path}/journal\n' + functions('fenced_by_rollback')
             + '\nif fenced_by_rollback; then echo fenced; else echo not; fi\n')
    out = subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0 and out.stdout.split() == ['fenced' if fenced else 'not'], out.stderr


H = 'e' * 40


def pair_shell(tmp_path, changes):
    """pair_check on a batch that switched to G and resumed, after the changes a rollback's fence waited out."""
    root, fri_root, backup = tmp_path/'srv', tmp_path/'fri', tmp_path/'backup'
    for path in root/'secrets', fri_root, backup/'db', backup/'installed-source':
        path.mkdir(parents=True)
    (backup/'db/controller.sqlite3').touch()
    (backup/'release.env').write_text(f'GENE_RELEASE_SHA={OLD_G}\nGENE_ROOT=/srv/gene\nGENE_RUNTIME_IMAGE=gene-runtime:{OLD_G}\n')
    (backup/'worker.env').write_text(f'GENE_WORKER_IMAGE=gene-worker:{OLD_G}\n')
    (backup/'fri-release.env').write_text(f'FRI_RELEASE_SHA={OLD_F}\nFRI_RUNTIME_IMAGE=fri-runtime:{OLD_F}\n')
    (backup/'fri-release.json').write_text(json.dumps({'sha': OLD_F}))
    release = f'GENE_RELEASE_SHA={G}\nGENE_ROOT={root}\nGENE_RUNTIME_IMAGE=gene-runtime:{G}\n'
    settings = {'active_sha': G, 'previous_sha': OLD_G, 'fri_active_sha': F}
    record, runtime, deployment = {'sha': F, 'previous': OLD_F}, 'new-runtime', None
    if 'deployed' in changes:  # As the deployer and the controller leave a feature deployment of H from G.
        release = release.replace(G, H, 1)
        settings |= {'active_sha': H, 'previous_sha': G}
        (root/f'releases/{H}/cogs/gene').mkdir(parents=True)
        (root/f'releases/{H}/cogs/gene/__init__.py').touch()
        deployment = {'sha': H, 'previous_sha': G, 'state': 'deployed'}
    if 'runtime image' in changes:
        release = release.replace(f'gene-runtime:{G}', f'gene-runtime:{OLD_G}')
    if 'extra line' in changes:
        release += 'GENE_EXTRA=1\n'
    if 'settings unchanged' in changes:
        settings |= {'active_sha': G, 'previous_sha': OLD_G}
    if 'no cogs' in changes:
        (root/f'releases/{H}/cogs/gene/__init__.py').unlink()
    if 'deployment failed' in changes:
        deployment |= {'state': 'failed'}
    if 'no deployment' in changes:
        deployment = None
    if 'red on the old image' in changes:
        runtime = 'old-runtime'
    if 'fri record' in changes:
        record = {'sha': 'f' * 40, 'previous': F}
    (root/'release.env').write_text(release)
    (root/'secrets/worker.env').write_text(f'GENE_WORKER_IMAGE=gene-worker:{G}\n')
    (fri_root/'release.env').write_text(f'FRI_RELEASE_SHA={F}\nFRI_RUNTIME_IMAGE=fri-runtime:{F}\n')
    (fri_root/'release.json').write_text(json.dumps(record))
    (tmp_path/'state.json').write_text(json.dumps({
        'settings': settings, 'busy_jobs': 0, 'source': G, 'deployment': deployment,
        'controller': {'health': 'healthy', 'http': '200', 'image': 'new-runtime'},
        'red': {'image': runtime, 'running': True}, 'fri_container': {'image': 'new-fri', 'running': True}}))
    (tmp_path/'manifest.json').write_text(json.dumps({
        'old': {'runtime': {'tag': f'gene-runtime:{OLD_G}', 'id': 'old-runtime'}, 'fri': {'id': 'old-fri'}},
        'new': {'runtime': {'tag': f'gene-runtime:{G}', 'id': 'new-runtime'}, 'fri': {'id': 'new-fri'}}}))
    (tmp_path/'baseline.json').write_text(json.dumps({'settings': {'previous_sha': '1' * 40}, 'source': 'unmarked'}))
    shell = (functions('pair_check') + f'\nstate() {{ cat {tmp_path}/state.json; }}\nbackup_dir() {{ echo {backup}; }}\n'
             f'MANIFEST={tmp_path}/manifest.json BASELINE={tmp_path}/baseline.json ROOT={root} FRI_ROOT={fri_root}\n'
             f'G={G} F={F} OLD_G={OLD_G} OLD_F={OLD_F}\npair_check\n')
    return subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize('changes,rc', [
    ((), 0),
    (('deployed',), 3),  # a deployment the fence waited out moved Gene: the rollback may resume as it is
    # gene #121 round 1: any change no deployment explains keeps the fence (exit 1), release.env included.
    (('runtime image',), 1),  # the same Gene under another runtime image
    (('deployed', 'fri record'), 1),  # a deployment, and a Fri record from outside the pair
    (('deployed', 'runtime image'), 1),  # a deployment inherits the runtime image; it never picks one
    (('deployed', 'extra line'), 1),
    (('deployed', 'settings unchanged'), 1),  # release.env moved, but the controller recorded no deployment
    (('deployed', 'no cogs'), 1),  # nothing installed the release's cogs
    (('deployed', 'deployment failed'), 1),
    (('deployed', 'no deployment'), 1),
    (('deployed', 'red on the old image'), 1),
])
def test_only_a_deployment_explains_gene_moving_during_the_fence(tmp_path, changes, rc):
    out = pair_shell(tmp_path, changes)
    assert out.returncode == rc, out.stderr
    assert ('outside the recorded old/new pair' in out.stderr) is (rc != 0), out.stderr


def test_claims_wait_for_the_window_and_an_owner_rollback_does_not(store, monkeypatch):
    job = proposed(store)
    store.approve(job['id'], G, 'owner')
    for when in ('2026-10-28T03:29:59-07:00', '2026-10-28T07:20:00-07:00', '2026-10-28T07:50:00-07:00',
                 '2026-10-28T16:00:00-07:00'):
        clock(monkeypatch, when)
        assert store.claim_deploy() is None
    assert store.get(job['id'])['state'] == 'approved'  # deferred, untouched
    clock(monkeypatch, '2026-10-29T07:19:59-07:00')  # the last claim leaves time for the switch
    release = store.claim_deploy()
    assert release['id'] == job['id']
    store.finish_deploy(release['id'], release['lease'], True, 'Healthy')
    clock(monkeypatch, '2026-10-29T13:00:00-07:00')
    rollback = store.rollback('guild', 'owner')
    assert store.claim_deploy()['id'] == rollback['id']


CLAIM_EDGES = [
    ('2026-10-28T03:29:59-07:00', False),
    ('2026-10-28T03:30:00-07:00', True),
    ('2026-10-28T07:19:59-07:00', True),
    ('2026-10-28T07:20:00-07:00', False),  # ten minutes before the close, so a claimed switch starts in time
    # The days daylight saving ends and starts: the same local edges.
    ('2026-11-01T11:29:59+00:00', False),
    ('2026-11-01T03:30:00-08:00', True),
    ('2026-11-01T07:19:59-08:00', True),
    ('2026-11-01T07:20:00-08:00', False),
    ('2027-03-14T10:29:59+00:00', False),
    ('2027-03-14T10:30:00+00:00', True),
    ('2027-03-14T14:19:59+00:00', True),
    ('2027-03-14T14:20:00+00:00', False),
]


@pytest.mark.parametrize('when,claimed', CLAIM_EDGES)
def test_automatic_claims_run_from_the_open_until_ten_minutes_before_the_close(store, monkeypatch, when, claimed):
    job = approved(store, G, 'message-1')
    clocks(monkeypatch, when)
    result = store.claim_deploy()
    assert (result and result['id']) == (job['id'] if claimed else None)
    assert store.get(job['id'])['state'] == ('deploying' if claimed else 'approved')


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
    clocks(monkeypatch, '2026-10-28T07:50:00-07:00')
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
    clock(monkeypatch, '2026-10-28T07:30:00-07:00')
    worker = deployer(tmp_path)
    job = {'id': 'job', 'kind': 'feature', 'proposal': {'sha': G, 'base_sha': OLD_G}}
    with pytest.raises(RuntimeError, match='Outside the maintenance window'):
        worker.process(job)
    worker.prepare.assert_not_called()
    worker.compose.assert_not_called()
    worker.process(job | {'kind': 'rollback'})  # restoring service runs at any hour
    worker.compose.assert_called_once_with('stop', 'red')


@pytest.mark.parametrize('when,switches', [
    ('2026-10-28T03:29:59-07:00', False),
    ('2026-10-28T03:30:00-07:00', True),
    ('2026-10-28T07:29:59-07:00', True),  # a job claimed before 07:20 still switches until the close
    ('2026-10-28T07:30:00-07:00', False),
])
def test_a_claimed_feature_switches_only_inside_the_window(tmp_path, monkeypatch, when, switches):
    clock(monkeypatch, when)
    worker = deployer(tmp_path)
    job = {'id': 'job', 'kind': 'feature', 'proposal': {'sha': G, 'base_sha': OLD_G}}
    if switches:
        worker.process(job)
        worker.compose.assert_any_call('stop', 'red')
    else:
        with pytest.raises(RuntimeError, match='Outside the maintenance window'):
            worker.process(job)
        worker.compose.assert_not_called()


def test_each_deployment_leaves_a_bounded_record_without_member_content(tmp_path, monkeypatch):
    now = maintenance.clock()  # Stopped, so a midnight during the test can't move the record's day (gene #88).
    monkeypatch.setattr(maintenance, 'clock', lambda: now)
    worker = deployer(tmp_path)
    job = {'id': 'a1b2c3d4e5f6', 'kind': 'feature', 'lease': 'f'*32, 'body': 'a member said something private',
           'proposal': {'sha': G, 'base_sha': OLD_G}}
    worker.api = Mock(post=Mock(side_effect=lambda path, data=None: job if path == '/deploy/claim' else {'ok': True}))
    assert worker.tick()
    row = json.loads((tmp_path/'production-actions.jsonl').read_text().splitlines()[-1])
    assert row['action'] == 'deploy feature job a1b2c3d4e5f6' and row['outcome'] == 'ok'
    assert row['before'] == {'gene': OLD_G} and row['after'] == {'gene': G}
    assert row['tz'] == 'America/Los_Angeles' and row['at'] == maintenance.local().isoformat()  # The window's clock.
    assert maintenance.is_open()
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
    ('2026-10-28T07:30:00-07:00', F, '', False),
    ('2026-10-28T07:30:00-07:00', OLD_F, '', True),  # rolling back to the Fri this one replaced
    ('2026-10-28T07:30:00-07:00', F, 'owner: emergency fix, 2026-10-28', True),
])
def test_fri_releases_start_only_in_the_window(tmp_path, monkeypatch, when, sha, exception, allowed):
    clock(monkeypatch, when)
    assert fri_release.permitted(sha, fri_root(tmp_path, {'sha': 'e'*40, 'previous': OLD_F}), exception) is allowed


def test_a_refused_fri_release_changes_nothing_and_is_recorded(tmp_path, monkeypatch):
    clock(monkeypatch, '2026-10-28T07:40:00-07:00')
    root, gene_root = fri_root(tmp_path, {'sha': OLD_F, 'previous': None}), tmp_path/'gene'
    gene_root.mkdir()
    monkeypatch.setattr(sys, 'argv', ['fri_release', F, '--root', str(root), '--gene-root', str(gene_root)])
    monkeypatch.delenv('MAINTENANCE_EXCEPTION', raising=False)
    monkeypatch.setattr(fri_release, 'FriRelease', Mock(side_effect=AssertionError('no release object')))
    with pytest.raises(SystemExit, match=re.escape('Outside the maintenance window [03:30, 07:30) America/Los_Angeles')):
        fri_release.main()
    row = json.loads((gene_root/'production-actions.jsonl').read_text())
    assert row['outcome'].startswith('refused') and row['before'] == {'fri': OLD_F} == row['after']
    assert json.loads((root/'release.json').read_text())['sha'] == OLD_F


# The release script itself, run against an empty root: the gate runs before its lock, staging or
# anything else, so a refusal leaves only its log line.
def script(tmp_path, phase, when, journal=(), extra_env=None, g=G, db=True):
    root = tmp_path/'srv/gene'
    root.mkdir(parents=True, exist_ok=True)
    if db and not (root/'data/controller/gene.sqlite3').exists():
        Store(root/'data/controller/gene.sqlite3')  # The daily cap reads the controller's deployments.
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
@pytest.mark.parametrize('when', ['2026-10-28T03:29:59-07:00', '2026-10-28T07:30:00-07:00',
                                  '2026-10-28T13:00:00-07:00'])
def test_baseline_outside_the_window_takes_no_lock_and_writes_no_staging(tmp_path, when):
    out, root, rows = script(tmp_path, 'baseline', when)
    assert out.returncode == 4
    assert 'REFUSED: outside the maintenance window [03:30, 07:30) America/Los_Angeles' in out.stderr
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
    ('status', ('baseline', 'intent:abort'), False),  # gene #70: an abort's fence may have left development paused
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
    out, _, rows = script(tmp_path, phase, '2026-10-28T07:50:00-07:00', journal)
    if gated:
        assert out.returncode == 4 and 'REFUSED' in out.stderr
    else:
        assert 'REFUSED' not in out.stderr  # past the gate; this empty root fails later on its own
    if phase != 'status':
        assert rows[-1]['action'] == 'batch-release '+phase


@needs_bash4
def test_a_refusal_names_the_batchs_safe_path(tmp_path):
    out, _, _ = script(tmp_path, 'gene', '2026-10-28T07:50:00-07:00', ('baseline', 'drained'))
    assert 'Run resume-old.' in out.stderr
    out, _, _ = script(tmp_path, 'evals', '2026-10-28T07:50:00-07:00', ('baseline', 'drained', 'intent:fri'))
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


def controller(root):
    """The controller DB at the path batch-release.sh reads, set up like the store fixture."""
    result = Store(root/'data/controller/gene.sqlite3')
    result.bootstrap(OLD_G)
    result.update_models([{'id': 'gpt-6-astra', 'name': 'GPT-6 Astra',
                           'efforts': ['medium', 'high'], 'default_effort': 'medium'}])
    result.select_model('gpt-6-astra', 'medium', 'owner')
    result.set_paused(False, 'owner')
    return result


def deploy(store, job, success):
    """The deployer claims the job and finishes it, without writing a line to the production-action log."""
    claimed = store.claim_deploy()
    assert claimed['id'] == job['id']
    store.finish_deploy(claimed['id'], claimed['lease'], success, 'Healthy' if success else 'Failed')


@needs_bash4
def test_the_daily_cap_counts_a_deployment_the_controller_claimed_without_a_log_line(tmp_path, monkeypatch):
    # gene #68: the controller's own deployment record decides, whatever reached the action log.
    root = tmp_path/'srv/gene'
    store = controller(root)
    clocks(monkeypatch, '2026-10-27T04:10:00-07:00')
    deploy(store, approved(store, G, 'message-1'), True)  # yesterday's
    clocks(monkeypatch, '2026-10-28T04:02:00-07:00')
    deploy(store, store.rollback('guild', 'owner'), True)  # restoring service never counts
    out, _, rows = script(tmp_path, 'baseline', '2026-10-28T04:40:00-07:00')
    assert 'REFUSED' not in out.stderr
    clocks(monkeypatch, '2026-10-28T04:41:00-07:00')
    job = approved(store, 'e'*40, 'message-2')
    deploy(store, job, False)  # claimed today and failed
    out, _, rows = script(tmp_path, 'stage', '2026-10-28T04:45:00-07:00')
    assert not [row for row in rows if row['action'].startswith('deploy ')]
    assert out.returncode == 4
    assert f'the controller claimed an automatic deployment today: feature job {job["id"]}' in out.stderr
    assert "CRITICAL_FIX='<reason>'" in out.stderr
    assert rows[-1]['action'] == 'batch-release stage' and rows[-1]['outcome'].startswith('refused')
    out, _, _ = script(tmp_path, 'baseline', '2026-10-29T04:05:00-07:00')  # the next day's window
    assert 'REFUSED' not in out.stderr


@needs_bash4
@pytest.mark.parametrize('job_kind, claimed_at', [
    (None, '2026-10-28T00:00:00-07:00'),  # its job is gone: it counts, as for the controller
    ('repair', '2026-10-29T01:00:00-07:00'),  # dated later than now: the clock went back
])
def test_the_daily_cap_counts_deployments_as_the_controller_does(tmp_path, job_kind, claimed_at):
    root = tmp_path/'srv/gene'
    controller(root)
    with sqlite3.connect(root/'data/controller/gene.sqlite3') as db:
        if job_kind:
            db.execute('INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated,kind) '
                       "VALUES ('job-1','guild','member','src-1','','request','deployed',?,?,?)",
                       (at(claimed_at), at(claimed_at), job_kind))
        db.execute("INSERT INTO deployments VALUES ('job-1',?,?,'deployed',?)", (G, OLD_G, at(claimed_at)))
    out, _, _ = script(tmp_path, 'baseline', '2026-10-28T04:40:00-07:00')
    assert out.returncode == 4
    assert f'the controller claimed an automatic deployment today: {job_kind or "unrecorded"} job job-1' in out.stderr


@needs_bash4
@pytest.mark.parametrize('history', ['missing', 'not a database', 'no deployments table'])
def test_missing_or_unreadable_deployment_history_holds_the_batch(tmp_path, history):
    path = tmp_path/'srv/gene/data/controller/gene.sqlite3'
    path.parent.mkdir(parents=True)
    if history == 'not a database':
        path.write_bytes(b'not a database, but long enough to be read as one' * 4)
    elif history == 'no deployments table':
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    out, root, rows = script(tmp_path, 'baseline', '2026-10-28T04:40:00-07:00', db=False)
    assert out.returncode == 4 and "the controller's deployment history could not be read" in out.stderr
    assert not (root/'batch-release.lock').exists() and not (root/'staging').exists()
    assert rows[-1]['action'] == 'batch-release baseline' and rows[-1]['outcome'].startswith('refused')
    assert path.exists() is (history != 'missing')  # read-only: a missing DB is never created


def pausing(tmp_path, *steps):
    """batch-release.sh's pause and resume on their own, against a controller DB at tmp_path/data, and the
    paused setting after: each step is `ours` (the fence's pause), `lost-id` (that pause, killed before its
    audit id reached the journal), `legacy` (the fence's pause as the script before audit ids made it),
    `late-baseline` (this batch's baseline taken after that), `owner-pause`, `owner-resume`, `resume`
    (resume_services) or `lost-done` (that resume, killed before its journal line)."""
    store = Store(tmp_path/'data/gene.sqlite3')
    store.set_paused(False, 'owner')
    (tmp_path/'staging/batch-x').mkdir(parents=True)
    (tmp_path/'staging/batch-x/baseline.json').write_text('{}')
    stamp = time.time() - 60  # the batch's baseline, a minute before its drain
    os.utime(tmp_path/'staging/batch-x/baseline.json', (stamp, stamp))
    owner = f'"{sys.executable}" -c "import sys; from pathlib import Path; from gene.store import Store; ' \
            'Store(Path(sys.argv[1])).set_paused(sys.argv[2] == \'pause\', \'555\')" "$DATA/gene.sqlite3"'
    calls = {
        'ours': 'mark intent:pause; pause_development',
        'lost-id': "mark intent:pause; pause_development; sed -i '/^paused:/d' \"$JOURNAL\"",
        # 8deef14's fence: the intent line, then Store.set_paused's writes with an audit row that names no batch.
        'legacy': f"""mark intent:pause
"{sys.executable}" - "$DATA/gene.sqlite3" <<'PY'
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE settings SET value='true' WHERE key='paused'")
    db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)',
               (time.time(), 'maintainer:claude', 'pause', '', ''))
PY""",
        'late-baseline': 'touch -d "@$(( $(date +%s) + 60 ))" "$BASELINE"',
        'owner-pause': owner + ' pause',  # /pause, as the controller records it
        'owner-resume': owner + ' resume',
        'resume': 'resume_services ' + G,
        'lost-done': "sed -i '/^done:unpause$/d' \"$JOURNAL\"",  # resume killed before its journal line
    }
    stubs = f'''
set -Eeuo pipefail
DATA={tmp_path}/data JOURNAL={tmp_path}/journal STAGE={tmp_path}/staging/batch-x NOTE=''
BASELINE=$STAGE/baseline.json
db_py() {{ local code; code=$(cat); "{sys.executable}" -c "${{code//\\/data\\//$DATA/}}" "$@"; }}
systemctl() {{ :; }}
audit() {{ :; }}
die() {{ echo "FAIL: $*" >&2; exit 1; }}
'''
    body = functions('log', 'mark', 'pause_mark', 'owns_pause', 'pause_id', 'pause_development', 'unpause_if_ours',
                     'pause_kept', 'resume_services')
    out = subprocess.run(['bash', '-c', stubs + body + '\n' + '\n'.join(calls[step] for step in steps)],
                         capture_output=True, text=True, timeout=60)
    with sqlite3.connect(tmp_path/'data/gene.sqlite3') as db:
        paused = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()[0]
        events = db.execute("SELECT actor,action FROM audit WHERE action IN ('pause','resume') ORDER BY id").fetchall()
    journal = (tmp_path/'journal').read_text().split() if (tmp_path/'journal').exists() else []
    return out, paused, events[1:], journal  # the fixture's own first resume left out


@needs_bash4
def test_resume_lifts_only_the_batchs_own_latest_pause(tmp_path):
    out, paused, events, journal = pausing(tmp_path, 'ours', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'false' and events == [('maintainer:claude', 'pause'), ('maintainer:claude', 'resume')]
    assert journal[-1] == 'done:unpause' and journal[1].startswith('paused:')
    out, paused, events, journal = pausing(tmp_path/'repeat', 'ours', 'resume', 'lost-done', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'false' and events == [('maintainer:claude', 'pause'), ('maintainer:claude', 'resume')]
    assert journal[-1] == 'done:unpause' and 'development already resumed by this batch' in out.stdout


@needs_bash4
def test_resume_leaves_a_pause_the_owner_made_after_the_batchs(tmp_path):
    # gene #69: the owner paused through /pause while the fence waited.
    out, paused, events, journal = pausing(tmp_path, 'ours', 'owner-pause', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'true' and events == [('maintainer:claude', 'pause'), ('555', 'pause')]
    assert 'development stays paused: the latest pause' in out.stdout and 'done:unpause' not in journal


@needs_bash4
def test_resume_leaves_development_the_owner_resumed_after_the_batchs_pause(tmp_path):
    out, paused, events, journal = pausing(tmp_path, 'ours', 'owner-resume', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'false' and events == [('maintainer:claude', 'pause'), ('555', 'resume')]
    assert 'resumed (audit' in out.stdout and 'after this batch paused it' in out.stdout and 'done:unpause' not in journal


@needs_bash4
def test_a_pause_whose_id_never_reached_the_journal_is_known_by_its_batch(tmp_path):
    out, paused, events, journal = pausing(tmp_path, 'lost-id', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'false' and journal == ['intent:pause', 'done:unpause']
    out, paused, _, _ = pausing(tmp_path/'owner', 'lost-id', 'owner-pause', 'resume')
    assert paused == 'true' and 'development stays paused' in out.stdout


@needs_bash4
def test_resume_lifts_the_pause_an_older_script_made_for_this_batch(tmp_path):
    # gene #71 review B1: before audit ids, the pause row named no batch and the journal had no paused:<id>.
    out, paused, events, journal = pausing(tmp_path, 'legacy', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'false' and events == [('maintainer:claude', 'pause'), ('maintainer:claude', 'resume')]
    assert journal == ['intent:pause', 'done:unpause']
    out, paused, _, journal = pausing(tmp_path/'owner', 'legacy', 'owner-pause', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'true' and 'development stays paused: the latest pause' in out.stdout and 'done:unpause' not in journal
    out, paused, _, journal = pausing(tmp_path/'older', 'legacy', 'late-baseline', 'resume')  # from before this batch
    assert out.returncode == 0, out.stderr
    assert paused == 'true' and 'development stays paused: the latest pause' in out.stdout


@needs_bash4
def test_a_pause_the_owner_made_just_before_the_batchs_is_not_the_batchs(tmp_path):
    # gene #71 review S1: the owner paused after the fence read the setting and before the batch's pause.
    out, paused, events, journal = pausing(tmp_path, 'owner-pause', 'ours', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'true' and events == [('555', 'pause')]
    assert journal == ['intent:pause', 'skipped:pause']
    # gene #78: the pause it kept is reported, though the batch never owned it.
    assert 'development stays paused: someone else paused it first (audit' in out.stdout


@needs_bash4
def test_resume_reports_a_pause_from_before_the_batch_and_leaves_it(tmp_path):
    out, paused, events, journal = pausing(tmp_path, 'owner-pause', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'true' and events == [('555', 'pause')] and journal == []
    assert "development stays paused: the latest pause (audit" in out.stdout and "is not this batch's" in out.stdout
    out, paused, _, _ = pausing(tmp_path/'resumed', 'owner-pause', 'owner-resume', 'resume')
    assert out.returncode == 0, out.stderr
    assert paused == 'false' and 'stays paused' not in out.stdout  # nothing to report once it is lifted


@needs_bash4
def test_a_batch_that_resumed_the_old_release_does_not_go_forward_again(tmp_path):
    out, _, _ = script(tmp_path, 'gene', '2026-10-29T04:10:00-07:00', ('baseline', 'drained', 'resumed_old'))
    assert out.returncode == 4 and 'already resumed the old release' in out.stderr


@needs_bash4
@pytest.mark.parametrize('phase', ['drain', 'gene', 'fri', 'evals'])
def test_a_batch_that_stopped_after_its_fence_only_restores(tmp_path, phase):
    # gene #70: a stop journaled before restoring services, whose restore didn't finish. Inside the window
    # and under the cap, the forward phases still refuse and name resume-old, which replays it.
    out, _, rows = script(tmp_path, phase, '2026-10-28T04:10:00-07:00', ('baseline', 'intent:pause', 'intent:abort'))
    assert out.returncode == 4 and 'stopped after its fence' in out.stderr
    assert 'Run resume-old: it resumes development as it is' in out.stderr
    assert rows[-1]['action'] == 'batch-release '+phase and rows[-1]['outcome'].startswith('refused')


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
    out, root, _ = script(tmp_path, 'accept-evals', '2026-10-28T07:50:00-07:00', journal)
    assert out.returncode == 1 and 'set EVALS_ACCEPTED' in out.stderr
    out, root, rows = script(tmp_path, 'accept-evals', '2026-10-28T07:50:00-07:00', journal,
                             {'EVALS_ACCEPTED': 'known sass miss; rerun passed'})
    assert out.returncode == 0 and 'evals accepted: known sass miss' in out.stdout
    assert (root/f'staging/batch-{G}-{F}/journal').read_text().splitlines()[-1] == 'evaluated'


def test_the_deployer_takes_back_a_feature_whose_preparation_ran_past_the_close(tmp_path, monkeypatch):
    times = iter([at('2026-10-28T07:19:00-07:00'), at('2026-10-28T07:30:30-07:00')])
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
    out, _, _ = script(tmp_path, 'resume', '2026-10-28T07:50:00-07:00', journal)
    assert ('REFUSED' in out.stderr) is gated


@needs_bash4
def test_accepting_needs_the_last_evals_run_finished(tmp_path):
    journal = ('baseline', 'drained', 'evals_started', 'evals_ran', 'evals_started')
    out, _, _ = script(tmp_path, 'accept-evals', '2026-10-28T07:50:00-07:00', journal, {'EVALS_ACCEPTED': 'why'})
    assert out.returncode == 4


PYPROJECT = (Path(__file__).parents[1]/'pyproject.toml').read_text()
# gene #116: changes to the live pyproject.toml, as (old, new) or text to append. Metadata may differ...
METADATA_CHANGES = {
    'license': ('description = ', 'license = "GPL-3.0-only"\ndescription = '),
    'description': ('description = "', 'description = "an older line. '),
    'authors': ('description = ', 'authors = [{name = "someone"}]\ndescription = '),
    'urls': '\n[project.urls]\nSource = "https://example.invalid/gene"\n',
    'ruff': ('line-length = 100', 'line-length = 110'),
    'pytest': ('testpaths = ["tests"]', 'testpaths = ["tests", "more"]'),
}
# ...but nothing the editable install installs from, or how.
INSTALL_CHANGES = {
    'project.dependencies': ('"httpx>=0.28,<1",', '"httpx>=0.28,<1",\n    "anyio>=4,<5",'),
    'project.optional-dependencies': ('"pillow>=13,<14",', '"pillow>=12,<14",'),
    'project.requires-python': ('">=3.11,<3.12"', '">=3.11,<3.13"'),
    'project.version': ('version = "0.1.0"', 'version = "0.1.1"'),
    'project.scripts': ('gene-deployer = "gene.deploy:main"', 'gene-deployer = "gene.deploy:run"'),
    'build-system.requires': ('hatchling==1.27.0', 'hatchling==1.26.3'),
    'dependency-groups.dev': ('"pytest>=8,<9"', '"pytest>=8,<10"'),
    'tool.hatch': ('packages = ["src/gene"]', 'packages = ["src/gene", "src/other"]'),
    'tool.uv': '\n[tool.uv]\nindex-url = "https://example.invalid/simple"\n',
}


def edited(change):
    if isinstance(change, str):
        return PYPROJECT + change
    old, new = change
    assert PYPROJECT.count(old) == 1, old
    return PYPROJECT.replace(old, new)


def installs_same(tmp_path, live, tomllib=True, staged=None):
    """batch-release.sh's stage check of the live pyproject.toml against the candidate's (this checkout's, or
    `staged`)."""
    bin_dir = tmp_path/'bin'
    bin_dir.mkdir(parents=True)
    (bin_dir/'python3').write_text(f'#!/usr/bin/env bash\nexec "{sys.executable}" "$@"\n')
    (bin_dir/'python3').chmod(0o755)
    (tmp_path/'live.toml').write_text(live)
    candidate = SCRIPT.parents[2]/'pyproject.toml'
    if staged is not None:
        candidate = tmp_path/'staged.toml'
        candidate.write_text(staged)
    env = {**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'}
    if not tomllib:  # A python3 older than 3.11: the import fails.
        (tmp_path/'shadow').mkdir()
        (tmp_path/'shadow/tomllib.py').write_text('raise ImportError("no tomllib here")\n')
        env['PYTHONPATH'] = str(tmp_path/'shadow')
    shell = ('set -Eeuo pipefail\n' + functions('installs_same')
             + f'\ninstalls_same {tmp_path}/live.toml {candidate}\n')
    return subprocess.run(['bash', '-c', shell], capture_output=True, text=True, timeout=30, env=env)


@pytest.mark.parametrize('name', sorted(METADATA_CHANGES))
def test_stage_lets_a_metadata_only_pyproject_change_through(tmp_path, name):
    out = installs_same(tmp_path, edited(METADATA_CHANGES[name]))
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith('pyproject.toml differs only in metadata:') and name in out.stdout


@pytest.mark.parametrize('name', sorted(INSTALL_CHANGES))
def test_stage_refuses_a_pyproject_change_in_what_the_venv_installs(tmp_path, name):
    out = installs_same(tmp_path, edited(INSTALL_CHANGES[name]))
    assert out.returncode != 0
    assert 'pyproject.toml changes what the host venv installs:' in out.stderr and name in out.stderr


def test_stage_refuses_a_pyproject_it_cant_parse_or_compare(tmp_path):
    out = installs_same(tmp_path/'broken', PYPROJECT + 'broken = [\n')
    assert out.returncode != 0 and 'could not parse' in out.stderr
    out = installs_same(tmp_path/'not-a-table', 'project = "gene"\n')
    assert out.returncode != 0 and '[project] is not a table' in out.stderr
    out = installs_same(tmp_path/'no-tomllib', edited(METADATA_CHANGES['license']), tomllib=False)
    assert out.returncode != 0 and 'no tomllib' in out.stderr and 'differs only' not in out.stdout


@pytest.mark.parametrize('live, staged', [
    ('[tool.uv]\npackage = 1\n', '[tool.uv]\npackage = true\n'),
    ('[tool.uv]\nconstraint-dependencies = [1]\n', '[tool.uv]\nconstraint-dependencies = [1.0]\n'),
    ('[tool.hatch.build]\nreproducible = 1\n', '[tool.hatch.build]\nreproducible = true\n'),
])
def test_stage_refuses_a_setting_that_only_changed_type(tmp_path, live, staged):
    out = installs_same(tmp_path, PYPROJECT + '\n' + live, staged=PYPROJECT + '\n' + staged)
    assert out.returncode != 0 and 'pyproject.toml changes what the host venv installs:' in out.stderr


STAMP = '[tool.hatch.build.hooks.custom]\nbuilt = {}\n'


# gene #124: a timestamp is the setting as written. The same instant at another offset reads differently (a build
# hook taking `.hour` gets 5, not 12).
@pytest.mark.parametrize('live, staged', [
    ('2026-10-28T12:00:00Z', '2026-10-28T05:00:00-07:00'),
    ('2026-10-28T12:00:00+00:00', '2026-10-28T21:00:00+09:00'),
    ('2026-10-28T12:00:00Z', '2026-10-28T12:00:00.000001Z'),
    ('12:00:00', '12:00:00.5'),
    ('2026-10-28', '2026-10-29'),
])
def test_stage_refuses_a_timestamp_written_differently(tmp_path, live, staged):
    out = installs_same(tmp_path, PYPROJECT + '\n' + STAMP.format(live), staged=PYPROJECT + '\n' + STAMP.format(staged))
    assert out.returncode != 0 and 'changes what the host venv installs: tool.hatch' in out.stderr


@pytest.mark.parametrize('live, staged', [
    ('2026-10-28T12:00:00Z', '2026-10-28T12:00:00+00:00'),  # Two spellings of UTC, read alike.
    ('2026-10-28T05:00:00-07:00', '2026-10-28 05:00:00-07:00'),
    ('2026-10-28T12:00:00', '2026-10-28T12:00:00'),
    ('12:00:00', '12:00:00'),
    ('2026-10-28', '2026-10-28'),
])
def test_stage_lets_an_unchanged_timestamp_through(tmp_path, live, staged):
    out = installs_same(tmp_path, PYPROJECT + '\n' + STAMP.format(live), staged=PYPROJECT + '\n' + STAMP.format(staged))
    assert out.returncode == 0, out.stderr


# A float is the setting bit for bit: 0.0 and -0.0 can steer a build hook differently (math.copysign), and an
# unchanged nan is unchanged, not a reason to stop.
@pytest.mark.parametrize('live, staged', [('0.0', '-0.0'), ('-0.0', '0.0'), ('nan', '-nan'), ('{a = [0.0]}', '{a = [-0.0]}')])
def test_stage_refuses_a_float_that_changed_sign(tmp_path, live, staged):
    out = installs_same(tmp_path, PYPROJECT + '\n' + STAMP.format(live), staged=PYPROJECT + '\n' + STAMP.format(staged))
    assert out.returncode != 0 and 'changes what the host venv installs: tool.hatch' in out.stderr


@pytest.mark.parametrize('value', ['nan', '-nan', 'inf', '0.0', '-0.0', '{a = [nan]}'])
def test_stage_lets_an_unchanged_float_through(tmp_path, value):
    out = installs_same(tmp_path, PYPROJECT + '\n' + STAMP.format(value), staged=PYPROJECT + '\n' + STAMP.format(value))
    assert out.returncode == 0, out.stderr
