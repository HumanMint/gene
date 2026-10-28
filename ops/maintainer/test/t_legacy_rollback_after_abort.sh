# shellcheck shell=bash
# gene #121 round 1: a journal that the script before gene #121 finished. That script journaled intent:rollback once
# per batch, so a rollback it ran after a stop left no new intent:rollback. A Fri-only release is rolled back and
# resumed; a second rollback-fri finds Gene moved during its fence and resumes development as it is (intent:abort).
# Gene is put back at G by hand and Fri's container dies. The old script's rollback-fri then stops Fri and is
# killed there. resume-old must not take that earlier stop for the latest: Fri's restore began since, so it checks
# the old pair, finds Fri stopped and refuses, with development still fenced. Repeating rollback-fri, at any hour,
# restores Fri, and resume-old then resumes development at the old pair.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
H=$(printf 'e%.0s' {1..40})
LEGACY=$MOCK/batch-release-before-121.sh  # main before gene #121
git -C "$REPO" show 0a3af1a9ab4f3e69407b1170f3c9b5b4d6960225:ops/maintainer/batch-release.sh > "$LEGACY"
release
run 0 rollback-fri 1
run 0 resume-old 1
expect old false up
previous=$(setting previous_sha)
# A deployment of H lands during a second rollback-fri's fence: development resumes as it is, at H.
deployment_in_flight job-h "$H" "$G"
on_sleep <<< "deployment_lands job-h $H $G"
run 1 rollback-fri 2
output_has 'Gene moved during the fence'
journal_has intent:abort
units up
# Gene goes back to G by hand, as it was, and Fri's container dies.
as_owner "$GENE_PY" -c '
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE settings SET value=? WHERE key=\"previous_sha\"", (sys.argv[2],))
    db.execute("UPDATE settings SET value=? WHERE key=\"active_sha\"", (sys.argv[3],))' "$DB" "$previous" "$G"
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$G/" "$BATCH_ROOT/release.env"
fri_dies() {
  python3 - "$MOCK/docker.json" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
state['containers']['fri-runtime-1']['running'] = False
json.dump(state, open(sys.argv[1], 'w'))
PY
}
fri_dies
fri_stopped() { [[ $(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["containers"]["fri-runtime-1"]["running"])' "$MOCK/docker.json") == False ]]; }
# The old script's rollback-fri restarts it: it fences, journals intent:fri_restore but no new intent:rollback,
# stops Fri and is killed there.
S=$LEGACY MOCK_KILL_AFTER_FRI_STOP=1 run 137 rollback-fri 1
[[ $(tail -n 1 "$JOURNAL") == intent:fri_restore ]] || fail "the old script's rollback did not stop at Fri's restore"
[[ $(grep -cx intent:rollback "$JOURNAL") == 1 ]] || fail 'the old script journaled intent:rollback again'
fri_stopped || fail 'Fri is running'
units down
at 2026-10-28T08:00:00-07:00  # Recovery runs at any hour.
run 1 resume-old 2
output_has 'old release is not consistent'
units down
[[ $(setting paused) == true ]] || fail 'development was unpaused with Fri stopped'
fri_stopped || fail 'Fri was started by resume-old'
run 0 rollback-fri 2
expect old true down
run 0 resume-old 1
expect old false up
finish
