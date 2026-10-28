# shellcheck shell=bash
# After resume a deployment is in flight. The rollback fence waits for it, and it lands a third Gene release H
# meanwhile: the second pair check stops before anything is restored. gene #93: the rollback changed nothing but
# its fence, so it journals its stop, starts the worker and deployer again and lifts its own pause, leaving Gene
# at H; a repeated resume-old stays at H. When that restore fails, a repeated rollback-gene refuses and names
# resume-old, which, after the window, resumes development at H; a repeat of it is safe.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
H=$(printf 'b%.0s' {1..40})
release
fri_before=$(cd "$BATCH_FRI_ROOT" && sha256sum release.env release.json)
in_flight() {  # job: back at G, with H in flight; the rollback's fence waits for it, and it lands meanwhile
  as_owner "$GENE_PY" -c '
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE settings SET value=? WHERE key=\"previous_sha\"", (sys.argv[2],))
    db.execute("UPDATE settings SET value=? WHERE key=\"active_sha\"", (sys.argv[3],))
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES (?,\"444\",\"1\",?,\"\",\"in flight\",\"deploying\",?,?)", (sys.argv[4], "src-"+sys.argv[4], time.time(), time.time()))' \
    "$DB" "$OLD_G" "$G" "$1"
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
at_h() {  # Gene where the deployment left it, and nothing the rollback restores touched
  grep -qx "GENE_RELEASE_SHA=$H" "$BATCH_ROOT/release.env" || fail 'release.env no longer names H'
  [[ $(setting active_sha) == "$H" ]] || fail 'the active release is no longer H'
  [[ $fri_before == "$(cd "$BATCH_FRI_ROOT" && sha256sum release.env release.json)" ]] || fail 'Fri was changed'
  [[ $(cat "$BATCH_ROOT/source/.release-sha") == "$G" ]] || fail 'the source tree was changed'
  journal_lacks intent:fri_restore; journal_lacks done:downgrade; journal_lacks gene_rolled_back; purges 0
}
running() {  # development resumed: the units up and the batch's pause lifted
  units up
  [[ $(setting paused) == false ]] || fail 'development was left paused'
}
in_flight job-h
run 1 rollback-gene 3
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
output_has 'outside the recorded old/new pair (release.env, release settings)'
output_has 'Gene moved during the fence; nothing was rolled back'
journal_has intent:abort; journal_has resumed_old
running; at_h
run 0 resume-old 2  # A repeat stays at H.
output_has 'already resumed development as it is'
running; at_h
run 1 rollback-gene 2  # H is outside the recorded pair: refused before the fence.
output_has 'outside the recorded old/new pair'
running; at_h
# The same, but the worker and deployer then fail to start, so development stays fenced at H.
rm -f "$MOCK/on-sleep.ran"
in_flight job-h2
: > "$MOCK/start-fails"
run 1 rollback-gene 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
units down
[[ $(setting paused) == true ]] || fail 'the fence did not pause development'
at 2026-10-28T08:00:00-07:00  # Restoring service runs at any hour.
run 1 rollback-gene 2
output_has 'run resume-old first'
units down
run 0 resume-old 2
output_has "at Gene ${H:0:7}"
running; at_h
run 0 resume-old 1  # A repeat is safe.
running; at_h
finish
