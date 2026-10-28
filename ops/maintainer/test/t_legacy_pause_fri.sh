# shellcheck shell=bash
# gene #71 review B1, rollback-fri's stop: a Fri-only batch drained by the script from before pause audit ids
# (8deef14), then switched by this one. A deployment lands Gene H during rollback-fri's fence, so it stops with
# Fri untouched, and it lifts the old script's pause: development runs again.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
H=$(printf 'c%.0s' {1..40})
LEGACY=$MOCK/legacy-batch-release.sh
git -C "$REPO" show 8deef142e571c2e2126605f0a5e84d1cc35eee5f:ops/maintainer/batch-release.sh > "$LEGACY"
S=$LEGACY run 0 baseline 0; upload; S=$LEGACY run 0 stage 0; S=$LEGACY run 0 rehearse 0; S=$LEGACY run 0 drain 0
! grep -q '^paused:' "$JOURNAL" || fail 'the old script journaled a pause id'
run 0 gene 0; run 0 fri 0
fri_before=$(cd "$BATCH_FRI_ROOT" && sha256sum release.env release.json)
as_owner "$GENE_PY" - "$DB" <<'PY'
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES ('job-h','444','1','src-h','','in flight','deploying',?,?)", (time.time(), time.time()))
PY
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
output_has "development resumed: this batch's pause"
[[ $fri_before == "$(cd "$BATCH_FRI_ROOT" && sha256sum release.env release.json)" ]] || fail 'Fri was changed'
[[ $(sort "$MOCK/units" | tr '\n' ' ') == 'gene-deployer gene-worker ' ]] || fail "units not running: $(tr '\n' ' ' < "$MOCK/units")"
[[ $(as_owner "$GENE_PY" -c 'import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute("SELECT value FROM settings WHERE key=?", ("paused",)).fetchone()[0])' "$DB") == false ]] ||
  fail 'development was left paused'
journal_has done:unpause; journal_lacks intent:rollback
finish
