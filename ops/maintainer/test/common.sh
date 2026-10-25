# shellcheck shell=bash
# Shared by the t_*.sh tests: fresh mock host, then helpers.
set -u
export MOCK=$1; H=$(dirname "$0"); "$H/setup.sh" "$MOCK"; . "$H/env.sh"
run() { echo "---- $1"; bash "$S" "$1" "$G" "$F" > "$MOCK/out-$1" 2>&1; local rc=$?; tail -${2:-3} "$MOCK/out-$1"; echo "rc=$rc"; return $rc; }
prep() { run baseline 0 && upload && run stage 0 && run rehearse 0 && run drain 0; }
summary() { bash "$S" status "$G" "$F" | head -1 | python3 -c 'import json,sys; s=json.load(sys.stdin); print("settings", s["settings"]); print("pins", s["gene_env"]["GENE_RELEASE_SHA"][:7], s["worker_pin"][-40:-33], s["fri_env"]["FRI_RELEASE_SHA"][:7], "containers", s["controller"]["image"][:20], s["red"], s["fri_container"]["image"][:20])'
  echo "units: $(tr '\n' ' ' < "$MOCK/units")"; echo "source marker: $(cat "$BATCH_ROOT/source/.release-sha" 2>/dev/null || echo none) old: $(cat "$BATCH_ROOT/source/src/marker" 2>/dev/null)"
  echo "purges: $(wc -l < "$MOCK/purges" 2>/dev/null || echo 0)"; grep -rl 'do-not-print' "$MOCK"/out-* || echo 'no secret in outputs'; }
