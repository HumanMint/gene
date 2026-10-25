# shellcheck shell=bash
# A release that changes only Fri, with Gene live at G: nothing of Gene's is uploaded, built, stopped or
# downgraded. Then rollback-fri after it shipped (an eval the owner didn't like, say), and resume-old.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
release
expect new false up
[[ ! -e $ST/gene.tar ]] || fail 'gene.tar was uploaded'
! grep -q -e '-t gene-' -e 'stop red' -e 'stop controller' -e 'controller red' "$MOCK/docker.log" || fail 'a Fri-only release touched Gene'
run 1 rollback-gene 1
output_has "phase needs 'gene_attempted' first"
run 0 rollback-fri 2
journal_has fri_restored; journal_has fri_rolled_back; purges 0
expect old true down
run 1 resume 1
run 0 resume-old 1
expect old false up
run 0 cleanup 1
finish
