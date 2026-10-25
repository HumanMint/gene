# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
prep
run gene 1
MOCK_FRI_FAIL=$F run fri 4
run rollback-gene 6
summary
