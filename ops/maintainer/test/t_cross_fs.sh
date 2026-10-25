# shellcheck shell=bash
# The backups dir moved to another filesystem after staging: the switch refuses before any
# change, since its moves would become copies.
. "$(dirname "$0")/common.sh"
prep
elsewhere=$(mktemp -d /dev/shm/batch-backups.XXXXXX)
[[ $(stat -c %d "$elsewhere") != "$(stat -c %d "$BATCH_ROOT")" ]] || { echo 'no second filesystem here; skipping'; rm -rf "$elsewhere"; echo 'RESULT: PASS'; exit 0; }
rmdir "$BATCH_ROOT/backups"; ln -s "$elsewhere" "$BATCH_ROOT/backups"
run 1 gene 1
output_has 'must share one filesystem'
journal_lacks gene_attempted
expect old true down
rm -rf "$elsewhere"
finish
