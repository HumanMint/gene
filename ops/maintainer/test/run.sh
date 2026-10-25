#!/usr/bin/env bash
# Offline checks of batch-release.sh: FRI_REPO=<fri checkout> ops/maintainer/test/run.sh
# Each test builds a fresh mock host under a temp dir; outputs stay in it for inspection.
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=$(mktemp -d)
for t in t_success t_health_fail t_quiet_mid t_fri_fail t_after_resume t_crash; do
  echo "######## $t"
  bash "$HERE/$t.sh" "$BASE/$t" 2>&1 | grep -v '^---- \(baseline\|stage\|rehearse\|drain\)$'
done
echo "outputs in $BASE"
