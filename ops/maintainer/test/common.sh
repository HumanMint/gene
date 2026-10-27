# shellcheck shell=bash
# Shared by the t_*.sh tests: a fresh mock host, then phase runs with asserted outcomes.
set -uo pipefail
export MOCK=$1
H=$(cd "$(dirname "$0")" && pwd)
. "$H/env.sh"
"$H/setup.sh" > "$MOCK.setup.log" 2>&1 || { cat "$MOCK.setup.log"; echo 'RESULT: FAIL (mock host setup)'; exit 1; }
FAILED=0 RUNS=0
JOURNAL=$ST/journal
at() {  # local time with offset, e.g. 2026-10-28T04:05:00-07:00: the mock host's maintenance-window clock
  echo "$(/bin/date -d "$1" +%s) $(/bin/date +%s)" > "$MOCK/clock"
}
next_day() {  # The same wall-clock time a day later, in the window's zone.
  local base
  read -r base _ < "$MOCK/clock"
  at "$(TZ=America/Los_Angeles /bin/date -d "$(TZ=America/Los_Angeles /bin/date -d "@$base" '+%F %T') tomorrow" --iso-8601=seconds)"
}
at 2026-10-28T04:05:00-07:00  # Inside the window unless a test says otherwise.
fail() { echo "ASSERT FAILED: $*"; FAILED=1; }
run() {  # expected_rc phase [lines of output to show]
  local want=$1 phase=$2 lines=${3:-3} rc=0 out
  RUNS=$(( RUNS + 1 )); out=$MOCK/out-$RUNS-$phase
  echo "---- $phase"
  bash "$S" "$phase" "$G" "$F" > "$out" 2>&1 || rc=$?
  # A killed run's children keep the batch lock until they exit, and the next phase would rightly refuse.
  (( rc != 137 )) || flock "$BATCH_ROOT/batch-release.lock" true
  (( lines == 0 )) || tail -n "$lines" "$out"
  [[ $rc == "$want" ]] || { tail -n 15 "$out"; fail "$phase exited $rc, expected $want"; }
}
as_owner() { setpriv --reuid=10001 --regid=10001 --clear-groups "$@"; }
prep() { run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0; run 0 drain 0; }
release() { prep; run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 0; }
journal_has() { grep -qx -- "$1" "$JOURNAL" || fail "journal lacks $1"; }
journal_lacks() { ! grep -qx -- "$1" "$JOURNAL" || fail "journal has $1"; }
output_has() { grep -q -- "$1" "$MOCK/out-$RUNS-"* || fail "last output lacks: $1"; }
purges() { [[ $(cat "$MOCK/purges" 2>/dev/null | wc -l) == "$1" ]] || fail "expected $1 purges, saw $(cat "$MOCK/purges" 2>/dev/null | wc -l)"; }
expect() {  # old|new paused(true|false) units(up|down): what is recorded, pinned, running and reported
  local s
  s=$(bash "$S" status "$G" "$F" 2>/dev/null | head -1)
  python3 - "$s" "$1" "$2" "$3" "$G" "$F" "$OLD_G" "$OLD_F" "$(tr '\n' ' ' < "$MOCK/units")" "$MOCK/docker.json" "$OLD_SOURCE" <<'PY' || fail "expected $1 release, paused=$2, units $3"
import json, sys
s, which, paused, units, G, F, OLD_G, OLD_F, running, docker, old_source = json.loads(sys.argv[1]), *sys.argv[2:12]
images = json.load(open(docker))['images']
g, f = (G, F) if which == 'new' else (OLD_G, OLD_F)
runtime, fri = images['gene-runtime:'+g]['id'], images['fri-runtime:'+f]['id']
checks = {
    'active_sha': s['settings']['active_sha'] == g, 'paused': s['settings']['paused'] == paused,
    'fri_active_sha': s['settings'].get('fri_active_sha') == f,
    'gene_env': s['gene_env'] == {'GENE_RELEASE_SHA': g, 'GENE_RUNTIME_IMAGE': 'gene-runtime:'+g},
    'worker_pin': s['worker_pin'] == 'gene-worker:'+g,
    'source': s['source'] == (G if which == 'new' and G != OLD_G else old_source),
    'fri_env': s['fri_env'] == {'FRI_RELEASE_SHA': f, 'FRI_RUNTIME_IMAGE': 'fri-runtime:'+f}, 'fri_record': s['fri_record_sha'] == f,
    'controller': s['controller']['health'] == 'healthy' and s['controller']['image'] == runtime,
    'red': s['red']['running'] and s['red']['image'] == runtime,
    'fri_container': s['fri_container']['running'] and s['fri_container']['image'] == fri,
    'gene_health': s['gene']['sha'] == g and s['gene']['guild_connected'] is True, 'fri_health': s['fri']['sha'] == f,
    'units': sorted(running.split()) == (['gene-deployer', 'gene-worker'] if units == 'up' else [])}
bad = [k for k, v in checks.items() if not v]
print('  state:', which, 'paused='+paused, 'units', units, '->', 'ok' if not bad else 'MISMATCH '+', '.join(bad))
sys.exit(1 if bad else 0)
PY
}
finish() {
  local stray
  stray=$(find "$BATCH_ROOT/data/controller" ! -user 10001 -print)
  [[ -z $stray ]] || fail "controller data not owned by the DB owner: $stray"
  ! grep -rl 'do-not-print' "$MOCK"/out-* || fail 'a secret reached the output'
  if (( FAILED )); then echo 'RESULT: FAIL'; exit 1; fi
  echo 'RESULT: PASS'
}
