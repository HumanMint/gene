# shellcheck shell=bash
# Resume interrupted around the result file: before it was linked into place (a partial temporary
# left behind), and after it was written but before resumed was journaled. A repeat finishes; a
# different result file is refused.
. "$(dirname "$0")/common.sh"
release
RESULT=$BATCH_ROOT/maintainer-$G-$F-result.json
rm "$RESULT"; echo '{"sha": "partial' > "$RESULT.stray"  # an earlier writer's own temporary
sed -i '/^resumed$/d' "$JOURNAL"
run 0 resume 1
python3 -c 'import json, sys; assert json.load(open(sys.argv[1]))["sha"] == sys.argv[2]' "$RESULT" "$G" || fail 'result file not valid'
[[ $(find "$BATCH_ROOT" -maxdepth 1 -name "maintainer-$G-$F-result.json.*" ! -name '*.stray' | wc -l) == 0 ]] || fail 'temporary result left behind'
sed -i '/^resumed$/d' "$JOURNAL"
run 0 resume 1
journal_has resumed
expect new false up
echo '{"sha": "other"}' > "$RESULT"
sed -i '/^resumed$/d' "$JOURNAL"
run 1 resume 1
output_has 'already exists with a different result'
finish
