# shellcheck shell=bash
# A rollback after a completed release is killed right after it stops the new controller and Red.
# A repeated resume must refuse rather than report the release as resumed; finishing the rollback works.
. "$(dirname "$0")/common.sh"
release
MOCK_KILL_AFTER_STOP=1 run 137 rollback-gene 1
journal_has intent:rollback; journal_lacks gene_rolled_back
run 1 resume 1
output_has 'a rollback has started'
run 0 rollback-gene 3
expect old true down
run 0 resume-old 1
expect old false up
finish
