# shellcheck shell=bash
# gene.fri_release dies mid-switch: new pins and container, old record. Rollback reconciles Fri by hand.
. "$(dirname "$0")/common.sh"
prep
run 0 gene 1
MOCK_FRI_CRASH=$F run 1 fri 2
run 0 rollback-gene 3
journal_has fri_restored; purges 1
expect old true down
finish
