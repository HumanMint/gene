# shellcheck shell=bash
# One batch drains per local day: after a rollback at 04:20, a retry the same morning is refused before
# it takes the lock; the owner's explicit exception is let through and logged; the next day it runs.
. "$(dirname "$0")/common.sh"
release
run 0 rollback-gene 1
run 0 resume-old 1
mv "$BATCH_ROOT/releases/$G" "$BATCH_ROOT/releases/$G.attempt-1"
export BATCH_ATTEMPT=2
ST=$ST-2
JOURNAL=$ST/journal
at 2026-10-28T04:40:00-07:00
run 4 baseline 1
output_has 'a batch already drained today'
[[ ! -e $ST ]] || fail 'the refused retry created staging'
MAINTENANCE_EXCEPTION='owner, 2026-10-28: test exception' run 0 baseline 0
grep -q '"exception": "owner, 2026-10-28: test exception"' "$BATCH_ROOT/production-actions.jsonl" || fail 'exception not logged'
at 2026-10-29T04:05:00-07:00
upload; run 0 stage 0; run 0 rehearse 0; run 0 drain 0
finish
