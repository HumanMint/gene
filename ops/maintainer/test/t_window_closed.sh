# shellcheck shell=bash
# The maintenance window (owner policy, 2026-10-27; widened 2026-10-28): outside [03:30, 07:30)
# America/Los_Angeles a phase takes no lock and writes no staging, an overdue wake is refused, and the
# daylight-saving day still opens at 03:30 local. After the close only the restore path runs: resume-old
# at once, cleanup next window.
. "$(dirname "$0")/common.sh"
# Exact boundaries are unit-tested with a stopped clock; this one runs on in real time.
for when in 2026-10-28T03:29:50-07:00 2026-10-28T07:30:00-07:00 2026-10-28T16:30:00-07:00; do
  at "$when"
  run 4 baseline 1
  output_has 'REFUSED: outside the maintenance window'
  run 4 status 0
done
[[ ! -e $BATCH_ROOT/batch-release.lock ]] || fail 'a refused phase took the lock'
[[ ! -e $BATCH_ROOT/staging ]] || fail 'a refused phase created staging'
at 2026-11-01T03:30:00-08:00  # Daylight saving ended at 02:00: the window opens at 03:30 PST.
prep
at 2026-11-01T07:30:00-08:00
run 4 gene 1
output_has 'Run resume-old.'
journal_lacks gene_attempted
expect old true down
run 0 resume-old 1
expect old false up
run 4 cleanup 1
at 2026-11-02T04:10:00-08:00
run 0 cleanup 1
python3 - "$BATCH_ROOT/production-actions.jsonl" <<'PY' || fail 'production-action log'
import json, os, stat, sys
rows = [json.loads(line) for line in open(sys.argv[1])]
keys = {'at', 'tz', 'actor', 'trigger', 'action', 'reason', 'before', 'after', 'duration_s', 'restart_s', 'outcome', 'exception'}
assert all(keys <= set(row) for row in rows), rows
refused = [row for row in rows if row['outcome'].startswith('refused')]
assert [row['action'] for row in refused] == ['batch-release baseline']*3 + ['batch-release gene', 'batch-release cleanup'], refused
assert {row['local_date'] for row in rows if row['action'] == 'batch-release drain'} == {'2026-11-01'}
assert rows[0]['at'].startswith('2026-10-28T03:29:5') and rows[0]['tz'] == 'America/Los_Angeles'
assert stat.S_IMODE(os.stat(sys.argv[1]).st_mode) == 0o600
PY
finish
