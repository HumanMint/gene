"""The production maintenance window and the production-action log (owner policy, 2026-10-27).

Production changes start only in [04:00, 05:00) America/Los_Angeles, following daylight saving.
Rollback and restoring healthy service run at any hour. An unknown clock or zone counts as closed.
ops/maintainer/batch-release.sh carries its own copy of the window, since it runs without this package.
"""

import datetime as dt
import fcntl
import json
import math
import os
import time
from pathlib import Path

ZONE = 'America/Los_Angeles'
OPENS, CLOSES = dt.time(4), dt.time(5)
# Automatic deployments are claimed only this long before the close, so a switch never starts after it.
CLAIM_MARGIN = dt.timedelta(minutes=10)
EARLIEST = 1793059200  # 2026-10-27T00:00Z: a clock before the policy existed is not trusted
LOG_MAX = 1 << 20  # bytes; past this the log keeps its newest half
clock = time.time


def local(now=None):
    """The wall-clock time in the window's zone, or None when the clock or the zone is unknown."""
    try:
        from zoneinfo import ZoneInfo
        stamp = float(clock() if now is None else now)
        if not math.isfinite(stamp) or stamp < EARLIEST:
            return None
        return dt.datetime.fromtimestamp(stamp, ZoneInfo(ZONE))
    except Exception:  # A missing zone database, an unreadable clock: closed, never open.
        return None


def is_open(now=None, margin=dt.timedelta(0)):
    """True only inside the window, ending margin before the close."""
    moment = local(now)
    if moment is None:
        return False
    closes = (dt.datetime.combine(dt.date(2000, 1, 1), CLOSES) - margin).time()
    return OPENS <= moment.time() < closes


def record(path, action, *, actor, trigger, reason, outcome, before=None, after=None,
           started=None, restart_s=None, exception=None, **extra):
    """Append one production action. Never put secrets or member content in any field."""
    now = clock()
    moment = local(now)
    row = {'at': moment.isoformat() if moment else dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
           'tz': ZONE if moment else 'UTC (zone unavailable)', 'actor': actor, 'trigger': trigger,
           'action': action, 'reason': reason, 'before': before, 'after': after,
           'duration_s': None if started is None else round(now-started, 1),
           'restart_s': None if restart_s is None else round(restart_s, 1),
           'outcome': outcome, 'exception': exception or None} | extra
    append(Path(path), json.dumps(row, sort_keys=True))


def append(path, line):
    """One line, under the log's lock; past LOG_MAX the oldest lines go, so the log stays bounded."""
    while True:
        with open(path, 'a') as log:
            os.fchmod(log.fileno(), 0o600)
            fcntl.flock(log, fcntl.LOCK_EX)
            try:
                if os.fstat(log.fileno()).st_ino != os.stat(path).st_ino:
                    continue  # Trimmed meanwhile: write to the file that is there now.
            except FileNotFoundError:
                continue
            log.write(line+'\n')
            log.flush()
            if log.tell() > LOG_MAX:
                trim(path)
            return


def trim(path):
    lines = Path(path).read_bytes().splitlines(keepends=True)
    kept, size = [], 0
    for line in reversed(lines):
        if size+len(line) > LOG_MAX // 2:
            break
        kept.append(line)
        size += len(line)
    temporary = path.with_name(path.name+'.tmp')
    with open(temporary, 'wb') as out:
        os.fchmod(out.fileno(), 0o600)
        out.writelines(reversed(kept))
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
