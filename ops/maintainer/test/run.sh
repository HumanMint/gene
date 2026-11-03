#!/usr/bin/env bash
# Offline checks of batch-release.sh: FRI_REPO=<fri checkout> ops/maintainer/test/run.sh [t_name ...]
# Each test builds a fresh mock host under a temp dir and asserts every phase's exit code and the
# resulting state. Tests run in parallel; outputs stay in the temp dir for inspection.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$(mktemp -d); chmod 755 "$BASE"
export OLD_SRC_ROOT=$BASE/old-src
tests=("$@"); (( ${#tests[@]} )) || mapfile -t tests < <(cd "$HERE" && find . -maxdepth 1 -name 't_*.sh' | sed 's|^\./||; s/\.sh$//' | sort)
for old in '' 7d6e36a0e4621bbce75dd9c404b4864b90699b19 34dbbb3799715328772b0ae68ab2fb1d81a68fdb G; do  # Each old release's code, once.
  (MOCK_OLD_G=$old MOCK=$BASE/prime . "$HERE/env.sh")
done
for t in "${tests[@]}"; do bash "$HERE/$t.sh" "$BASE/$t" > "$BASE/$t.log" 2>&1 & done
wait
failures=()
for t in "${tests[@]}"; do
  echo "######## $t"; cat "$BASE/$t.log"
  tail -n 1 "$BASE/$t.log" | grep -qx 'RESULT: PASS' || failures+=("$t")
done
echo "outputs in $BASE"
if (( ${#failures[@]} )); then echo "FAILED: ${failures[*]}"; exit 1; fi
echo "ALL ${#tests[@]} PASSED"
