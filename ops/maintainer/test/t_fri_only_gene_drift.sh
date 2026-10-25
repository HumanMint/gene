# shellcheck shell=bash
# A Fri-only release, then a member request deploys Gene H: settings and release.env move, while the images,
# worker pin and source stay at G, as a feature deployment leaves them. rollback-fri refuses with Fri
# untouched, whether H landed before it started or during its fence, and development keeps running.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
H=$(printf 'c%.0s' {1..40})
release
expect new false up
setting() { as_owner "$GENE_PY" -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT value FROM settings WHERE key=?", (sys.argv[2],)).fetchone()[0])' "$DB" "$1"; }
running() {  # Fri untouched, and development as it was before the rollback
  [[ $fri_before == "$(cd "$BATCH_FRI_ROOT" && sha256sum release.env release.json)" ]] || fail 'Fri was changed'
  [[ $(sort "$MOCK/units" | tr '\n' ' ') == 'gene-deployer gene-worker ' ]] || fail "units not running: $(tr '\n' ' ' < "$MOCK/units")"
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
# Back at G, with H in flight: the rollback's fence waits for it, and it lands meanwhile.
as_owner "$GENE_PY" -c '
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE settings SET value=? WHERE key=\"previous_sha\"", (sys.argv[2],))
    db.execute("UPDATE settings SET value=? WHERE key=\"active_sha\"", (sys.argv[3],))
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES (\"job-h\",\"444\",\"1\",\"src-h\",\"\",\"in flight\",\"deploying\",?,?)", (time.time(), time.time()))' \
  "$DB" "$previous" "$G"
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$G/" "$BATCH_ROOT/release.env"
cat > "$MOCK/on-sleep" <<X
#!/usr/bin/env bash
# The deployer finishes: Gene moves to H.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE jobs SET state=\"live\" WHERE id=\"job-h\"")
    db.execute("UPDATE settings SET value=\"$G\" WHERE key=\"previous_sha\"")
    db.execute("UPDATE settings SET value=\"$H\" WHERE key=\"active_sha\"")'
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$H/" "$BATCH_ROOT/release.env"
X
chmod +x "$MOCK/on-sleep"
run 1 rollback-fri 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
output_has 'Gene moved during the fence'
running
finish
