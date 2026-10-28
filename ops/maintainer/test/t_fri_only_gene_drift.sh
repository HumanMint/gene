# shellcheck shell=bash
# A Fri-only release, then a member request deploys Gene H: settings and release.env move, while the images,
# worker pin and source stay at G, as a feature deployment leaves them. rollback-fri refuses with Fri
# untouched, whether H landed before it started or during its fence, and development keeps running, even
# when the worker and deployer first fail to start (gene #70).
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
H=$(printf 'c%.0s' {1..40})
release
expect new false up
running() {  # Fri untouched, and development as it was before the rollback
  [[ $fri_before == "$(cd "$BATCH_FRI_ROOT" && sha256sum release.env release.json)" ]] || fail 'Fri was changed'
  units up
  [[ $(setting paused) == false ]] || fail 'development was left paused'
  journal_lacks intent:rollback; journal_lacks intent:fri_restore
}
previous=$(setting previous_sha)
fri_before=$(cd "$BATCH_FRI_ROOT" && sha256sum release.env release.json)
# H deployed before the rollback.
as_owner "$GENE_PY" -c '
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE settings SET value=? WHERE key=\"previous_sha\"", (sys.argv[2],))
    db.execute("UPDATE settings SET value=? WHERE key=\"active_sha\"", (sys.argv[3],))' "$DB" "$G" "$H"
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$H/" "$BATCH_ROOT/release.env"
run 1 rollback-fri 2
output_has 'Gene is no longer'; output_has 'nothing was changed'
running
in_flight() {  # job: back at G, with H in flight; the rollback's fence waits for it, and it lands meanwhile
  as_owner "$GENE_PY" -c '
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE settings SET value=? WHERE key=\"previous_sha\"", (sys.argv[2],))
    db.execute("UPDATE settings SET value=? WHERE key=\"active_sha\"", (sys.argv[3],))
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES (?,\"444\",\"1\",?,\"\",\"in flight\",\"deploying\",?,?)", (sys.argv[4], "src-"+sys.argv[4], time.time(), time.time()))' \
    "$DB" "$previous" "$G" "$1"
  sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$G/" "$BATCH_ROOT/release.env"
  cat > "$MOCK/on-sleep" <<X
#!/usr/bin/env bash
# The deployer finishes: Gene moves to H.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE jobs SET state=\"live\" WHERE id=\"$1\"")
    db.execute("UPDATE settings SET value=\"$G\" WHERE key=\"previous_sha\"")
    db.execute("UPDATE settings SET value=\"$H\" WHERE key=\"active_sha\"")'
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$H/" "$BATCH_ROOT/release.env"
X
  chmod +x "$MOCK/on-sleep"
}
in_flight job-h
run 1 rollback-fri 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
output_has 'Gene moved during the fence'
running
# gene #70: the same, but the worker and deployer then fail to start, so development stays fenced. A
# repeated rollback-fri changes nothing and points to resume-old, which, outside the window, restores
# development at H, where the deployment left Gene, rather than refusing the moved release.
rm -f "$MOCK/on-sleep.ran"
in_flight job-h2
: > "$MOCK/start-fails"
run 1 rollback-fri 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
units down
[[ $(setting paused) == true ]] || fail 'the fence did not pause development'
at 2026-10-28T05:30:00-07:00
run 1 rollback-fri 2
output_has 'run resume-old'
units down
run 0 resume-old 2
output_has "at Gene ${H:0:7}"
grep -qx "GENE_RELEASE_SHA=$H" "$BATCH_ROOT/release.env" || fail 'release.env no longer names H'
running
finish
