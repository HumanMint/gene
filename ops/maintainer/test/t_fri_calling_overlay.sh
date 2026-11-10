# shellcheck shell=bash
if [[ -z ${OVERLAY_CASE:-} ]]; then
  for mutation in missing dangling mode replaced; do
    OVERLAY_CASE=$mutation bash "$0" "$1/$mutation" || exit 1
  done
  echo 'RESULT: PASS'
  exit 0
fi
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
overlay=$BATCH_FRI_ROOT/compose.calling.yml
if [[ $OVERLAY_CASE == missing ]]; then
  printf 'services: {}\n' > "$overlay"; chmod 600 "$overlay"
fi
prep
run 0 gene 0
export MOCK_OVERLAY_MUTATION=$OVERLAY_CASE
if [[ $OVERLAY_CASE == missing ]]; then export MOCK_FRI_FAIL=$F; else export MOCK_FRI_CRASH=$F; fi
run 1 fri 2
unset MOCK_OVERLAY_MUTATION MOCK_FRI_FAIL MOCK_FRI_CRASH
run 0 rollback-fri 1
run 0 resume-old 1
expect old false up
python3 - "$MOCK/compose-commands.jsonl" "$ST" "$OVERLAY_CASE" <<'PY'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1])]
files = [row['files'] for row in rows if row['project'] == 'fri']
assert files and all(f == files[0] for f in files)
if sys.argv[3] == 'missing':
    assert len(files[0]) == 2 and files[0][1] == sys.argv[2]+'/calling.snapshot.yml'
else:
    assert all(len(f) == 1 for f in files)
PY
[[ $? == 0 ]] || fail 'Compose configuration changed during recovery'
finish
