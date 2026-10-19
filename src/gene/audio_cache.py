"""Bounded private audio files; independent of Red's metadata cache."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

from fastapi import HTTPException

VIDEO_ID = re.compile(r'^[A-Za-z0-9_-]{11}$')
MEDIA_EXTENSIONS = {'.webm', '.m4a', '.mp4', '.ogg', '.mp3'}


class MediaCache:
    """Atomically publish downloads, expire idle tracks, and protect playback leases."""

    def __init__(self, folder, *, max_bytes=256 * 1024 * 1024,
                 track_bytes=64 * 1024 * 1024, ttl=3600, reserve_bytes=0, evict=False):
        self.folder = Path(folder).resolve()
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if min(max_bytes, track_bytes, ttl) <= 0 or reserve_bytes < 0:
            raise ValueError('Invalid cache limits')
        self.max_bytes, self.track_bytes, self.ttl = max_bytes, track_bytes, ttl
        self.reserve_bytes, self.evict = reserve_bytes, evict
        self.lock = threading.RLock()
        self.lease_file = self.folder / '.leases.json'
        self.leases = json.loads(self.lease_file.read_text()) if self.lease_file.exists() else {}

    def _files(self):
        return [path for path in self.folder.iterdir()
                if path.is_file() and not path.is_symlink()
                and path.suffix in MEDIA_EXTENSIONS and VIDEO_ID.fullmatch(path.stem)]

    def _write_json(self, path, value):
        fd, temporary = tempfile.mkstemp(prefix='.write-', dir=self.folder)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump(value, output, ensure_ascii=False)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def _remove(self, path):
        path.unlink()
        (self.folder / (path.stem + '.json')).unlink(missing_ok=True)

    def _protected(self):
        now = time.time()
        live = {key: value for key, value in self.leases.items() if value['until'] > now}
        if live != self.leases:
            self.leases = live
            self._write_json(self.lease_file, live)
        return {row['id'] for row in live.values()}

    def clean(self):
        with self.lock:
            now, protected = time.time(), self._protected()
            for path in self.folder.iterdir():
                # Transfers finish within minutes. Crash debris gets its own short lifetime.
                if path.name.startswith('download-') and path.is_dir() and not path.is_symlink():
                    if now - path.stat().st_mtime > 900:
                        shutil.rmtree(path)
                elif path.name.startswith('.write-') and not path.is_symlink():
                    if now - path.stat().st_mtime > 900:
                        path.unlink()
            for path in self._files():
                if path.stem not in protected and now - path.stat().st_mtime > self.ttl:
                    self._remove(path)

    def _make_room(self):
        def enough():
            used = sum(p.stat().st_size for p in self.folder.rglob('*')
                       if p.is_file() and not p.is_symlink())
            # Include a bounded metadata record when enforcing the production budget.
            required = self.track_bytes + (4096 if self.evict else 0)
            return (used + required <= self.max_bytes
                    and shutil.disk_usage(self.folder).free >= required + self.reserve_bytes)

        if enough():
            return
        if self.evict:
            protected = self._protected()
            for path in sorted(self._files(), key=lambda p: p.stat().st_mtime):
                if path.stem in protected:
                    continue
                self._remove(path)
                if enough():
                    return
        raise HTTPException(503, 'The music cache is full. Try again after older tracks expire.')

    def get(self, identifier, download):
        if not VIDEO_ID.fullmatch(identifier):
            raise ValueError('A video ID is required')
        # Serialize misses, deduplicate requests, and include partial downloads in the cap.
        with self.lock:
            self.clean()
            for path in self._files():
                if path.stem == identifier and 0 < path.stat().st_size <= self.track_bytes:
                    path.touch()
                    return path
            self._make_room()
            with tempfile.TemporaryDirectory(prefix='download-', dir=self.folder) as temporary:
                folder = Path(temporary)
                candidate = Path(download(folder, self.track_bytes))
                if candidate.is_symlink() or candidate.resolve().parent != folder.resolve():
                    raise ValueError('Invalid cached audio path')
                if candidate.suffix not in MEDIA_EXTENSIONS or not 0 < candidate.stat().st_size <= self.track_bytes:
                    raise ValueError('Audio file exceeds the track limit or uses an unsupported format')
                if shutil.disk_usage(self.folder).free < self.reserve_bytes:
                    raise HTTPException(503, 'Host storage is low; the track was not cached.')
                destination = self.folder / (identifier + candidate.suffix)
                candidate.chmod(0o600)
                candidate.replace(destination)
                return destination

    def save_metadata(self, identifier, row):
        with self.lock:
            if not VIDEO_ID.fullmatch(identifier) or row.get('identifier') != identifier:
                raise ValueError('Invalid track metadata')
            # Persist only display metadata, never remote URLs, paths, or extractor history.
            safe = {'identifier': identifier, 'title': str(row['title'])[:300],
                    'author': str(row['author'])[:300], 'length': int(row['length']),
                    'uri': 'https://www.youtube.com/watch?v=' + identifier}
            self._write_json(self.folder / (identifier + '.json'), safe)

    def metadata(self, identifier):
        with self.lock:
            self.clean()
            if not VIDEO_ID.fullmatch(identifier) or not any(p.stem == identifier for p in self._files()):
                return None
            path = self.folder / (identifier + '.json')
            if not path.exists() or path.is_symlink():
                return None
            return json.loads(path.read_text())

    def pin(self, identifier, ttl=7200):
        with self.lock:
            self._protected()
            if len(self.leases) >= 512:
                raise HTTPException(503, 'Too many active audio leases')
            lease = secrets.token_hex(16)
            # Persist across resolver restarts. Bounded lifetime recovers orphaned leases.
            self.leases[lease] = {'id': identifier, 'until': time.time() + ttl}
            self._write_json(self.lease_file, self.leases)
            return lease

    def release(self, lease):
        with self.lock:
            if self.leases.pop(lease, None) is not None:
                self._write_json(self.lease_file, self.leases)
