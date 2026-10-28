# shellcheck shell=bash
# The evals pass, then a rerun inside the window fails: the earlier pass no longer counts, so resume after
# 07:30 waits for the eval rule's decision on the rerun.
. "$(dirname "$0")/common.sh"
at 2026-10-28T04:20:00-07:00
prep; run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0
MOCK_EVAL_FAIL=sass run 1 evals 1
at 2026-10-28T07:50:00-07:00
run 4 resume 1
output_has 'accept-evals'
EVALS_ACCEPTED='sass miss is a known one' run 0 accept-evals 1
run 0 resume 1
expect new false up
finish
