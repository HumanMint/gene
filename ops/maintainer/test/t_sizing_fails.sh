# shellcheck shell=bash
# gene #100: a size that can't be measured stops the phase instead of dropping out of a sum. du fails on Red's
# data while stage sizes the backups: stage stops before building anything. Then it fails at the Gene switch's
# own measurement, after the attempt is journaled: the switch rolls back from its backup instead of going on.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload
echo "$BATCH_ROOT/data/red 1" > "$MOCK/du-fails"
run 1 stage 2
output_has "could not measure $BATCH_ROOT/data/red"
journal_lacks staged
! grep -q '^build ' "$MOCK/docker.log" || fail 'stage built an image after a failed measurement'
rm -f "$MOCK/du-fails" "$MOCK/du-seen"
run 0 stage 0; run 0 rehearse 0; run 0 drain 0
echo "$BATCH_ROOT/data/red 2" > "$MOCK/du-fails"  # gene's second disk check, the one after gene_attempted
run 1 gene 3
output_has "could not measure $BATCH_ROOT/data/red"
output_has 'Gene switch failed; rolling back'
journal_has gene_attempted; journal_has gene_rolled_back; journal_lacks gene_recorded
rm -f "$MOCK/du-fails" "$MOCK/du-seen"
run 0 resume-old 0
expect old false up
finish
