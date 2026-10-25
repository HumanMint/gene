# shellcheck shell=bash
# gene.fri_release fails and restores the old Fri itself; rollback-gene leaves Fri alone.
. "$(dirname "$0")/common.sh"
prep
run 0 gene 1
MOCK_FRI_FAIL=$F run 1 fri 2
run 0 rollback-gene 3
journal_lacks intent:fri_restore; purges 1
expect old true down
finish
