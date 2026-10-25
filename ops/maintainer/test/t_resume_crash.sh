# shellcheck shell=bash
# Resume stopped after writing the result file but before journaling resumed: a repeat finishes.
. "$(dirname "$0")/common.sh"
release
sed -i '/^resumed$/d' "$JOURNAL"
run 0 resume 1
journal_has resumed
expect new false up
echo '{"sha": "other"}' > "$BATCH_ROOT/maintainer-$G-result.json"
sed -i '/^resumed$/d' "$JOURNAL"
run 1 resume 1
output_has 'already exists with a different result'
finish
