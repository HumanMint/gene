# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
prep
MOCK_BROKEN_SHA=$G run gene 12
summary
echo '== rollback again (idempotent)'; run rollback-gene 4; summary
run resume-old 2; summary
