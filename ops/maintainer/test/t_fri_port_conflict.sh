# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
prep
run 0 gene 1
export MOCK_PORT_BUSY=1
run 1 fri 3
# Prove the same occupied-port recovery fails with #273's script first.
git -C "$REPO" show 3d8e23a:ops/maintainer/batch-release.sh > "$MOCK/previous-release.sh"
S="$MOCK/previous-release.sh" run 1 rollback-gene 3
output_has 'bind: address already in use'
run 0 rollback-gene 3
expect old true down
[[ $(cat "$BATCH_ROOT/source/src/marker") == old ]] || fail 'previous source was not restored'
run 0 resume-old 1
expect old false up
finish
