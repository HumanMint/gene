# shellcheck shell=bash
# The suite's reviewed ops-diff allowance must not mask the production refusal gate.
unset ALLOW_OPS_DIFF
. "$(dirname "$0")/common.sh"
run 0 baseline 0
upload
run 1 stage 2
output_has 'ops/fri-compose.yml changed; review the diff'
journal_lacks staged
! grep -q '^build ' "$MOCK/docker.log" || fail 'unapproved ops diff built an image'
export ALLOW_OPS_DIFF=1
run 0 stage 1
journal_has staged
finish
