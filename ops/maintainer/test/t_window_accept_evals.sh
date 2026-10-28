# shellcheck shell=bash
# A suite fails inside the window. After 07:30, resume waits for the eval rule's decision (accept-evals,
# with its reason) and a passing verify-off; then it restores service on the kept release.
. "$(dirname "$0")/common.sh"
at 2026-10-28T04:20:00-07:00
prep; run 0 gene 0; run 0 fri 0
MOCK_EVAL_FAIL=sass run 1 evals 1
journal_has evals_ran; journal_lacks evaluated
at 2026-10-28T07:50:00-07:00
run 4 resume 1
output_has 'accept-evals'
run 1 accept-evals 1
output_has 'set EVALS_ACCEPTED'
EVALS_ACCEPTED='sass miss is a known one; rerun passed' run 0 accept-evals 1
journal_has evaluated
run 4 resume 1
run 0 verify-off 1
run 0 resume 1
expect new false up
finish
