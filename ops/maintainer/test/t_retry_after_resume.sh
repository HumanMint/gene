# shellcheck shell=bash
# A release that shipped, was rolled back after resume, and is retried with BATCH_ATTEMPT=2 as the runbook
# says: the retry gets its own staging dir and result file, so its resume finishes.
. "$(dirname "$0")/common.sh"
release
expect new false up
run 0 rollback-gene 1
run 0 resume-old 1
expect old false up
mv "$BATCH_ROOT/releases/$G" "$BATCH_ROOT/releases/$G.attempt-1"
export BATCH_ATTEMPT=2
ST=$ST-2
release
expect new false up
[[ -f $BATCH_ROOT/maintainer-$G-$F-2-result.json ]] || fail 'no result file for the retry'
[[ -f $BATCH_ROOT/maintainer-$G-$F-result.json ]] || fail 'the first attempt lost its result file'
run 0 resume 1
output_has 'already resumed'
run 0 cleanup 1
finish
