# shellcheck shell=bash
# CI acknowledges reviewed ops changes; staging still requires that explicit acknowledgment.
. "$(dirname "$0")/common.sh"
unset ALLOW_OPS_DIFF
run 0 baseline 0; upload
# Ensure a difference even when a later candidate has the same Compose as its baseline.
echo '# mock ops difference' >> "$BATCH_ROOT/source/ops/compose.yml"
run 1 stage 2
output_has 'ops/compose.yml changed; review the diff, then rerun with ALLOW_OPS_DIFF=1'
journal_lacks staged
! grep -q '^build ' "$MOCK/docker.log" || fail 'stage built an image before acknowledgment'
export ALLOW_OPS_DIFF=1
run 0 stage 0
finish
