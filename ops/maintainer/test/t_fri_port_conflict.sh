# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
prep
run 0 gene 1
export MOCK_PORT_BUSY=1
run 1 fri 3
run 0 rollback-gene 3
expect old true down
[[ $(cat "$BATCH_ROOT/source/src/marker") == old ]] || fail 'previous source was not restored'
run 0 resume-old 1
expect old false up
finish
