# shellcheck shell=bash
# Both halves switched by 04:20, then the operator comes back after 05:00 with evals not run. Evals and
# resume are refused; the safe path is the rollback, which runs at once, and cleanup waits a window.
. "$(dirname "$0")/common.sh"
at 2026-10-28T04:10:00-07:00
prep; run 0 gene 0; run 0 fri 0
at 2026-10-28T05:01:00-07:00
run 4 evals 1
output_has 'Run rollback-gene, then resume-old.'
run 4 resume 1
run 0 rollback-gene 1
run 0 resume-old 1
expect old false up
run 4 cleanup 1
at 2026-10-29T04:05:00-07:00
run 0 cleanup 1
finish
