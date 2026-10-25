# shellcheck shell=bash
# After resume the new controller dies; the rollback pauses and downgrades without it.
. "$(dirname "$0")/common.sh"
release
python3 - "$MOCK/docker.json" <<'PY'
import json, sys
st = json.load(open(sys.argv[1]))
st['containers']['gene-controller-1']['running'] = False
json.dump(st, open(sys.argv[1], 'w'))
PY
run 0 rollback-gene 3
journal_has done:downgrade
expect old true down
run 0 resume-old 1
expect old false up
finish
