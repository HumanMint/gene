# shellcheck shell=bash
# After resume a deployment is in flight. The rollback fence waits for it, and it lands a third
# Gene release H meanwhile: the second pair check refuses before anything is restored.
. "$(dirname "$0")/common.sh"
release
H=$(printf 'b%.0s' {1..40})
as_owner "$GENE_PY" - "$BATCH_ROOT/data/controller/gene.sqlite3" <<'PY'
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
with sqlite3.connect("$BATCH_ROOT/data/controller/gene.sqlite3") as db:
    db.execute("UPDATE jobs SET state=\"live\" WHERE id=\"job-h\"")
    db.execute("UPDATE settings SET value=\"$G\" WHERE key=\"previous_sha\"")
    db.execute("UPDATE settings SET value=\"$H\" WHERE key=\"active_sha\"")'
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$H/" "$BATCH_ROOT/release.env"
X
chmod +x "$MOCK/on-sleep"
fri_before=$(cd "$BATCH_ROOT/../fri" && sha256sum release.env release.json)
run 1 rollback-gene 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
output_has 'outside the recorded old/new pair (release.env, release settings)'
[[ $fri_before == "$(cd "$BATCH_ROOT/../fri" && sha256sum release.env release.json)" ]] || fail 'Fri was changed'
journal_lacks intent:fri_restore; journal_lacks done:downgrade; purges 0
finish
