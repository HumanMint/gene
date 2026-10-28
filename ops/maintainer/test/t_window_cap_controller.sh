# shellcheck shell=bash
# gene #68: the daily cap reads the controller's own deployment record, so an automatic deployment that
# never reached the production-action log still uses the day. The controller claims it after the drain's
# gate, before its pause, and it finishes during the fence, recorded only in SQLite: the drain resumes
# development as it was and never drains, and a retry that day is refused at its gate.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
read -r base set < "$MOCK/clock"
cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
# The drain's first docker call, past its gate: the controller claims a feature deployment, now by the window's clock.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3, time
with sqlite3.connect("$DB") as db:
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
               ("a1b2c3d4e5f6", "444", "1", "src-a", "", "in flight", "deploying", time.time(), time.time()))
    db.execute("INSERT INTO deployments VALUES (?,?,?,?,?)", ("a1b2c3d4e5f6", "$G", "$OLD_G", "deploying", $(( base + $(/bin/date +%s) - set ))))'
X
chmod +x "$MOCK/on-docker"
cat > "$MOCK/on-sleep" <<X
#!/usr/bin/env bash
# The deployment fails unswitched. It records that in SQLite and writes no production-action line.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE jobs SET state=\"failed\" WHERE id=\"a1b2c3d4e5f6\"")
    db.execute("UPDATE deployments SET state=\"failed\" WHERE job_id=\"a1b2c3d4e5f6\"")'
X
chmod +x "$MOCK/on-sleep"
run 1 drain 2
[[ -e $MOCK/on-docker.ran ]] || fail 'the deployment was never claimed'
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
! grep -q '"deploy feature job' "$BATCH_ROOT/production-actions.jsonl" || fail 'the deployment reached the action log'
output_has 'the controller claimed an automatic deployment today: feature job a1b2c3d4e5f6'
journal_lacks drained; journal_has resumed_old
expect old false up
export BATCH_ATTEMPT=2
ST=$ST-2 JOURNAL=$ST/journal
at 2026-10-28T04:20:00-07:00
run 4 baseline 1
output_has 'the controller claimed an automatic deployment today: feature job a1b2c3d4e5f6'
[[ ! -e $ST ]] || fail 'the refused retry created staging'
next_day
run 0 baseline 0
finish
