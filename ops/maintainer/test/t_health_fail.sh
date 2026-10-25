# shellcheck shell=bash
# A new Gene that never connects: automatic rollback, a repeated rollback, then resume-old.
. "$(dirname "$0")/common.sh"
prep
MOCK_BROKEN_SHA=$G run 1 gene 4
output_has 'Gene rolled back'
expect old true down
journal_has gene_rolled_back; journal_has done:downgrade; purges 1
run 0 rollback-gene 2
purges 1
expect old true down
run 0 resume-old 1
expect old false up
finish
