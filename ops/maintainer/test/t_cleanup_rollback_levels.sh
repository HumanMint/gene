# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
# Paired G1+F1, Fri-only G1+F2, then Gene-only G2+F2.
G2=$G F2=$F
G=$(git -C "$REPO" rev-parse HEAD^) F=$(git -C "$FRI_REPO" rev-parse HEAD^)
ST=$BATCH_ROOT/staging/batch-$G-$F; JOURNAL=$ST/journal
release
first=$ST/manifest.json
next_day
F=$F2
ST=$BATCH_ROOT/staging/batch-$G-$F; JOURNAL=$ST/journal
release
next_day
G=$G2
ST=$BATCH_ROOT/staging/batch-$G-$F; JOURNAL=$ST/journal
release
run 0 cleanup 3
python3 - "$first" "$MOCK/docker.json" <<'PY' || fail 'cleanup removed an image from the second Gene rollback level'
import json, sys
manifest, docker = (json.load(open(path)) for path in sys.argv[1:])
for side in ('old', 'new'):
    for image in manifest[side].values():
        assert docker['images'].get(image['tag'], {}).get('id') == image['id'], image['tag']
PY
finish
