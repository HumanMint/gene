# shellcheck shell=bash
# Players on the audio node for the whole window, even idle: no switch, then resume-old.
. "$(dirname "$0")/common.sh"
prep
MOCK_PLAYERS=1 MOCK_PLAYING=0 run 1 gene 2
output_has 'ask the owner'
journal_lacks gene_attempted
expect old true down
run 1 rollback-gene 1
run 0 resume-old 1
expect old false up
finish
