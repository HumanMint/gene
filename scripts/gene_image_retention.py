"""Root-only Gene image retention. Unknown/inactive images are never retirement evidence."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

SHA = re.compile(r'[0-9a-f]{40}')
IMAGE_ID = re.compile(r'sha256:[0-9a-f]{64}')
VERSION_TAG = re.compile(r'gene-(?:runtime|worker):([0-9a-f]{40})')
TERMINAL = {'failed', 'rejected', 'deployed', 'cancelled'}
GRACE = 24 * 60 * 60


def command(*args):
    return subprocess.check_output(args, text=True, timeout=120)


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


def docker_inventory():
    images = {}
    for line in command('docker', 'image', 'ls', '--no-trunc', '--format', '{{json .}}').splitlines():
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
        settings = dict(db.execute("SELECT key,value FROM settings WHERE key IN ('active_sha','previous_sha')"))
        jobs, cog_jobs = [], {}
        for job, kind, state, proposal, approved in db.execute(
                'SELECT id,kind,state,proposal,approved_sha FROM jobs'):
            if state == 'deployed' and kind in {'feature', 'repair', 'rollback'}:
                details = json.loads(proposal) if proposal else {}
                cog_jobs[job] = (details.get('sha'), details.get('base_sha'))
            if state in TERMINAL:
                continue
            if state in {'building', 'deploying'}:
                raise ValueError('A Gene build/deployment is in progress; retry later')
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
            raise ValueError('A Docker build is in progress; retry later')
    return refs


def snapshot(root, policy):
    images, containers = docker_inventory()
    by_tag = {tag: iid for iid, row in images.items() for tag in row['tags']}
    settings, jobs, deployments, cog_jobs = controller_refs(root)
    env = read_env(root / 'release.env')
    worker = read_env(root / 'secrets/worker.env')
    if env.get('GENE_RELEASE_SHA') != settings['active_sha']:
        raise ValueError('Controller and configured release disagree')
    current_refs = [env['GENE_RUNTIME_IMAGE'], worker['GENE_WORKER_IMAGE']]
    functional = set(current_refs + process_refs())
    refs = set(functional) | set(policy['keep_images'])
    config_files = sorted(set(root.glob('*.env')) | set((root / 'secrets').glob('*.env'))
                          | set((root / 'pending').glob('*/release.env'))
                          | set((root / 'pending').glob('*/worker.env'))
                          | set((root / 'pending').glob('*/audio-release.env')))
    config = {}
    for path in config_files:
        values = read_env(path)
        config[str(path)] = values
        refs.update(v for k, v in values.items() if k.endswith('_IMAGE'))
    # Audio is independent. Resolve both pins even though its repository is never deleted.
    audio = dict(line.split('=', 1) for line in (root / 'audio-release.env').read_text().splitlines()
                 if line and not line.startswith('#') and '=' in line)
    refs.add('gene-audio:' + audio['GENE_AUDIO_SHA'])
    refs.add('gene-audio-resolver:' + audio.get('GENE_AUDIO_RESOLVER_SHA', audio['GENE_AUDIO_SHA']))
    protected_shas = {settings['active_sha'], settings.get('previous_sha', ''), *jobs}
    past = {}
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
                if VERSION_TAG.fullmatch(ref) and ref in by_tag:
                    iid = by_tag[ref]
                    record = past.setdefault(iid, {'retired_at': at, 'tags': [],
                                                  'evidence': 'Superseded in successful deployment history'})
                    record['retired_at'] = max(record['retired_at'], at)
                    if ref not in record['tags']:
                        record['tags'].append(ref)
            break
    rollback_files = {}
    cursor, before, complete = settings['active_sha'], float('inf'), 0
    # Preserve two complete rollback configurations, rather than guessing by image age.
    while complete < 2:
        matches = [row for row in deployments if row[1] == cursor and row[3] < before]
        if not matches:
            raise ValueError('Missing successful rollback lineage for ' + cursor)
        job, _, previous, at = matches[0]
        if before == float('inf') and previous != settings.get('previous_sha'):
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
            protected_shas.add(previous)
            cursor, before = previous, at
            continue
        if (not (backup / 'installed-source').is_dir()
                or any(not (backup / name).is_file() for name in
                       ('controller.sqlite3', 'red.tar.gz', 'fri.tar.gz'))):
            raise ValueError('Rollback source or data backup is incomplete')
        release, old_worker = read_env(backup / 'release.env'), read_env(backup / 'worker.env')
        if (release.get('GENE_RELEASE_SHA') != previous
                or not (root / 'releases' / previous / 'cogs').is_dir()):
            raise ValueError('Rollback source/release configuration is incomplete')
        rollback_files[str(backup)] = [release, old_worker]
        refs.update((release['GENE_RUNTIME_IMAGE'], old_worker['GENE_WORKER_IMAGE']))
        protected_shas.add(previous)
        cursor, before = previous, at
        complete += 1
    if (root / 'deploy-result.json').exists():
        raise ValueError('A deployment completion receipt needs reconciliation')
    protected, resolved = set(containers.values()), {}
    for ref in refs:
        iid = ref if IMAGE_ID.fullmatch(ref) else by_tag.get(ref)
        if iid not in images:
            # The default listing hides intermediate images. An explicit ID/digest
            # hold must resolve through inspection rather than being treated as absent.
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/@-]*', ref):
                raise ValueError('Invalid configured image reference')
            metadata = image_metadata(ref)
            iid = metadata['id']
            if not IMAGE_ID.fullmatch(iid) or (IMAGE_ID.fullmatch(ref) and ref != iid):
                raise ValueError('Configured image identity is invalid')
            images[iid] = {'tags': sorted(metadata.get('tags') or [])}
        resolved[ref] = iid
        protected.add(iid)
    current_ids = {resolved[ref] for ref in functional}
    current_tags = {iid: sorted(ref for ref in functional if by_tag.get(ref) == iid
                                and VERSION_TAG.fullmatch(ref)) for iid in current_ids}
    for iid, row in images.items():
        if any((m := VERSION_TAG.fullmatch(t)) and m[1] in protected_shas for t in row['tags']):
            protected.add(iid)
    guard = {'settings': settings, 'jobs': jobs, 'config': config, 'rollback': rollback_files,
             'containers': containers, 'functional': sorted(functional), 'resolved': resolved,
             'policy': policy}
    fingerprint = hashlib.sha256(json.dumps(guard, sort_keys=True).encode()).hexdigest()
    return {'images': images, 'protected': protected, 'current': current_ids,
            'current_tags': current_tags, 'past': past, 'fingerprint': fingerprint}


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
            reason = 'configured, container, pending job, hold, or rollback reference'
        elif not tags or any(not VERSION_TAG.fullmatch(tag) for tag in tags):
            reason = 'outside the versioned Gene runtime/worker scope'
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


def remove_parents(root, state, fingerprint, result):
    """No implicit pruning: attempt only exact ancestors listed in the saved plan."""
    for row in result['delete_parents']:
        iid = row['id']
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
        try:
            command('docker', 'image', 'rm', '--no-prune', iid)
        except subprocess.CalledProcessError:
            continue  # Docker preserves parents with dependent children/containers.
        result.setdefault('removed_parent_ids', []).append(iid)
        state['retired_parents'].pop(iid, None)
        save_json(root / 'maintenance/image-retention-state.json', state)
        save_json(root / 'maintenance/image-retention-result.json', result)


def run(root, apply=False):
    folder = root / 'maintenance'
    policy = read_json(folder / 'image-retention-policy.json')
    if policy.get('version') != 1 or not isinstance(policy.get('keep_images'), list):
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
    result = plan(view, state, now)
    result['removed_tags'] = []
    for row in result['delete']:
        record_parents(state, row)
    plan_parents(view, state, result, now)
    save_json(folder / 'image-retention-plan.json', result)
    if apply:
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
                fresh = snapshot(root, read_json(folder / 'image-retention-policy.json'))
                if fresh['fingerprint'] != view['fingerprint'] or row['id'] in fresh['protected']:
                    raise ValueError('References changed before tag removal; cleanup aborted')
                # A tag must still resolve to the exact reviewed content before removing it.
                actual = json.loads(command('docker', 'image', 'inspect', tag))[0]['Id']
                if actual != row['id']:
                    raise ValueError('Image was retagged; removal aborted')
                try:
                    command('docker', 'image', 'rm', '--no-prune', tag)
                except subprocess.CalledProcessError:
                    result.setdefault('skipped_tags', []).append(tag)
                    continue  # A retained child/container may still require the parent image.
                result['removed_tags'].append(tag)
                state.setdefault('removed', {})[tag] = {'id': row['id'], 'at': time.time()}
                save_json(state_path, state)
                save_json(folder / 'image-retention-result.json', result)
        remove_parents(root, state, view['fingerprint'], result)
        save_json(folder / 'image-retention-result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('/srv/gene'))
    parser.add_argument('--apply', action='store_true', help='Remove only proven retired images')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run as root on the Gene host')
    with (args.root / 'maintenance/image-retention.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(args.root.resolve(), args.apply)
    print(json.dumps({'apply': args.apply, 'eligible_images': len(result['delete']),
                      'eligible_parents': len(result['delete_parents']),
                      'removed_tags': len(result['removed_tags']),
                      'removed_parents': len(result.get('removed_parent_ids', [])),
                      'kept_images': len(result['keep'])}))


if __name__ == '__main__':
    main()
