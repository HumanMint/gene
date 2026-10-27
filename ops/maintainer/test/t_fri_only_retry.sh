# shellcheck shell=bash
# A Fri-only release that shipped, was rolled back with rollback-fri after resume, and is retried with
# BATCH_ATTEMPT=2: the retry gets its own staging dir and result file, so its resume finishes.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
release
expect new false up
run 0 rollback-fri 1
run 0 resume-old 1
expect old false up
next_day  # A retry is a new batch, and one batch drains per local day.
export BATCH_ATTEMPT=2
ST=$ST-2
release
expect new false up
[[ -f $BATCH_ROOT/maintainer-$G-$F-2-result.json ]] || fail 'no result file for the retry'
run 0 cleanup 1
finish
