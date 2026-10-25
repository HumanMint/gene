# shellcheck shell=bash
# The whole release, then repeats of resume and status.
. "$(dirname "$0")/common.sh"
run 0 baseline 2; upload; run 0 stage 4; run 0 rehearse 6; run 0 drain 1; run 0 gene 3; run 0 fri 2
run 0 evals 2; run 0 verify-off 1; run 0 resume 1
expect new false up
[[ -f $BATCH_ROOT/maintainer-$G-result.json ]] || fail 'no result file'
run 0 resume 1; run 0 cleanup 1
expect new false up
purges 0
finish
