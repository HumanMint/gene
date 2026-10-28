"""Root-only Gene image retention. Unknown/inactive images are never retirement evidence.

Release history comes from batch-release.sh's staging manifests and journals and from the controller's
legacy deployments rows with their backups. A run, plan or apply, takes a lock and writes files, so it starts
only in the production maintenance window, [04:00, 05:00) America/Los_Angeles (owner policy, 2026-10-27).
--status is a read-only health check for any hour.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

SHA = re.compile(r'[0-9a-f]{40}')
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')
VERSION_TAG = re.compile(r'gene-(?:runtime|worker):([0-9a-f]{40})')
FRI_TAG = re.compile(r'fri-runtime:[0-9a-f]{40}')
REFERENCE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]*')
BATCH_DIR = re.compile(r'batch-([0-9a-f]{40})(?:-([0-9a-f]{40})(?:-([0-9]+))?)?')  # batch-<G> before 1ad1493
TERMINAL = {'failed', 'rejected', 'deployed', 'cancelled'}
GRACE = 24 * 60 * 60
ROLLBACK_DEPTH = 2  # complete rollback configurations kept behind the live release
KEEP_RELEASES = 3  # batch-release.sh cleanup keeps the images of this many recently resumed batches
ROLES = {'runtime', 'worker', 'fri'}
LIVE = {'gene_recorded', 'fri_switched', 'resumed'}
# What batch-release.sh's rollback-gene and resume-old read from a batch, besides its manifest and images.
LEVEL_MARKS = LIVE | {'gene_attempted', 'drained'}
FRI_ONLY_MARKS = LIVE | {'fri_backup', 'drained'}  # what rollback-fri and resume-old need from a Fri-only batch
BACKUP_FILES = ('release.env', 'worker.env', 'fri-release.env', 'fri-release.json', 'db/controller.sqlite3',
                'installed-source/ops/compose.yml', 'installed-source/ops/fri-compose.yml')
SAVED_GENE = ('release.env', 'worker.env', 'fri-release.env', 'fri-release.json')  # what rollback-gene restores
SAVED_FRI = ('fri-release.env', 'fri-release.json')  # what rollback-fri restores
DOWNGRADES = re.compile(r'(?:gene\.[a-z_]+(?: gene\.[a-z_]+)*)?')
# The modules batch-release.sh's downgrade_step knows; it dies on any other, partway through a rollback.
DOWNGRADE_STEPS = {'gene.fri_reply_checks', 'gene.profile_downgrade', 'gene.fri_member_summary'}
# What rollback-gene's compose and source steps read from installed-source once it is moved to source/;
# a symlink among them would mean something else after the move.
MOVED_ENTRIES = ('', '.release-sha', 'ops', 'ops/compose.yml', 'ops/fri-compose.yml')
ZONE = 'America/Los_Angeles'
EARLIEST = 1793059200  # 2026-10-27T00:00Z: a clock before the window policy existed is not trusted
STALE = 50 * 60 * 60  # two missed daily windows
LOCK_BUDGET = 10 * 60  # longest a run keeps batch-release.lock, from taking it to its last removal
REMOVAL_MARGIN = 120  # a removal starts only with a full command timeout left on the budget
monotonic = time.monotonic  # the budget's clock
DEADLINE = None  # while a run holds batch-release.lock: the monotonic time it must give the lock back by
REFUSED = 4  # exit status of a refused or skipped run: nothing changed, the next window tries again
FRI_ROOT = Path('/srv/fri')
RELEASE = Path(__file__).resolve().parent.name  # maintenance/releases/<sha>/ when installed
RELEASE = RELEASE if SHA.fullmatch(RELEASE) else None
CONTAINER = 'used by a container (running or stopped)'
CURRENT = 'current Gene release (pins or a running service)'
ROLLBACK = 'rollback release'
RECENT = 'recent batch release (batch cleanup keeps these)'
UNFINISHED = 'unfinished batch release'
FRI_LIVE = 'live Fri release'
PENDING = 'tagged with the current, previous, a rollback or a pending release SHA'
CONFIGURED = 'configured image reference'
HOLD = 'explicit hold in the retention policy'
AUDIO = 'independent audio release'
OUTSIDE = 'outside the versioned Gene runtime/worker scope'
FRI_OWNED = 'Fri release tag: batch cleanup owns its removal'
KEPT_PARENT = 'untagged parent of a kept image: Docker keeps it while that image exists'
UNCLASSIFIED = 'untagged and unclassified: nothing proves it belongs to a retired Gene version'
PRIORITY = [CONTAINER, CURRENT, ROLLBACK, RECENT, UNFINISHED, FRI_LIVE, PENDING, CONFIGURED, HOLD, AUDIO]


class Busy(ValueError):
    """Another release step holds the host. Nothing changed; the next window tries again."""


def spent():
    return Busy(f'This run used its {LOCK_BUDGET // 60}-minute hold on batch-release.lock; the rest waits for the '
                'next window')


def check_deadline():
    if DEADLINE is not None and DEADLINE - monotonic() <= 0:
        raise spent()


def command(*args):
    """Every command a run starts ends within the time left on its hold of batch-release.lock."""
    left = math.inf if DEADLINE is None else DEADLINE - monotonic()
    if left <= 0:
        raise spent()
    timeout = min(120, left)
    try:
        return subprocess.check_output(args, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if timeout < 120:
            raise spent() from None
        raise


def read_env(path):
    """Read data without sourcing shell code or returning credentials."""
    result = {}
    for line in path.read_text().splitlines():
        if '=' not in line or line.lstrip().startswith('#'):
            continue
        key, value = line.split('=', 1)
        key, value = key.strip(), value.strip().strip('\"\'')
        if key.endswith('_IMAGE') or key == 'GENE_RELEASE_SHA':
            if key in result:
                raise ValueError('Duplicate image/release setting: ' + str(path))
            result[key] = value
    return result


def read_pins(path, keys):
    """Only the named pin lines; the rest of the file is never returned."""
    found = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator and key.strip() in keys:
            if key.strip() in found:
                raise ValueError('Duplicate pin in ' + str(path))
            found[key.strip()] = value.strip().strip('\"\'')
    return found


def read_json(path):
    if path.is_symlink():
        raise ValueError('Refusing symlinked state/policy')
    return json.loads(path.read_text())


def save_json(path, data):
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(descriptor, 'w') as output:
            json.dump(data, output, sort_keys=True, indent=2)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def local(now):
    """The time in the window's zone, or None when the clock or the zone is unknown."""
    try:
        from zoneinfo import ZoneInfo
        if not math.isfinite(now) or now < EARLIEST:
            return None
        return dt.datetime.fromtimestamp(now, ZoneInfo(ZONE))
    except Exception:  # A missing zone database or an unreadable clock: closed, never open.
        return None


def window_open(now):
    """[04:00, 05:00) local, following daylight saving. src/gene/maintenance.py holds the same window."""
    moment = local(now)
    return moment is not None and dt.time(4) <= moment.time() < dt.time(5)


def docker_inventory():
    images = {}
    # -a: unnamed intermediates too, so the plan accounts for every image object.
    for line in command('docker', 'image', 'ls', '-a', '--no-trunc', '--format', '{{json .}}').splitlines():
        row = json.loads(line)
        iid = row['ID']
        if not IMAGE_ID.fullmatch(iid):
            raise ValueError('Docker returned an invalid image ID')
        tags = images.setdefault(iid, {'tags': []})['tags']
        if row['Repository'] != '<none>' and row['Tag'] != '<none>':
            tags.append(row['Repository'] + ':' + row['Tag'])
    for row in images.values():
        row['tags'] = sorted(set(row['tags']))
    ids = command('docker', 'ps', '-aq', '--no-trunc').split()
    rows = command('docker', 'inspect', '--format', '{{.Id}} {{.Image}}', *ids).splitlines() if ids else []
    # Request only metadata, never complete inspect output containing credentials.
    return images, dict(row.split() for row in rows)


def controller_refs(root):
    path = root / 'data/controller/gene.sqlite3'
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN "
                                   "('active_sha','previous_sha','fri_active_sha')"))
        jobs, cog_jobs = [], {}
        for job, kind, state, proposal, approved in db.execute(
                'SELECT id,kind,state,proposal,approved_sha FROM jobs'):
            if state == 'deployed' and kind in {'feature', 'repair', 'rollback'}:
                details = json.loads(proposal) if proposal else {}
                cog_jobs[job] = (details.get('sha'), details.get('base_sha'))
            if state in TERMINAL:
                continue
            if state in {'building', 'deploying'}:
                raise Busy('A Gene build/deployment is in progress; retry later')
            proposal = json.loads(proposal) if proposal else {}
            refs = [v for v in (proposal.get('sha'), proposal.get('base_sha'), approved) if v]
            if any(not SHA.fullmatch(v) for v in refs):
                raise ValueError('Invalid pending job reference')
            jobs.extend(refs)
        deployments = list(db.execute("SELECT job_id,sha,previous_sha,at FROM deployments "
                                     "WHERE state='deployed' ORDER BY at DESC"))
    if not SHA.fullmatch(settings.get('active_sha', '')):
        raise ValueError('Missing active controller release')
    return settings, sorted(set(jobs)), deployments, cog_jobs


def process_refs():
    refs = []
    for unit in ('gene-worker.service', 'gene-deployer.service'):
        pid = int(command('systemctl', 'show', unit, '--property=MainPID', '--value').strip())
        if pid:
            for value in Path(f'/proc/{pid}/environ').read_bytes().split(b'\0'):
                key, _, text = value.partition(b'=')
                if key.endswith(b'_IMAGE'):
                    refs.append(text.decode())
    # A maintainer must not start or retarget a build while cleanup holds its lock.
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            args = (proc / 'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:
            continue
        if (any(b'docker' in arg or b'buildx' in arg for arg in args[:2])
                and any(arg in (b'build', b'buildx') for arg in args)):
            raise Busy('A Docker build is in progress; retry later')
    return refs


@contextmanager
def batch_lock(root):
    """batch-release.sh's own lock, taken non-blocking and held for the whole run, for at most LOCK_BUDGET.

    No batch command starts while retention discovers and removes. One that tries exits 1 at its lock
    check having changed nothing (batch_lock in batch-release.sh), and is repeated once this run ends.
    """
    global DEADLINE
    path = root / 'batch-release.lock'
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        descriptor = os.open(path, os.O_RDONLY | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy('A batch-release command holds its lock; retry in the next window') from None
        DEADLINE = monotonic() + LOCK_BUDGET
        yield
    finally:
        DEADLINE = None
        os.close(descriptor)


def batch_records(root):
    """batch-release.sh's record of each batch: staging/batch-*/manifest.json and its journal."""
    staging, records = root / 'staging', []
    if staging.is_symlink():
        raise ValueError('Refusing symlinked staging')
    for path in sorted(staging.glob('batch-*')) if staging.is_dir() else []:
        journal = path / 'journal'
        if path.is_symlink() or journal.is_symlink():
            raise ValueError('Refusing symlinked batch record ' + path.name)
        if not journal.exists():
            continue  # A baseline refused before its first journal line changed nothing; cleanup skips it too.
        match = BATCH_DIR.fullmatch(path.name)
        if not match or not journal.is_file():
            raise ValueError('Unrecognized batch record ' + path.name)
        try:
            manifest = read_json(path / 'manifest.json')
        except (OSError, ValueError) as exc:
            raise ValueError(f'Malformed batch record {path.name}: unreadable manifest ({type(exc).__name__})') from None
        data = journal.read_bytes()
        try:
            words = set(data.decode().split())  # cleanup's prune_images reads marks this way
        except UnicodeDecodeError:
            raise ValueError(f'Malformed batch record {path.name}: its journal is not text') from None
        # The phases ask has(), grep -x: whole lines, a carriage return included, in the journal's order.
        lines = [line.decode('utf-8', 'surrogateescape') for line in data.split(b'\n')]
        records.append(batch_record(path.name, match, manifest, set(lines), journal.stat().st_mtime, words, lines))
    return records


def is_sha(value):
    """A full SHA as batch-release.sh writes it: a JSON string. A number that jget would print as the same
    digits is not one; neither is anything the script would read differently."""
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def batch_record(name, match, manifest, marks, at, words=None, lines=None):
    def bad(why):
        return ValueError(f'Malformed batch record {name}: {why}')
    if (not isinstance(manifest, dict) or manifest.get('gene') != match[1]
            or (match[2] and manifest.get('fri') != match[2]) or not is_sha(manifest.get('fri'))):
        raise bad('the manifest names a different release')
    images = {}
    for kind in ('old', 'new'):
        entries = manifest.get(kind, {})
        if not isinstance(entries, dict) or not set(entries) <= ROLES:
            raise bad(f'unexpected {kind} images')
        for role, entry in entries.items():
            if (not isinstance(entry, dict) or not isinstance(entry.get('tag'), str)
                    or not REFERENCE.fullmatch(entry['tag']) or not isinstance(entry.get('id'), str)
                    or not IMAGE_ID.fullmatch(entry['id'])):
                raise bad(f'invalid {kind} {role} image')
        images[kind] = {role: (entry['tag'], entry['id']) for role, entry in entries.items()}
    if set(images['old']) != ROLES or ('resumed' in marks and set(images['new']) != ROLES):
        raise bad('incomplete image record')
    # batch-<G> manifests have no old pair. Their baseline pinned the old worker as gene-worker:<old Gene>.
    old_gene = manifest.get('old_gene', images['old']['worker'][0].removeprefix('gene-worker:'))
    if not is_sha(old_gene) or images['old']['worker'][0] != 'gene-worker:' + old_gene:
        raise bad('no old Gene release')
    old_fri = manifest.get('old_fri')  # batch-<G>: read from its backup's Fri pins when it is a rollback step
    if old_fri is not None and not is_sha(old_fri):
        raise bad('invalid old Fri release')
    return {'name': name, 'gene': match[1], 'fri': manifest['fri'], 'old_gene': old_gene, 'old_fri': old_fri,
            'images': images, 'marks': marks, 'at': at, 'backup': manifest.get('backup'),
            'old_pair': 'old_gene' in manifest and 'old_fri' in manifest, 'downgrades': manifest.get('downgrades'),
            'gene_changes': match[1] != old_gene,
            'words': marks if words is None else words, 'lines': sorted(marks) if lines is None else lines,
            'live': LIVE <= marks and not marks & {'intent:rollback', 'resumed_old'}}


def touched(record):
    """batch-release.sh's touched(): this batch has fenced development, or begun to."""
    return bool(record['marks'] & {'drained', 'intent:pause', 'intent:abort'})


def aborting(record):
    """batch-release.sh's aborting(): a stop after a fence whose restore hasn't finished, by the later of the
    journal's intent:abort and resumed_old lines. rollback-fri refuses then, and resume-old resumes as it is."""
    latest = [line for line in record['lines'] if line in ('intent:abort', 'resumed_old')]
    return bool(latest) and latest[-1] == 'intent:abort'


def in_flight(record):
    """Development is fenced or switching for this batch: touched without resuming, mid-rollback, or stopped
    after its fence with its restore unfinished."""
    marks = record['marks']
    return aborting(record) or ('resumed_old' not in marks and (
        'intent:rollback' in marks or ('resumed' not in marks and touched(record))))


def unfinished(record):
    """cleanup's rule: not back in service yet, so its next phase checks every image its manifest names."""
    marks = record['words']
    return 'resumed_old' not in marks and ('resumed' not in marks or 'intent:rollback' in marks)


def switch_backup(root, record):
    """The batch's switch backup directory: the manifest's path, used as written, as batch-release.sh uses it.
    It must be canonical (absolute, no . or .. or trailing newline that the kernel or jget would reinterpret),
    directly under backups/, and not a symlink."""
    text = record['backup']
    if not isinstance(text, str) or '\n' in text or not os.path.isabs(text) or os.path.normpath(text) != text:
        return None
    backup = Path(text)
    if backup.parent != root / 'backups' or backup.is_symlink() or not backup.is_dir():
        return None
    return backup


def batch_evidence(root, record, by_tag):
    """The old Gene tags a batch in service retired, only when its record checks out.

    The manifest's old pair must be that release's own tags, still naming the recorded images, and the
    switch backup's pins must name the same pair. Otherwise the record supersedes nothing.
    """
    old, old_gene = record['images']['old'], record['old_gene']
    backup = switch_backup(root, record)
    if (not record['live'] or not record['gene_changes'] or backup is None
            or old['runtime'][0] != 'gene-runtime:' + old_gene or old['worker'][0] != 'gene-worker:' + old_gene
            or any(by_tag.get(tag) != iid for tag, iid in (old['runtime'], old['worker']))):
        return []
    try:
        release, workers = (backup / 'release.env').read_bytes(), (backup / 'worker.env').read_bytes()
    except OSError:
        return []
    if (gene_pins(release) != {'GENE_RELEASE_SHA': old_gene, 'GENE_RUNTIME_IMAGE': old['runtime'][0]}
            or worker_pin(workers, old_gene) != old['worker'][0]):
        return []
    return [old['runtime'][0], old['worker'][0]]


def batch_step(root, record, by_tag):
    """One batch as a complete rollback level: everything its rollback-gene and resume-old read.

    Returns its old Fri and the previous_sha its rollback restores from baseline.json.
    """
    name, old, new = record['name'], record['images']['old'], record['images']['new']

    def incomplete(why):
        return ValueError(f'Batch {name} is not a complete rollback configuration: {why}')
    if not record['old_pair']:
        # batch-release.sh reads OLD_G and OLD_F from the manifest; only the older script it came from could roll it back.
        raise incomplete('its manifest names no old pair, so this batch-release.sh cannot roll it back')
    missing = sorted(LEVEL_MARKS - record['marks'])
    if missing:
        raise incomplete('its journal has no ' + ', '.join(missing))
    if not isinstance(record['downgrades'], str) or not DOWNGRADES.fullmatch(record['downgrades']):
        raise incomplete('its manifest records no downgrades list')
    unknown = sorted(set(record['downgrades'].split()) - DOWNGRADE_STEPS)
    if unknown:
        raise incomplete('downgrade_step has no step for ' + ', '.join(unknown))
    if new['runtime'][0] != 'gene-runtime:' + record['gene'] or new['worker'][0] != 'gene-worker:' + record['gene']:
        raise incomplete('its new Gene images are not tagged with its release')
    # check_manifest: every recorded tag still names the exact recorded image.
    for kind, entries in (('old', old), ('new', new)):
        for role, (tag, iid) in sorted(entries.items()):
            if by_tag.get(tag) != iid:
                raise incomplete(f'{kind} {role} image {tag} is missing or changed')
    baseline = baseline_of(root, record, incomplete)
    backup = switch_backup(root, record)
    if backup is None:
        raise incomplete('its switch backup is missing or outside backups/')
    missing = [path for path in BACKUP_FILES if not (backup / path).is_file()]
    if missing:
        raise incomplete(f'backup {backup.name} is missing ' + ', '.join(missing))
    # What the saved pins and Fri record say is checked once they are restored, by verify old's own reading.
    saved = {name: (backup / name).read_bytes() for name in SAVED_GENE}
    old_fri = record['old_fri']
    if new['fri'] != (old['fri'] if record['fri'] == old_fri else ('fri-runtime:' + record['fri'], new['fri'][1])):
        raise incomplete('its new Fri image is not its Fri release')
    if not (root / 'releases' / record['old_gene'] / 'cogs').is_dir():
        raise incomplete(f"the rollback cogs releases/{record['old_gene']} are missing")
    return {'record': record, 'baseline': baseline, 'backup': backup, 'saved': saved}


def fri_step(root, record, by_tag, restores, source, gene_images):
    """A Fri-only batch above a Gene level, which rollback-fri undoes before that level's rollback-gene can run:
    everything its rollback-fri and resume-old read.

    restores is the controller previous_sha of the Gene release it sits on, source that release's source
    marker when known, and gene_images that release's runtime and worker images when known.
    """
    name, old, new = record['name'], record['images']['old'], record['images']['new']

    def incomplete(why):
        return ValueError(f'Batch {name} is not a complete Fri-only rollback: {why}')
    if not record['old_pair']:
        raise incomplete('its manifest names no old pair, so this batch-release.sh cannot roll it back')
    missing = sorted(FRI_ONLY_MARKS - record['marks'])
    if missing:
        raise incomplete('its journal has no ' + ', '.join(missing))
    if (record['fri'] == record['old_fri'] or new['runtime'] != old['runtime'] or new['worker'] != old['worker']
            or new['fri'] != ('fri-runtime:' + record['fri'], new['fri'][1])):
        raise incomplete('its images are not a Fri-only release')
    if gene_images is not None and (old['runtime'], old['worker']) != gene_images:
        raise incomplete('its Gene images are not the ones the Gene release it sits on put in service')
    for kind, entries in (('old', old), ('new', new)):
        for role, (tag, iid) in sorted(entries.items()):
            if by_tag.get(tag) != iid:
                raise incomplete(f'{kind} {role} image {tag} is missing or changed')
    baseline = baseline_of(root, record, incomplete)
    if baseline['settings']['previous_sha'] != restores:
        raise incomplete(f"its baseline.json previous_sha {baseline['settings']['previous_sha']} is not the Gene "
                         f"release it sits on's ({restores})")
    if source is not None and baseline['source'] != source:
        raise incomplete('its baseline.json source is not the Gene release it sits on')
    backup = switch_backup(root, record)
    if backup is None:
        raise incomplete('its switch backup is missing or outside backups/')
    missing = [path for path in ('fri-release.env', 'fri-release.json') if not (backup / path).is_file()]
    if missing:
        raise incomplete(f'backup {backup.name} is missing ' + ', '.join(missing))
    saved = {name: (backup / name).read_bytes() for name in SAVED_FRI}  # checked restored, by verify old
    return {'record': record, 'baseline': baseline, 'backup': backup, 'saved': saved}


def live_images():
    """Each service container's image ID, or 'none', as batch-release.sh's container_image reads it."""
    found = {}
    for key, name in (('controller', 'gene-controller-1'), ('red', 'gene-red-1'), ('fri', 'fri-runtime-1')):
        try:
            found[key] = command('docker', 'inspect', '-f', '{{.Image}}', name).rstrip('\n')
        except subprocess.CalledProcessError:
            found[key] = 'none'
    return found


def live_pair_state(root, settings):
    """What batch-release.sh's pair checks read from the live host: the pin files and Fri record as bytes
    (compared, never printed), the controller's release settings, the source marker and container images."""
    def read(path):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
    tree = root / 'source'
    return {'release.env': read(root / 'release.env'), 'worker.env': read(root / 'secrets/worker.env'),
            'fri release.env': read(FRI_ROOT / 'release.env'), 'fri release.json': read(FRI_ROOT / 'release.json'),
            'settings': {key: settings.get(key) for key in ('active_sha', 'previous_sha', 'fri_active_sha')},
            'source': source_state(tree), 'source_tree': tree if os.path.lexists(tree) else None} | live_images()


def source_state(tree):
    """batch-release.sh's source_state for a source tree, as $(source_state) yields it: [[ -f ]] and cat follow
    symlinks; command substitution drops NUL bytes and trailing newlines and nothing else; else unmarked or
    missing."""
    marker = tree / '.release-sha'
    if marker.is_file():
        return marker.read_bytes().replace(b'\0', b'').rstrip(b'\n').decode('utf-8', 'surrogateescape')
    return 'unmarked' if tree.is_dir() else 'missing'


def gene_pins(data):
    """state()'s gene_env from release.env bytes, or None where state() itself would fail on them."""
    values = release_values(data)
    return None if values is None else {key: values.get(key) for key in ('GENE_RELEASE_SHA', 'GENE_RUNTIME_IMAGE')}


def fri_pins(data):
    """state()'s fri_env from Fri's release.env bytes, or None where state() itself would fail on them."""
    values = release_values(data)
    return None if values is None else {key: values.get(key) for key in ('FRI_RELEASE_SHA', 'FRI_RUNTIME_IMAGE')}


def worker_pin(data, *shas):
    """state()'s worker_pin: the last of shas whose exact line grep -Fxc counts once in worker.env."""
    pin = 'unknown'
    for sha in shas:
        if (data or b'').split(b'\n').count(f'GENE_WORKER_IMAGE=gene-worker:{sha}'.encode()) == 1:
            pin = 'gene-worker:' + sha
    return pin


def release_values(data):
    """An env file as state()'s env() and the deployer read it: text with universal newlines, every line with
    '=', split once, nothing stripped. None where decoding would fail them."""
    try:
        return dict(line.split('=', 1) for line in (data or b'').decode().splitlines() if '=' in line)
    except UnicodeDecodeError:
        return None


def pair_mirror(kind, step, state, root_text):
    """The checks batch-release.sh's recovery makes before it changes anything, on a simulated live state:
    pair_check for rollback-gene (kind 'gene'), fri_pair_check and gene_unmoved for rollback-fri ('fri').
    The same fields, the same byte comparisons and exceptions. Returns the names of the failing checks."""
    record, saved, baseline = step['record'], step['saved'], step['baseline']
    G, F, OLD_G, OLD_F = record['gene'], record['fri'], record['old_gene'], record['old_fri']
    old, new, settings = record['images']['old'], record['images']['new'], state['settings']
    try:
        fri_record = json.loads(state['fri release.json'] or b'null')
    except ValueError:
        fri_record = None
    checks = {
        'fri release.env': state['fri release.env'] in (saved['fri-release.env'],
                                                        saved['fri-release.env'].replace(OLD_F.encode(), F.encode())),
        # The record a rollback restores is its own saved bytes; otherwise it must be F's, replacing OLD_F.
        'fri release.json': (state['fri release.json'] == saved['fri-release.json']
                             or (isinstance(fri_record, dict) and fri_record.get('sha') == F
                                 and fri_record.get('previous') == OLD_F)),
        'fri_active_sha': settings.get('fri_active_sha') in (None, OLD_F, F),
        'fri image': state['fri'] in (old['fri'][1], new['fri'][1], 'none')}
    if kind == 'gene':
        new_release = f'GENE_RELEASE_SHA={G}\nGENE_ROOT={root_text}\nGENE_RUNTIME_IMAGE=gene-runtime:{G}\n'.encode()
        new_worker = re.sub(rb'(?m)^GENE_WORKER_IMAGE=.*$', b'GENE_WORKER_IMAGE=gene-worker:' + G.encode(),
                            saved['worker.env'])
        gene_images = (old['runtime'][1], new['runtime'][1], 'none')
        checks |= {
            'release.env': state['release.env'] in (saved['release.env'], new_release),
            'worker.env': state['worker.env'] in (saved['worker.env'], new_worker),
            'release settings': (settings.get('active_sha'), settings.get('previous_sha'))
                                in ((OLD_G, baseline['settings']['previous_sha']), (G, OLD_G)),
            'controller image': state['controller'] in gene_images, 'red image': state['red'] in gene_images,
            'source': (state['source'], (step['backup'] / 'installed-source').is_dir())
                      in ((baseline['source'], False), (G, True), ('missing', True))}
    else:
        checks |= {
            'active_sha': settings.get('active_sha') == G,
            'release.env': gene_pins(state['release.env']) == {'GENE_RELEASE_SHA': G, 'GENE_RUNTIME_IMAGE': old['runtime'][0]},
            'worker_pin': worker_pin(state['worker.env'], G, OLD_G) == old['worker'][0],
            'source': state['source'] == baseline['source'],
            'controller_image': state['controller'] == old['runtime'][1], 'red_image': state['red'] == old['runtime'][1]}
    return [name for name, passed in checks.items() if not passed]


def restore_source(step, state):
    """rollback-gene's source step on a simulated state: a source marked with G moves aside to the backup;
    if nothing is then at source/, installed-source is renamed into place; and the source must then read as
    baseline.json's source, or the rollback dies there. Returns (failing checks, the source it reads, its tree).

    rename(2) moves an entry as it is, so a symlink among what the recovery reads would point somewhere else
    once moved: that refuses rather than guessing.
    """
    record, baseline, backup = step['record'], step['baseline'], step['backup']
    tree, failing = state['source_tree'], []
    if tree is not None and source_state(tree) == record['gene']:  # [[ -f marker && $(cat marker) == "$G" ]]
        failed = backup / 'failed-source'
        if os.path.lexists(failed) and not failed.exists():  # [[ ! -e ]] picks it, then rename_tree refuses
            failing.append('a dangling failed-source is in the way')
        tree = None
    if tree is None or not tree.exists():  # [[ ! -e $ROOT/source ]]
        installed = backup / 'installed-source'
        if tree is not None:  # a dangling symlink at source/: rename_tree refuses to replace it
            failing.append('a dangling source is in the way')
        if not installed.is_dir():
            failing.append('installed-source is missing')
        elif any((installed / entry).is_symlink() for entry in MOVED_ENTRIES):
            failing.append('installed-source has a symlink that would change meaning when moved')
        tree = installed
    source = source_state(tree)
    # [[ $(source_state) == "$(jget "$BASELINE" source)" ]]: jget's print loses trailing newlines and NULs too.
    if source != str(baseline['source']).replace('\0', '').rstrip('\n'):
        failing.append('old source not in place')
    return failing, source, tree


def json_record(data):
    """state()'s load() of a Fri release record: {} when unreadable, None when not an object (state() fails)."""
    try:
        value = json.loads((data or b'').decode()) if data is not None else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else None


def shell_text(value):
    """What $(jget …) yields for a JSON value: print()'s text, less NUL bytes and trailing newlines."""
    return str(value).replace('\0', '').rstrip('\n')


def verify_mirror(step, state):
    """The record checks of verify old, which ends rollback-gene, rollback-fri and resume-old, on the state the
    rollback leaves, read as state() reads it. Health and idleness are the live release's own."""
    record, baseline = step['record'], step['baseline']
    old, settings = record['images']['old'], state['settings']
    fri_record = json_record(state['fri release.json'])
    checks = {
        'controller_image': state['controller'] == old['runtime'][1],
        'red_image': state['red'] == old['runtime'][1], 'fri_image': state['fri'] == old['fri'][1],
        'gene_env': gene_pins(state['release.env'])
                    == {'GENE_RELEASE_SHA': record['old_gene'], 'GENE_RUNTIME_IMAGE': old['runtime'][0]},
        'worker_pin': worker_pin(state['worker.env'], record['gene'], record['old_gene']) == old['worker'][0],
        'source': state['source'] == baseline['source'],
        'fri_env': fri_pins(state['fri release.env']) == {'FRI_RELEASE_SHA': record['old_fri'],
                                                          'FRI_RUNTIME_IMAGE': old['fri'][0]},
        'fri_record': fri_record is not None and fri_record.get('sha') == record['old_fri'],
        'release_settings': (settings.get('active_sha'), settings.get('previous_sha'))
                            == (record['old_gene'], baseline['settings']['previous_sha']),
        'fri_active_sha': settings.get('fri_active_sha') in (record['old_fri'], baseline['settings'].get('fri_active_sha')),
        # The baseline half of gene_health and fri_health: only values of the type state() reports can match.
        'gene_health': baseline_health(baseline, 'gene'), 'fri_health': baseline_health(baseline, 'fri')}
    return [name for name, passed in checks.items() if not passed]


def recover(kind, step, state, root_text):
    """One rollback-gene ('gene') or rollback-fri ('fri') on a simulated state: its first checks, what it
    restores, and verify old. Returns (why it would refuse or stop, or None, the state it leaves)."""
    what = 'rollback-gene' if kind == 'gene' else 'rollback-fri'
    failing = pair_mirror(kind, step, state, root_text)
    if failing:
        return f"its {what} would refuse ({', '.join(failing)})", None
    record, saved, baseline = step['record'], step['saved'], step['baseline']
    old = record['images']['old']
    state = dict(state, settings=dict(state['settings']))
    # restore_old_fri: nothing to do once the old Fri's pins, record, setting and image are all in place.
    if not (state['fri release.env'] == saved['fri-release.env'] and state['fri release.json'] == saved['fri-release.json']
            and state['settings'].get('fri_active_sha') in (None, record['old_fri']) and state['fri'] == old['fri'][1]):
        state.update({'fri release.env': saved['fri-release.env'], 'fri release.json': saved['fri-release.json'],
                      'fri': old['fri'][1]})
        state['settings']['fri_active_sha'] = record['old_fri']
    if kind == 'gene':
        state.update({'release.env': saved['release.env'], 'worker.env': saved['worker.env'],
                      'controller': old['runtime'][1], 'red': old['runtime'][1]})
        failing, source, tree = restore_source(step, state)
        if failing:
            return f"its {what} would stop restoring the source ({', '.join(failing)})", None
        state.update(source=source, source_tree=tree)
        # prev=$(jget "$BASELINE" settings.previous_sha), then settings_transition stores that text.
        state['settings'].update(active_sha=record['old_gene'], previous_sha=shell_text(baseline['settings']['previous_sha']))
    failing = verify_mirror(step, state)
    if failing:
        return f"its {what} would stop at verify old ({', '.join(failing)})", None
    return None, state


def after_deployer_rollback(root, step, state, root_text):
    """The owner's rollback of a cog-only release through the deployer, or None when it would refuse.

    store.rollback proposes (previous_sha, active_sha); the deployer needs that release's cogs and rewrites
    release.env keeping the runtime image (deploy.activate, GENE_ROOT defaulting to the host root); the
    controller then records active_sha = the proposal and previous_sha = the release it replaced.
    """
    settings = state['settings']
    values = release_values(state['release.env'])
    if (values is None or (settings.get('previous_sha'), settings.get('active_sha')) != (step['previous'], step['sha'])
            or not (root / 'releases' / step['previous'] / 'cogs/gene/__init__.py').is_file()):
        return None
    image = values.get('GENE_RUNTIME_IMAGE', 'gene-runtime:bootstrap')
    release = f"GENE_RELEASE_SHA={step['previous']}\nGENE_ROOT={root_text}\nGENE_RUNTIME_IMAGE={image}\n".encode()
    return dict(state, settings=dict(settings, active_sha=step['previous'], previous_sha=step['sha']),
                **{'release.env': release})


def baseline_of(root, record, incomplete):
    """baseline.json, the pre-batch state rollback-gene restores and verify old compares against."""
    path = root / 'staging' / record['name'] / 'baseline.json'
    if not path.is_file() or path.is_symlink():
        raise incomplete('its baseline.json is missing')
    try:
        baseline = json.loads(path.read_text())
    except ValueError:
        raise incomplete('its baseline.json is unreadable') from None
    # The types baseline() writes, from state(): the controller's settings as text, str(guild_id), and the
    # health files' str(status) presence. A value that only prints alike fails the rollback that reads it.
    settings = baseline.get('settings') if isinstance(baseline, dict) else None
    if (not isinstance(settings, dict) or settings.get('active_sha') != record['old_gene']
            or not is_sha(settings.get('previous_sha'))
            or not isinstance(settings.get('fri_active_sha', ''), (str, type(None)))
            or baseline.get('source') not in ('unmarked', record['old_gene'])):
        raise incomplete('its baseline.json is not the old release')
    for side in ('gene', 'fri'):
        if not baseline_health(baseline, side):
            raise incomplete(f'its baseline.json has no {side} guild and presence as the script writes them')
    return baseline


def baseline_health(baseline, side):
    """Whether verify old and the probes can ever match the baseline's guild and presence: state() reports
    str(guild_id) and both health files write str(status), so only strings can match."""
    health = baseline.get(side)
    return (isinstance(health, dict) and isinstance(health.get('guild_id'), str) and health['guild_id'] != 'None'
            and isinstance(health.get('presence'), str))


def snapshot(root, policy):
    records = batch_records(root)
    for record in records:
        if in_flight(record):
            raise Busy(f"Batch {record['name']} is mid-release or mid-rollback; retention waits until it resumes")
    images, containers = docker_inventory()
    by_tag = {tag: iid for iid, row in images.items() for tag in row['tags']}
    settings, jobs, deployments, cog_jobs = controller_refs(root)
    env = read_env(root / 'release.env')
    worker = read_env(root / 'secrets/worker.env')
    if env.get('GENE_RELEASE_SHA') != settings['active_sha']:
        raise ValueError('Controller and configured release disagree')
    reasons, refs = {}, {}

    def hold(iid, reason):
        if iid and reason not in reasons.setdefault(iid, []):
            reasons[iid].append(reason)

    def want(ref, reason):
        if reason not in refs.setdefault(ref, []):
            refs[ref].append(reason)
    current_refs = [env['GENE_RUNTIME_IMAGE'], worker['GENE_WORKER_IMAGE']]
    functional = set(current_refs + process_refs())
    for ref in sorted(functional):
        want(ref, CURRENT)
    for ref in policy['keep_images']:
        want(ref, HOLD)
    config_files = sorted(set(root.glob('*.env')) | set((root / 'secrets').glob('*.env'))
                          | set((root / 'pending').glob('*/release.env'))
                          | set((root / 'pending').glob('*/worker.env'))
                          | set((root / 'pending').glob('*/audio-release.env')))
    config = {}
    for path in config_files:
        values = read_env(path)
        config[str(path)] = values
        for key, value in values.items():
            if key.endswith('_IMAGE'):
                want(value, f'{CONFIGURED} ({path.relative_to(root)})')
    # Audio is independent. Resolve both pins even though its repository is never deleted.
    audio = dict(line.split('=', 1) for line in (root / 'audio-release.env').read_text().splitlines()
                 if line and not line.startswith('#') and '=' in line)
    want('gene-audio:' + audio['GENE_AUDIO_SHA'], AUDIO)
    want('gene-audio-resolver:' + audio.get('GENE_AUDIO_RESOLVER_SHA', audio['GENE_AUDIO_SHA']), AUDIO)
    live = [record for record in records if record['live']]
    # The images batch-release.sh cleanup keeps, so the two tools never disagree about a rollback.
    resumed = sorted((r for r in records if 'resumed' in r['words']), key=lambda r: r['at'], reverse=True)
    for reason, chosen in ((RECENT, resumed[:KEEP_RELEASES]), (UNFINISHED, [r for r in records if unfinished(r)])):
        for record in chosen:
            for entries in record['images'].values():
                for tag, iid in entries.values():
                    for image in {iid, by_tag.get(tag)} & images.keys():
                        hold(image, f"{reason} ({record['name']})")
    protected_shas = {settings['active_sha'], settings.get('previous_sha', ''), *jobs}
    past = {}

    def superseded(ref, at, evidence):
        if VERSION_TAG.fullmatch(ref) and ref in by_tag:
            record = past.setdefault(by_tag[ref], {'retired_at': at, 'tags': [], 'evidence': evidence})
            record['retired_at'] = max(record['retired_at'], at)
            if ref not in record['tags']:
                record['tags'].append(ref)
    # Successful maintainer backup records cover releases between timer runs.
    # An unreferenced build alone never enters this lifecycle history.
    for job, deployed, previous, at in deployments:
        if job in cog_jobs:
            continue  # Cog jobs never replace images, even when revisiting a maintainer SHA.
        for backup in (root / 'backups' / ('maintainer-' + deployed), root / 'backups' / job):
            if not (backup / 'release.env').is_file() or not (backup / 'worker.env').is_file():
                continue
            release, old_worker = read_env(backup / 'release.env'), read_env(backup / 'worker.env')
            if release.get('GENE_RELEASE_SHA') != previous:
                raise ValueError('Historical backup disagrees with successful deployment')
            for ref in (release['GENE_RUNTIME_IMAGE'], old_worker['GENE_WORKER_IMAGE']):
                superseded(ref, at, 'Superseded in successful deployment history')
            break
    # A batch that went into service replaced its old Gene images when it resumed, if its record checks out.
    for record in live:
        for tag in batch_evidence(root, record, by_tag):
            superseded(tag, record['at'], 'Superseded in batch release history (' + record['name'] + ')')
    fri_pins = {}
    if records:
        # Fri images are batch cleanup's to remove, but the batch records must still lead back from the live Fri.
        fri_pins = read_pins(FRI_ROOT / 'release.env', {'FRI_RELEASE_SHA', 'FRI_RUNTIME_IMAGE'})
    rollback_files, lineage, recovery = {}, [], []
    cursor, before, complete, steps = settings['active_sha'], float('inf'), 0, 0
    # The release pair a rollback lands on, carried down the chain: its Fri, and the images it runs.
    fri_cursor, carried = fri_pins.get('FRI_RELEASE_SHA') if records else None, None
    # Preserve two complete rollback configurations, rather than guessing by image age. Each step back is
    # the latest earlier release of the cursor, from either history; a tie between them is ambiguous.
    while complete < ROLLBACK_DEPTH:
        events = sorted([(row[3], row) for row in deployments if row[1] == cursor and row[3] < before]
                        + [(r['at'], r) for r in live if r['gene_changes'] and r['gene'] == cursor and r['at'] < before],
                        key=lambda event: event[0], reverse=True)
        if not events:
            raise ValueError('Missing successful rollback lineage for ' + cursor)
        if len(events) > 1 and events[0][0] == events[1][0]:
            raise ValueError('Ambiguous rollback lineage for ' + cursor)
        at, event = events[0]
        batch = isinstance(event, dict)
        previous = event['old_gene'] if batch else event[2]
        # Fri-only releases on this Gene come back first, newest first, by rollback-fri: each must be complete
        # and continue the pair, or this level's rollback-gene can never start.
        stacked = sorted((r for r in live if not r['gene_changes'] and r['gene'] == cursor and at < r['at'] < before),
                         key=lambda r: r['at'], reverse=True)
        for fri_only, after in zip(stacked, stacked[1:] + [None]):
            name = fri_only['name']
            if after is not None and after['at'] == fri_only['at']:
                raise ValueError('Ambiguous Fri-only history for ' + cursor)
            if fri_cursor is not None and fri_only['fri'] != fri_cursor:
                raise ValueError(f"Batch {name} Fri {fri_only['fri'][:7]} does not continue Fri {fri_cursor[:7]}")
            if carried is not None and fri_only['images']['new'] != carried:
                raise ValueError(f'Batch {name} images are not the ones the release after it replaced')
            recovery.append(('fri', fri_step(root, fri_only, by_tag, previous, cursor if batch else None,
                                             (event['images']['new']['runtime'], event['images']['new']['worker'])
                                             if batch else None)))
            for entries in fri_only['images'].values():
                for _, iid in entries.values():
                    hold(iid, f'{ROLLBACK} (batch {name})')
            rollback_files[name] = {'backup': fri_only['backup'], 'fri': fri_only['old_fri']}
            lineage.append({'sha': cursor, 'previous': cursor, 'record': 'batch ' + name, 'fri': fri_only['fri'],
                            'previous_fri': fri_only['old_fri'], 'fri_only': True})
            fri_cursor, carried, before = fri_only['old_fri'], fri_only['images']['old'], fri_only['at']
        if batch:
            name = event['name']
            if steps == 0 and previous != settings.get('previous_sha'):
                raise ValueError('Controller rollback pointer disagrees with batch ' + name)
            new = event['images']['new']
            if complete == 0 and current_refs != [new['runtime'][0], new['worker'][0]]:
                raise ValueError('Live Gene image pins disagree with batch ' + name)
            if fri_cursor is not None and event['fri'] != fri_cursor:
                if not lineage:
                    raise ValueError(f'Live Fri is not the release batch {name} put in service, and no complete '
                                     'Fri-only release leads back to it')
                raise ValueError(f"Batch {name} is not a complete rollback configuration: the Fri {fri_cursor[:7]} "
                                 f"its later releases roll back to does not continue its Fri {event['fri'][:7]}")
            if carried is not None and new != carried:
                raise ValueError(f'Batch {name} is not a complete rollback configuration: its new images are not '
                                 'the ones the release after it replaced')
            step = batch_step(root, event, by_tag)
            recovery.append(('gene', step))
            old_fri, restores = event['old_fri'], step['baseline']['settings']['previous_sha']
            for entries in event['images'].values():
                for _, iid in entries.values():
                    hold(iid, f'{ROLLBACK} (batch {name})')
            rollback_files[name] = {'backup': event['backup'], 'gene': previous, 'fri': old_fri}
            lineage.append({'sha': cursor, 'previous': previous, 'record': 'batch ' + name,
                            'fri': event['fri'], 'previous_fri': old_fri, 'restores_previous': restores})
            protected_shas.add(previous)
            fri_cursor, carried = old_fri, event['images']['old']
            cursor, before = previous, at
            complete += 1
            steps += 1
            continue
        fri_cursor = carried = None  # Older installs recorded no Fri release.
        job, _, previous, at = event
        if steps == 0 and previous != settings.get('previous_sha'):
            raise ValueError('Controller rollback pointer disagrees with deployment history')
        if not SHA.fullmatch(previous):
            raise ValueError('Invalid rollback lineage')
        choices = ([root / 'backups' / job] if job in cog_jobs else
                   [root / 'backups' / ('maintainer-' + cursor), root / 'backups' / job])
        backup = next((p for p in choices if (p / 'release.env').is_file()
                       and (p / 'worker.env').is_file()), None)
        if backup is None:
            # Trusted feature/repair/rollback jobs preserve the installed images. Their
            # backups contain Red/controller state, not a replaced control plane.
            backup = root / 'backups' / job
            if (cog_jobs.get(job) != (cursor, previous)
                    or any(not (backup / name).is_file() for name in ('controller.sqlite3', 'red.tar.gz'))
                    or not (root / 'releases' / previous / 'cogs').is_dir()):
                raise ValueError('Missing complete rollback image configuration for ' + cursor)
            rollback_files[str(backup)] = {'cog_sha': cursor, 'previous_sha': previous}
            recovery.append(('cog', {'sha': cursor, 'previous': previous, 'job': job}))
            lineage.append({'sha': cursor, 'previous': previous, 'record': 'cog job ' + job})
            protected_shas.add(previous)
            cursor, before = previous, at
            steps += 1
            continue
        missing = [] if (backup / 'installed-source').is_dir() else ['installed-source']
        missing += [name for name in ('controller.sqlite3', 'red.tar.gz', 'fri.tar.gz')
                    if not (backup / name).is_file()]
        if missing:
            raise ValueError(f'Rollback backup {backup.name} for {cursor[:7]} is incomplete: missing '
                             + ', '.join(missing))
        release, old_worker = read_env(backup / 'release.env'), read_env(backup / 'worker.env')
        if (release.get('GENE_RELEASE_SHA') != previous
                or not (root / 'releases' / previous / 'cogs').is_dir()):
            raise ValueError('Rollback source/release configuration is incomplete')
        rollback_files[str(backup)] = [release, old_worker]
        for ref in (release['GENE_RUNTIME_IMAGE'], old_worker['GENE_WORKER_IMAGE']):
            want(ref, f'{ROLLBACK} (deployment {job})')
        recovery.append(('legacy', None))
        lineage.append({'sha': cursor, 'previous': previous, 'record': 'deployment ' + job})
        protected_shas.add(previous)
        cursor, before = previous, at
        complete += 1
        steps += 1
    # Rolling a batch back restores baseline.json's previous_sha, which the next level's rollback then checks.
    gene_levels = [step for step in lineage if not step.get('fri_only')]
    for step, below in zip(gene_levels, gene_levels[1:]):
        if 'restores_previous' in step and step['restores_previous'] != below['previous']:
            raise ValueError(f"Batch {step['record'].removeprefix('batch ')} is not a complete rollback configuration: "
                             f"its baseline.json previous_sha {step['restores_previous']} is not the next rollback "
                             f"level's {below['previous']}")
    # Each batch's recovery must pass the release script's own first checks on the state the recoveries above
    # it leave: the live state for the first, then what each rollback restores. A legacy install's rollback
    # has no script to mirror, so nothing after it is simulated; a batch below one can't be.
    if any(kind in ('gene', 'fri') for kind, _ in recovery):
        state, root_text = live_pair_state(root, settings), str(root)
        for kind, step in recovery:
            if kind == 'legacy':
                state = None
            elif state is None:
                raise ValueError(f"Batch {step['record']['name']} sits below a legacy install, whose recovery has no "
                                 'release script to check')
            elif kind == 'cog':
                state = after_deployer_rollback(root, step, state, root_text)
                if state is None:
                    raise ValueError(f"The owner's rollback of cog job {step['job']} through the deployer would refuse, "
                                     'so the batch levels below it cannot be reached')
            else:
                why, state = recover(kind, step, state, root_text)
                if why:
                    raise ValueError(f"Batch {step['record']['name']} is not a complete rollback configuration: on the "
                                     f'state the rollbacks above it leave, {why}')
    if records:
        if live:
            latest = max(live, key=lambda record: record['at'])
            tag, iid = latest['images']['new']['fri']
            if fri_pins != {'FRI_RELEASE_SHA': latest['fri'], 'FRI_RUNTIME_IMAGE': tag} or by_tag.get(tag) != iid:
                raise ValueError(f"Live Fri is not the release batch {latest['name']} put in service; reconcile it first")
        if fri_pins.get('FRI_RUNTIME_IMAGE'):
            want(fri_pins['FRI_RUNTIME_IMAGE'], FRI_LIVE)
    if (root / 'deploy-result.json').exists():
        raise Busy('A deployment completion receipt needs reconciliation')
    resolved = {}
    for image in containers.values():
        hold(image, CONTAINER)
    for ref, why in refs.items():
        iid = ref if IMAGE_ID.fullmatch(ref) else by_tag.get(ref)
        if iid not in images:
            # An explicit ID/digest hold that the listing doesn't show must resolve through inspection
            # rather than being treated as absent.
            if not REFERENCE.fullmatch(ref):
                raise ValueError('Invalid configured image reference')
            metadata = image_metadata(ref)
            iid = metadata['id']
            if not IMAGE_ID.fullmatch(iid) or (IMAGE_ID.fullmatch(ref) and ref != iid):
                raise ValueError('Configured image identity is invalid')
            images[iid] = {'tags': sorted(metadata.get('tags') or [])}
        resolved[ref] = iid
        for reason in why:
            hold(iid, reason)
    current_ids = {resolved[ref] for ref in functional}
    current_tags = {iid: sorted(ref for ref in functional if by_tag.get(ref) == iid
                                and VERSION_TAG.fullmatch(ref)) for iid in current_ids}
    for iid, row in images.items():
        if any((m := VERSION_TAG.fullmatch(t)) and m[1] in protected_shas for t in row['tags']):
            hold(iid, PENDING)
    # The batch lock keeps batch-release.sh out; this also catches staging written any other way meanwhile.
    if batch_records(root) != records:
        raise Busy('Batch records changed during discovery; retry in the next window')
    batches = {r['name']: {'marks': sorted(r['marks']), 'images': r['images'], 'backup': r['backup']}
               for r in records}
    guard = {'settings': settings, 'jobs': jobs, 'config': config, 'rollback': rollback_files,
             'containers': containers, 'functional': sorted(functional), 'resolved': resolved,
             'policy': policy, 'batches': batches, 'fri': fri_pins, 'lineage': lineage}
    fingerprint = hashlib.sha256(json.dumps(guard, sort_keys=True).encode()).hexdigest()
    return {'images': images, 'protected': set(reasons), 'reasons': reasons, 'current': current_ids,
            'current_tags': current_tags, 'past': past, 'fingerprint': fingerprint, 'lineage': lineage,
            'live': {'gene': settings['active_sha'], 'fri': fri_pins.get('FRI_RELEASE_SHA')}}


def retirement_record(record):
    if (not isinstance(record, dict) or not isinstance(record.get('retired_at'), (int, float))
            or not record['retired_at'] > 0 or not record.get('evidence')
            or not isinstance(record.get('tags'), list) or not record['tags']
            or any(not VERSION_TAG.fullmatch(tag) for tag in record['tags'])):
        raise ValueError('Invalid retirement evidence')
    return record


def observe(state, view, now):
    retired = state.setdefault('retired', {})
    seen = state.setdefault('seen_current', {})
    for iid, record in view.get('past', {}).items():
        if iid not in retired:
            retired[iid] = record
        else:
            retired[iid]['retired_at'] = max(retired[iid]['retired_at'], record['retired_at'])
            retired[iid]['tags'] = sorted(set(retired[iid]['tags']) | set(record['tags']))
    # Holds and auxiliary configured functions are reactivation too. This only renews
    # existing evidence; protecting an unknown candidate never classifies it as retired.
    for iid in set(state.get('protected_last', [])) - view['protected']:
        if iid in retired:
            retired[iid]['retired_at'] = now
        if iid in state.get('retired_parents', {}):
            state['retired_parents'][iid]['retired_at'] = now
    for iid in state.get('active_ids', []):
        tags = seen[iid]
        if iid not in view['current']:
            # Only a previously configured version can automatically become retired.
            if tags and all(VERSION_TAG.fullmatch(t) for t in tags):
                retired[iid] = {'retired_at': now, 'tags': tags,
                                'evidence': 'Previously configured image was superseded'}
    for iid in view['current']:
        seen[iid] = view['current_tags'][iid]
        # Reusing an old version cancels retirement; a future switch starts a fresh grace period.
        retired.pop(iid, None)
    state['active_ids'] = sorted(view['current'])
    state['protected_last'] = sorted(view['protected'])
    return state


def plan(view, state, now):
    delete, keep = [], {}
    for iid, row in sorted(view['images'].items()):
        tags = row['tags']
        record = state['retired'].get(iid)
        if iid in view['protected']:
            why = view.get('reasons', {}).get(iid) or ['configured, container, pending job, hold, or rollback reference']
            reason = '; '.join(sorted(why, key=lambda r: next(
                (n for n, p in enumerate(PRIORITY) if r.startswith(p)), len(PRIORITY))))
        elif not tags or any(not VERSION_TAG.fullmatch(tag) for tag in tags):
            reason = OUTSIDE
        elif record is None:
            reason = 'no evidence that this image was superseded'
        elif not set(tags) <= set(retirement_record(record)['tags']):
            reason = 'new or unclassified alias requires maintainer review'
        elif now - record['retired_at'] < GRACE:
            reason = '24-hour supersession grace period'
        else:
            delete.append({'id': iid, 'tags': tags, 'evidence': record['evidence'],
                           'retired_at': record['retired_at']})
            continue
        keep[iid] = {'tags': tags, 'reason': reason}
    # Workers inherit runtime images; remove eligible child versions first.
    delete.sort(key=lambda row: (not any(t.startswith('gene-worker:') for t in row['tags']), row['id']))
    return {'delete': delete, 'keep': keep, 'fingerprint': view['fingerprint']}


def image_metadata(reference):
    template = '{"id":{{json .Id}},"parent":{{json (index . "Parent")}},"tags":{{json .RepoTags}}}'
    metadata = json.loads(command('docker', 'image', 'inspect', '--format', template, reference))
    metadata['parent'] = metadata.get('parent') or ''
    return metadata


def image_details(ids):
    """Parent, size and layer chain of each image, for the plan's ancestry and byte estimate."""
    template = ('{"id":{{json .Id}},"parent":{{json (index . "Parent")}},"size":{{json .Size}},'
                '"layers":{{json .RootFS.Layers}}}')
    details = {}
    for start in range(0, len(ids), 100):
        for line in command('docker', 'image', 'inspect', '--format', template, *ids[start:start + 100]).splitlines():
            row = json.loads(line)
            layers = row.get('layers') or []
            if (not IMAGE_ID.fullmatch(row['id']) or type(row['size']) is not int or row['size'] < 0
                    or not all(isinstance(layer, str) for layer in layers)):
                raise ValueError('Docker returned invalid image details')
            details[row['id']] = {'parent': row.get('parent') or '', 'size': row['size'], 'layers': layers}
    return details


def estimate_bytes(details, removed):
    """Layer bytes the removals free, never counting a shared layer twice.

    Images share a layer only at the same place in their chain, so the layers form a tree of chain
    prefixes. An image's size fixes the cumulative size where its chain ends. A removal frees the
    prefixes that no kept image uses. Where no image ends at a prefix its size is unknown, so the
    result is a range, a single number once every prefix is known (as with the classic builder).
    """
    def coarse(why):
        return {'lower_bytes': 0, 'upper_bytes': sum(details[i]['size'] for i in removed if i in details),
                'exact': False, 'method': f'coarse: {why}; the upper bound counts every layer of each removed image'}
    if not removed <= details.keys():
        return coarse('some planned images could not be inspected')
    parent, known, kept, freed = {}, {'': 0}, set(), set()
    for image, row in details.items():
        node = ''
        for layer in row['layers']:
            child = hashlib.sha256(f'{node}/{layer}'.encode()).hexdigest()
            parent[child] = node
            node = child
        if known.setdefault(node, row['size']) != row['size']:
            return coarse('images with the same layers report different sizes')
        target = freed if image in removed else kept
        while node and node not in target:
            target.add(node)
            node = parent[node]
    freed -= kept
    children = {}
    for node, above in parent.items():
        children.setdefault(above, []).append(node)
    low, order = {'': 0}, ['']
    for node in order:
        for child in children.get(node, []):
            if known.get(child, low[node]) < low[node]:
                return coarse('image sizes are not cumulative along shared layers')
            low[child] = known.get(child, low[node])
            order.append(child)
    high = {}
    for node in reversed(order):
        high[node] = min([known.get(node, math.inf)] + [high[child] for child in children.get(node, [])])
    # sum(size) = sum(P(n) - P(parent)): leaves are known; a branch point counts negatively, so the range
    # takes each unknown prefix at its lowest or highest consistent cumulative size.
    lower = upper = 0
    frontier = {}
    for node in freed:
        weight = 1 - len(children.get(node, []))
        upper += weight * (high[node] if weight > 0 else low[node])
        lower += weight * (low[node] if weight > 0 else high[node])
        if parent[node] not in freed:
            frontier[parent[node]] = frontier.get(parent[node], 0) + 1
    for node, count in frontier.items():
        upper -= count * low[node]
        lower -= count * high[node]
    unknown = sum(1 for node in freed | set(frontier) if node not in known)
    return {'lower_bytes': lower, 'upper_bytes': upper, 'exact': lower == upper,
            'method': f'layer chains of {len(details)} images; {unknown} freed or boundary layer prefixes '
                      'have no image ending there, so their sizes are bounded rather than known'}


def describe(result, details, why_unavailable=None):
    """The plan's account: why each image stays and what the removals free."""
    removed = {row['id'] for row in result['delete']} | {row['id'] for row in result['delete_parents']}
    kept_ancestors, users = set(), {}
    for image, row in (details or {}).items():
        if image in removed:
            continue
        parent, seen = row['parent'], {image}
        while parent and parent not in seen:
            seen.add(parent)
            kept_ancestors.add(parent)
            users.setdefault(parent, image)
            parent = details.get(parent, {}).get('parent', '')
    for image, row in result['keep'].items():
        if row['reason'] == OUTSIDE and not row['tags']:
            row['reason'] = KEPT_PARENT if image in kept_ancestors else UNCLASSIFIED
        elif row['reason'] == OUTSIDE and all(FRI_TAG.fullmatch(tag) for tag in row['tags']):
            row['reason'] = FRI_OWNED
    for row in result['delete'] + result['delete_parents']:
        if row['id'] in kept_ancestors:
            row['docker_keeps'] = f"kept image {users[row['id']]} is built on it, so Docker refuses or only untags"
    summary = {}
    for row in result['keep'].values():
        category = row['reason'].split('; ')[0].split(' (')[0]
        summary[category] = summary.get(category, 0) + 1
    result['keep_summary'] = dict(sorted(summary.items()))
    notes = ['Assumes every planned removal succeeds. Docker refuses an image a container or a kept child '
             'image still uses, which frees less.',
             "Bytes are Docker's uncompressed layer sizes. With the containerd image store the compressed "
             'blobs are freed too, so disk use can drop by more; measure df before and after.']
    if details is None:
        result['bytes'] = {'lower_bytes': None, 'upper_bytes': None, 'exact': False,
                           'method': 'unavailable: ' + why_unavailable, 'notes': notes}
    else:
        result['bytes'] = estimate_bytes(details, removed) | {'notes': notes}


def record_parents(state, row):
    """Record only unnamed ancestors of an exact, already approved retired Gene image."""
    parent = image_metadata(row['id'])['parent']
    visited = {row['id']}
    for _ in range(100):
        if not parent:
            return
        if not IMAGE_ID.fullmatch(parent) or parent in visited:
            raise ValueError('Invalid Docker image ancestry')
        visited.add(parent)
        metadata = image_metadata(parent)
        if metadata['id'] != parent:
            raise ValueError('Docker ancestor identity changed')
        if metadata.get('tags'):
            return  # Never remove a tagged parent/base/function image.
        state.setdefault('retired_parents', {}).setdefault(parent, {
            'from_image': row['id'], 'retired_at': row['retired_at'],
            'evidence': 'Unnamed ancestor of a verified retired Gene runtime/worker image'})
        parent = metadata['parent']
    raise ValueError('Docker ancestry exceeded inspection limit')


def plan_parents(view, state, result, now):
    result['delete_parents'] = []
    for iid, record in state.get('retired_parents', {}).items():
        if (not IMAGE_ID.fullmatch(iid) or not IMAGE_ID.fullmatch(record['from_image'])
                or record['from_image'] not in state['retired'] or not record.get('evidence')
                or record['from_image'] in view['protected']):
            continue
        if iid in view['protected']:
            continue
        try:
            metadata = image_metadata(iid)
        except subprocess.CalledProcessError:
            continue  # Already collected or unavailable; never broaden the request.
        if metadata['id'] != iid or metadata.get('tags'):
            continue
        if now - record['retired_at'] < GRACE:
            result['keep'][iid] = {'tags': [], 'reason': '24-hour ancestor reactivation grace period'}
            continue
        result['delete_parents'].append({'id': iid, **record})
        result['keep'].pop(iid, None)


def window_closed(result, exception):
    """Checked right before each removal: once the window has closed, or too little of this run's hold on
    batch-release.lock is left for a removal to finish, the rest waits for the next window."""
    if not exception and not window_open(time.time()):
        result['deferred'] = 'The maintenance window closed; the remaining removals wait for the next window'
    elif DEADLINE is not None and DEADLINE - monotonic() < REMOVAL_MARGIN:
        result['deferred'] = (f'This run is near the end of its {LOCK_BUDGET // 60}-minute hold on batch-release.lock; '
                              'the remaining removals wait for the next window')
    return 'deferred' in result


def remove_parents(root, state, fingerprint, result, exception):
    """No implicit pruning: attempt only exact ancestors listed in the saved plan."""
    for row in result['delete_parents']:
        iid = row['id']
        if window_closed(result, exception):
            return
        fresh = snapshot(root, read_json(root / 'maintenance/image-retention-policy.json'))
        if fresh['fingerprint'] != fingerprint:
            raise ValueError('References changed during ancestor cleanup')
        if iid in fresh['protected']:
            continue
        try:
            metadata = image_metadata(iid)
        except subprocess.CalledProcessError:
            continue
        if metadata['id'] != iid or metadata.get('tags'):
            continue
        if window_closed(result, exception):
            return
        try:
            command('docker', 'image', 'rm', '--no-prune', iid)
        except subprocess.CalledProcessError:
            continue  # Docker preserves parents with dependent children/containers.
        result.setdefault('removed_parent_ids', []).append(iid)
        state['retired_parents'].pop(iid, None)
        save_json(root / 'maintenance/image-retention-state.json', state)
        save_json(root / 'maintenance/image-retention-result.json', result)


def run(root, apply=False, exception='', progress=None):
    """Discovery and removal, all under batch-release.sh's lock."""
    with batch_lock(root):
        return retain(root, apply, exception, progress)


def retain(root, apply, exception, progress):
    folder = root / 'maintenance'
    policy = read_json(folder / 'image-retention-policy.json')
    if (policy.get('version') != 1 or not isinstance(policy.get('keep_images'), list)
            or not isinstance(policy.get('apply_enabled', False), bool)):
        raise ValueError('Unsupported retention policy')
    seed = policy['retired_images']
    for iid, record in seed.items():
        if not IMAGE_ID.fullmatch(iid):
            raise ValueError('Invalid seeded image ID')
        retirement_record(record)
    state_path = folder / 'image-retention-state.json'
    state = read_json(state_path) if state_path.exists() else {'version': 1, 'retired': seed.copy()}
    if state.get('version') != 1:
        raise ValueError('Unsupported retention state')
    for iid, record in seed.items():
        if iid not in state['retired']:
            state['retired'][iid] = record.copy()
        else:
            # A later explicit classification may add reviewed aliases, but cannot erase
            # a newer observed reactivation/supersession grace period.
            current = state['retired'][iid]
            current['retired_at'] = max(current['retired_at'], record['retired_at'])
            current['tags'] = sorted(set(current['tags']) | set(record['tags']))
    for iid, record in state['retired'].items():
        if not IMAGE_ID.fullmatch(iid):
            raise ValueError('Invalid retired image ID')
        retirement_record(record)
    view = snapshot(root, policy)
    now = time.time()
    state = observe(state, view, now)
    # Classic Docker may untag a runtime but retain its exact image object for worker
    # children. Carry only IDs that this tool itself retired/untagged into later plans.
    managed_ids = {row['id'] for key in ('removed', 'removal_intents')
                   for row in state.get(key, {}).values()}
    for iid in managed_ids & state['retired'].keys() & view['images'].keys():
        if not view['images'][iid]['tags']:
            state.setdefault('retired_parents', {}).setdefault(iid, {
                'from_image': iid, 'retired_at': state['retired'][iid]['retired_at'],
                'evidence': 'Verified retired Gene version retained after managed untagging'})
    result = plan(view, state, now)
    result['removed_tags'] = []
    for row in result['delete']:
        record_parents(state, row)
    plan_parents(view, state, result, now)
    # Deleting needs the policy's apply_enabled as well as --apply, so a newly installed or repaired
    # tool plans until a maintainer has reviewed what it would remove.
    deleting = apply and policy.get('apply_enabled') is True
    result.update(version=2, planned_at=stamp(now)['at'],
                  maintenance_release=RELEASE, live=view['live'], lineage=view['lineage'],
                  deletion='enabled' if deleting else ('disabled in the policy' if apply else 'plan only'))
    try:
        ids = sorted(view['images'].keys() | {row['id'] for row in result['delete_parents']})
        describe(result, image_details(ids))
    except Busy:
        raise
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError) as exc:
        describe(result, None, f'image inspection failed ({type(exc).__name__})')
    check_deadline()  # a plan from a run past its hold is not saved; the next window plans again
    save_json(folder / 'image-retention-plan.json', result)
    if progress is not None:
        progress['result'] = result  # so an interrupted removal run still logs what it removed
    if deleting:
        save_json(state_path, state)
        for row in result['delete']:
            # Abort on a concurrent operator/configuration/container change. No force, no prune.
            current_policy = read_json(folder / 'image-retention-policy.json')
            fresh = snapshot(root, current_policy)
            if fresh['fingerprint'] != view['fingerprint']:
                raise ValueError('References changed during cleanup; remaining removals aborted')
            eligible = {r['id']: r for r in plan(fresh, state, time.time())['delete']}
            if row['id'] not in eligible or eligible[row['id']]['tags'] != row['tags']:
                raise ValueError('Image tags or protection changed; remaining removals aborted')
            for tag in row['tags']:
                if window_closed(result, exception):
                    break
                fresh = snapshot(root, read_json(folder / 'image-retention-policy.json'))
                if fresh['fingerprint'] != view['fingerprint'] or row['id'] in fresh['protected']:
                    raise ValueError('References changed before tag removal; cleanup aborted')
                # A tag must still resolve to the exact reviewed content before removing it.
                actual = json.loads(command('docker', 'image', 'inspect', tag))[0]['Id']
                if actual != row['id']:
                    raise ValueError('Image was retagged; removal aborted')
                state.setdefault('removal_intents', {})[tag] = {'id': row['id'], 'at': time.time()}
                save_json(state_path, state)
                if window_closed(result, exception):
                    state['removal_intents'].pop(tag)  # nothing was attempted
                    save_json(state_path, state)
                    break
                try:
                    command('docker', 'image', 'rm', '--no-prune', tag)
                except subprocess.CalledProcessError:
                    result.setdefault('skipped_tags', []).append(tag)
                    continue  # A retained child/container may still require the parent image.
                result['removed_tags'].append(tag)
                state.setdefault('removed', {})[tag] = {'id': row['id'], 'at': time.time()}
                state['removal_intents'].pop(tag, None)
                save_json(state_path, state)
                save_json(folder / 'image-retention-result.json', result)
            if result.get('deferred'):
                break
        if not result.get('deferred'):
            remove_parents(root, state, view['fingerprint'], result, exception)
        save_json(folder / 'image-retention-result.json', result)
    return result


def summary(result):
    """Exact IDs and tags, for the status file and the production-action log."""
    return {'deletion': result.get('deletion'), 'delete': {row['id']: row['tags'] for row in result['delete']},
            'delete_parents': [row['id'] for row in result['delete_parents']],
            'removed_tags': result['removed_tags'], 'removed_parents': result.get('removed_parent_ids', []),
            'skipped_tags': result.get('skipped_tags', []), 'deferred': result.get('deferred'),
            'kept_images': len(result['keep']),
            'bytes': {key: result['bytes'].get(key) for key in ('lower_bytes', 'upper_bytes', 'exact')}}


def stamp(now):
    """A record's time: local with its zone, else UTC. An unreadable clock falls back to the system's."""
    moment = local(now)
    if moment is None:
        try:
            moment = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        except (ValueError, OverflowError, OSError):
            moment = dt.datetime.now(dt.timezone.utc)
        return {'at': moment.isoformat(), 'epoch': now if math.isfinite(now) else None,
                'tz': 'UTC (zone unavailable)'}
    return {'at': moment.isoformat(), 'epoch': now, 'tz': ZONE}


def record_status(folder, mode, outcome, detail, result=None):
    """The health record --status reads: the last run, the last success and the failures since."""
    path = folder / 'image-retention-status.json'
    try:
        record = read_json(path) if path.exists() else {}
    except (OSError, ValueError):
        record = {}
    now = time.time()
    record['version'] = 1
    record['last_run'] = stamp(now) | {'mode': mode, 'outcome': outcome, 'detail': detail[:500]}
    if outcome == 'ok':
        record['last_success'] = stamp(now) | {'mode': mode} | summary(result)
        record['consecutive_failures'] = 0
    elif outcome == 'failed':
        record['consecutive_failures'] = record.get('consecutive_failures', 0) + 1
        record['last_failure'] = stamp(now) | {'mode': mode, 'detail': detail[:500]}
    try:
        save_json(path, record)
    except OSError as exc:
        print(f'WARN: the retention status was not written ({type(exc).__name__})', file=sys.stderr)


def systemd_show(unit, *properties):
    try:
        text = command('systemctl', 'show', unit, '--property=' + ','.join(properties))
    except (OSError, subprocess.SubprocessError) as exc:
        return {'error': type(exc).__name__}
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def status(root, now=None):
    """Read-only health for maintainer checks: it takes no lock, writes nothing and starts no container.

    An enabled timer is not health. Healthy means the timer is enabled and active, the last service run and
    the last recorded run succeeded, and a run succeeded within STALE.
    """
    now = time.time() if now is None else now
    try:
        record = read_json(root / 'maintenance/image-retention-status.json')
    except FileNotFoundError:
        record = {}
    except (OSError, ValueError) as exc:
        record = {'unreadable': type(exc).__name__}
    timer = systemd_show('gene-image-retention.timer', 'UnitFileState', 'ActiveState', 'LastTriggerUSec',
                         'NextElapseUSecRealtime')
    service = systemd_show('gene-image-retention.service', 'ActiveState', 'Result', 'ExecMainStatus',
                           'ExecMainExitTimestamp')
    success, last = record.get('last_success') or {}, record.get('last_run') or {}
    succeeded = success.get('epoch') if isinstance(success.get('epoch'), (int, float)) else None
    problems = []
    if 'error' in timer or 'error' in service:
        problems.append(('unknown', 'systemd state is unavailable'))
    elif service.get('Result') != 'success':
        problems.append(('failing', f"the last service run ended with {service.get('Result')} "
                                    f"(exit status {service.get('ExecMainStatus')})"))
    if record.get('unreadable'):
        problems.append(('failing', 'the status file is unreadable'))
    if record.get('consecutive_failures'):
        # A skip (exit 4, a success to systemd) resolves nothing; only a successful run clears a failure.
        failure = record.get('last_failure') or (last if last.get('outcome') == 'failed' else {})
        problems.append(('failing', f"{record['consecutive_failures']} run(s) failed since the last success; the latest: "
                                    + str(failure.get('detail'))))
    if 'error' not in timer and (timer.get('UnitFileState') != 'enabled' or timer.get('ActiveState') != 'active'):
        problems.append(('disabled', 'the timer is not enabled and active'))
    if succeeded is None:
        problems.append(('never-run', 'no successful run is recorded'))
    elif not now - succeeded <= STALE:
        problems.append(('stale', f'no successful run in the last {STALE // 3600} hours'))
    return {'status': problems[0][0] if problems else 'ok', 'healthy': not problems,
            'reasons': [reason for _, reason in problems], 'last_success_at': success.get('at'),
            'last_success_mode': success.get('mode'), 'last_run': last or None,
            'consecutive_failures': record.get('consecutive_failures', 0), 'stale_after_hours': STALE // 3600,
            'timer': timer, 'service': service}


def live_pins(root):
    pins = {}
    for key, path, name in (('gene', root / 'release.env', 'GENE_RELEASE_SHA'),
                            ('fri', FRI_ROOT / 'release.env', 'FRI_RELEASE_SHA')):
        try:
            pins[key] = read_pins(path, {name}).get(name)
        except (OSError, ValueError):
            pins[key] = None
    return pins


def append_log(path, line):
    """One line under the log's lock. The release tools trim it past 1 MiB; this one only appends."""
    while True:
        with open(path, 'a') as log:
            os.fchmod(log.fileno(), 0o600)
            fcntl.flock(log, fcntl.LOCK_EX)
            if os.fstat(log.fileno()).st_ino != os.stat(path).st_ino:
                continue  # Trimmed meanwhile: write to the file that is there now.
            log.write(line + '\n')
            return


def log_action(root, mode, started, outcome, exception='', result=None):
    """This run's production-action log line, beside batch-release.sh's and the deployer's. No secrets."""
    now = time.time()
    when, pins = stamp(now), live_pins(root)
    row = {'at': when['at'], 'local_date': when['at'][:10], 'tz': when['tz'],
           'actor': os.environ.get('SUDO_USER') or os.environ.get('RETENTION_ACTOR') or 'root',
           'trigger': os.environ.get('RETENTION_TRIGGER') or 'maintainer', 'action': 'image-retention ' + mode,
           'reason': 'remove proven superseded Gene images' if mode == 'apply' else 'plan Gene image retention',
           'before': pins, 'after': pins, 'restart_s': None,
           'duration_s': round(now - started, 1) if math.isfinite(now - started) else None,
           'outcome': outcome, 'exception': exception or None, 'maintenance_release': RELEASE}
    if result is not None:
        row['images'] = summary(result)
    try:
        append_log(root / 'production-actions.jsonl', json.dumps(row, sort_keys=True))
    except OSError as exc:
        print(f'WARN: the production-action log was not written ({type(exc).__name__})', file=sys.stderr)


def execute(root, apply=False, exception=''):
    """One run as the timer or a maintainer starts it: window gate, lock, run, status record and log line.

    Returns (exit status, result). A refused run takes no lock and writes nothing but its log line.
    """
    mode, started = ('apply' if apply else 'plan'), time.time()
    if not exception and not window_open(started):
        why = ('outside the maintenance window [04:00, 05:00) America/Los_Angeles, '
               'or the clock or zone is unknown')
        log_action(root, mode, started, 'refused: ' + why)
        print('REFUSED: ' + why + '. Nothing changed; the next window runs it.', file=sys.stderr)
        return REFUSED, None
    folder, progress = root / 'maintenance', {}
    try:
        with (folder / 'image-retention.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Busy('Another image-retention run holds its lock') from None
            result = run(root, apply, exception, progress)
    except Busy as exc:
        record_status(folder, mode, 'skipped', str(exc))
        log_action(root, mode, started, f'skipped: {exc}', exception, progress.get('result'))
        print(f'SKIPPED: {exc}. The next window runs it.', file=sys.stderr)
        return REFUSED, None
    except Exception as exc:
        detail = f'{type(exc).__name__}: {exc}'[:500]
        record_status(folder, mode, 'failed', detail)
        log_action(root, mode, started, 'failed: ' + detail, exception, progress.get('result'))
        raise
    record_status(folder, mode, 'ok', result['deletion'], result)
    log_action(root, mode, started, 'ok' + (' (deferred at the window close)' if result.get('deferred') else ''),
               exception, result)
    return 0, result


def main(argv=None):
    global FRI_ROOT
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, default=Path('/srv/gene'))
    parser.add_argument('--fri-root', type=Path, default=FRI_ROOT, help="Fri's host root, for its live pins")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true',
                      help='Remove only proven retired images; needs the policy\'s "apply_enabled": true')
    mode.add_argument('--status', action='store_true', help='Read-only health; takes no lock, writes nothing')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('Run as root on the Gene host')
    FRI_ROOT = args.fri_root
    root = args.root.absolute()  # as batch-release.sh's ROOT, which the pins it writes name
    if args.status:
        report = status(root)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report['healthy'] else 1
    # Owner policy, 2026-10-27: any other out-of-window change needs the owner's explicit words.
    code, result = execute(root, args.apply, os.environ.get('MAINTENANCE_EXCEPTION', '').strip())
    if result is not None:
        print(json.dumps({'apply': args.apply, 'deletion': result['deletion'],
                          'eligible_images': len(result['delete']), 'eligible_parents': len(result['delete_parents']),
                          'removed_tags': len(result['removed_tags']),
                          'removed_parents': len(result.get('removed_parent_ids', [])),
                          'kept_images': len(result['keep']), 'keep_summary': result['keep_summary'],
                          'delete': summary(result)['delete'], 'delete_parents': summary(result)['delete_parents'],
                          'bytes': summary(result)['bytes'], 'deferred': result.get('deferred'),
                          'plan': str(root / 'maintenance/image-retention-plan.json')}, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
