"""Conservative backup retention, called under the batch release lock."""
import json
import re
import shutil
import sys
import time
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def backup_roots(root, fri):
    directories = (root/'backups', fri/'backups')
    if any(p.is_symlink() or not p.is_dir() for p in directories):
        raise ValueError('backup root must be a real directory; no backups removed')
    return tuple(p.resolve(strict=True) for p in directories)


def backup_path(value, directories):
    if not isinstance(value, str) or not value:
        raise ValueError('missing backup reference; no backups removed')
    path = Path(value)
    resolved = path.resolve(strict=True)
    if path.is_symlink() or not path.is_dir() or resolved.parent not in directories:
        raise ValueError('backup reference is outside the managed directories')
    return resolved


def plan(root, fri, now):
    directories = backup_roots(root, fri)

    def pin(path, key):
        values = [line.split('=', 1)[1] for line in path.read_text().splitlines() if line.startswith(key+'=')]
        if len(values) != 1 or not re.fullmatch('[0-9a-f]{40}', values[0]):
            raise ValueError('missing or invalid live release pin')
        return values[0]

    live = (pin(root/'release.env', 'GENE_RELEASE_SHA'), pin(fri/'release.env', 'FRI_RELEASE_SHA'))
    manifests = []
    for path in sorted((root/'staging').glob('batch-*/manifest.json')):
        m = read(path)
        pair = (m['gene'], m['fri'])
        previous = (m['old_gene'], m['old_fri'])
        marks = set((path.parent/'journal').read_text().splitlines())
        manifests.append((m, pair, previous, marks))
    current = [entry for entry in manifests if entry[1] == live]
    if not current:
        raise ValueError('live release has no manifest; no backups removed')
    pairs = {live} | {entry[2] for entry in current}
    if not pairs.issubset({entry[1] for entry in manifests}):
        raise ValueError('previous release has no manifest; no backups removed')
    keep = set()

    def protect(value):
        if value:
            keep.add(backup_path(value, directories))

    for m, pair, previous, marks in manifests:
        unfinished = 'resumed_old' not in marks and ('resumed' not in marks or 'intent:rollback' in marks)
        if pair in pairs or unfinished:
            protect(m.get('backup'))
    # Fri keeps its own rollback record; its saved before record can name the prior backup.
    record = read(fri/'release.json')
    live_backup = backup_path(record['backup'], directories)
    keep.add(live_backup)
    saved = read(live_backup/'before.json')
    if saved is not None:
        if not isinstance(saved, dict):
            raise ValueError('invalid previous Fri release record; no backups removed')
        keep.add(backup_path(saved.get('backup'), directories))
    for path in root.glob('maintainer-*-result.json'):
        result = read(path)
        if (result['sha'], result['fri_sha']) in pairs:
            protect(result.get('backup'))
    candidates = []
    for directory in directories:
        entries = [backup_path(str(p), directories) for p in directory.iterdir()]
        entries.sort(key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
        candidates.extend(p for p in entries[10:] if p not in keep and p.stat().st_mtime < now-14*86400)
    return sorted(candidates)


def main():
    root, fri = (Path(p).absolute() for p in sys.argv[1:3])
    mode = sys.argv[3]
    if mode not in ('--dry-run', '--apply'):
        raise ValueError('use --dry-run or --apply')
    directories = backup_roots(root, fri)
    candidates = plan(root, fri, time.time())
    if backup_roots(root, fri) != directories:
        raise ValueError('backup roots changed; no backups removed')
    for path in candidates:
        backup_path(str(path), directories)
    for path in candidates:
        print(str(path), flush=True)
    if mode == '--apply':
        for path in candidates:
            if backup_roots(root, fri) != directories:
                raise ValueError('backup roots changed; no further backups removed')
            shutil.rmtree(backup_path(str(path), directories))


if __name__ == '__main__':
    main()
