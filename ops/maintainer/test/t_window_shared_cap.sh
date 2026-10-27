# shellcheck shell=bash
# One production batch per local day across both routes. An automatic deployment the controller claimed
# before the drain's pause finishes during the fence and used today's batch: the drain resumes
# development as it was, never drains, and leaves the controller no hold. A retry that day is refused;
# CRITICAL_FIX lets a P0 fix drain, logged, and its drain leaves the audit row that holds the
# controller's automatic claims for the rest of the day.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
holds() {  # The controller's record of drained batches: maintenance.DRAINED audit rows.
  as_owner "$GENE_PY" -c 'import sqlite3, sys
db = sqlite3.connect("file:"+sys.argv[1]+"?mode=ro", uri=True)
print(db.execute("SELECT count(*) FROM audit WHERE action=?", ("batch_drained",)).fetchone()[0])' "$DB"
}
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
as_owner "$GENE_PY" - "$DB" <<'PY'
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES ('a1b2c3d4e5f6','444','1','src-a','','in flight','deploying',?,?)", (time.time(), time.time()))
PY
cat > "$MOCK/on-sleep" <<X
#!/usr/bin/env bash
# The deployment fails unswitched and logs its line, as gene.deploy does.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE jobs SET state=\"failed\" WHERE id=\"a1b2c3d4e5f6\"")'
echo '{"action": "deploy feature job a1b2c3d4e5f6", "actor": "gene-deployer", "at": "2026-10-28T04:05:40-07:00", "outcome": "failed"}' >> "$BATCH_ROOT/production-actions.jsonl"
X
chmod +x "$MOCK/on-sleep"
run 1 drain 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
output_has 'an automatic deployment ran today: deploy feature job a1b2c3d4e5f6'
journal_lacks drained; journal_has resumed_old
expect old false up
[[ $(holds) == 0 ]] || fail 'a batch that never drained holds automatic claims'
run 4 gene 1
output_has 'already resumed the old release'
export BATCH_ATTEMPT=2
ST=$ST-2
JOURNAL=$ST/journal
at 2026-10-28T04:20:00-07:00
run 4 baseline 1
output_has "CRITICAL_FIX='<reason>'"
[[ ! -e $ST ]] || fail 'the refused retry created staging'
export CRITICAL_FIX='P0: test crash fix'
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0; run 0 drain 0
journal_has drained
[[ $(holds) == 1 ]] || fail 'the drained batch left the controller no hold'
grep -q '"exception": "critical fix: P0: test crash fix"' "$BATCH_ROOT/production-actions.jsonl" || fail 'critical fix not logged'
finish
