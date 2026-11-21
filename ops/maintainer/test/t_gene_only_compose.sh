# shellcheck shell=bash
export MOCK_OLD_F=F
. "$(dirname "$0")/common.sh"
run 0 baseline 0
upload
for allowance in 0 1; do
  export ALLOW_OPS_DIFF=$allowance
  run 1 stage 2
  output_has 'Fri Compose change must ship with a Fri release'
  journal_lacks staged
  ! grep -q '^build ' "$MOCK/docker.log" || fail 'refused batch built an image'
done
expect old false up
finish
