# shellcheck shell=bash
# No clock can be read at all after the release (gene #61): rollback-gene and resume-old still restore the
# old release, since recovery takes its probe deadlines from bash's SECONDS, never from date.
. "$(dirname "$0")/common.sh"
release
touch "$MOCK/date-broken"
run 0 rollback-gene 1
run 0 resume-old 1
expect old false up
finish
