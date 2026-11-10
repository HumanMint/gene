# shellcheck shell=bash
# The whole release, then repeats of resume and status.
. "$(dirname "$0")/common.sh"
run 0 baseline 2; upload; run 0 stage 4; run 0 rehearse 6; run 0 drain 1; run 0 gene 3; run 0 fri 2
MOCK_EVAL_FAIL=sass run 1 evals 2
run 0 evals 2; run 0 verify-off 1; run 0 resume 1
expect new false up
[[ -f $BATCH_ROOT/maintainer-$G-$F-result.json ]] || fail 'no result file'
run 0 resume 1; run 0 cleanup 4
expect new false up
images=$(python3 -c 'import json, sys; print(" ".join(sorted(json.load(open(sys.argv[1]))["images"])))' "$MOCK/docker.json")
for tag in "gene-runtime:$G" "gene-worker:$G" "fri-runtime:$F" "gene-runtime:$OLD_G" "gene-worker:$OLD_G" "fri-runtime:$OLD_F" \
           "fri-runtime:$(printf '3%.0s' {1..40})" gene-runtime:bootstrap; do
  [[ " $images " == *" $tag "* ]] || fail "cleanup removed $tag"
done
[[ $images != *1111111111* && $images != *2222222222* ]] || fail "cleanup kept an older release's images: $images"
output_has 'kept image fri-runtime:3333'
# Exact arguments exclude -a and every broader prune command.
[[ $(grep 'prune' "$MOCK/docker.log") == 'image prune -f' ]] || fail 'unexpected prune call'
output_has 'sha256:dangling'
output_has 'Total reclaimed space: 1000B'
KEEP_IMAGES=1 run 0 cleanup 4
[[ $(grep -c 'prune' "$MOCK/docker.log") == 1 ]] || fail 'KEEP_IMAGES did not skip prune'
output_has 'KEEP_IMAGES=1: no images removed'
MOCK_IMAGE_PRUNE_FAIL=1 run 0 cleanup 4
output_has 'WARNING: dangling image prune failed'
purges 0
finish
