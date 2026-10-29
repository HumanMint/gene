# shellcheck shell=bash
# The evals ran inside the window; verify-off and resume finish after 07:30, restarting normal service on
# a release that already passed its gates. Cleanup waits for the next window.
. "$(dirname "$0")/common.sh"
at 2026-10-28T04:20:00-07:00
prep; run 0 gene 0; run 0 fri 0; run 0 evals 0
journal_has evaluated
at 2026-10-28T08:00:00-07:00
run 0 verify-off 1
run 0 resume 1
expect new false up
run 4 cleanup 1
python3 - "$BATCH_ROOT/production-actions.jsonl" "$G" "$F" "$OLD_G" "$OLD_F" <<'PY' || fail 'production-action log'
import json, sys
rows = {json.loads(line)['action']: json.loads(line) for line in open(sys.argv[1])}
G, F, OLD_G, OLD_F = sys.argv[2:6]
gene, fri = rows['batch-release gene'], rows['batch-release fri']
assert gene['before'] == {'gene': OLD_G, 'fri': OLD_F} and gene['after'] == {'gene': G, 'fri': OLD_F}, gene
assert fri['after'] == {'gene': G, 'fri': F} and isinstance(gene['restart_s'], int) and isinstance(fri['restart_s'], int)
assert rows['batch-release resume']['at'].startswith('2026-10-28T08:0') and rows['batch-release resume']['outcome'] == 'ok'
assert all(row['exception'] is None for row in rows.values())
PY
finish
