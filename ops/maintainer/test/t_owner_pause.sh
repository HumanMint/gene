# shellcheck shell=bash
# gene #69: the batch lifts only its own pause, and only while it is still the latest pause or resume, so a
# pause the owner makes through /pause during the batch stays. First the drain's fence: the owner pauses
# while it waits out an automatic deployment that then uses today's batch, and the drain starts the worker
# and deployer again but leaves the owner's pause. Then resume-old after a drain the owner paused during,
# and resume after a full release the owner paused during.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
owner() {  # pause|resume: the owner's /pause, written as Store.set_paused writes it
  as_owner "$GENE_PY" - "$DB" "$1" <<'PY'
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE settings SET value=? WHERE key='paused'", ('true' if sys.argv[2] == 'pause' else 'false',))
    db.execute('INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)', (time.time(), '555', sys.argv[2], '', ''))
PY
}
noted() {  # phase: the phase's last production-action line says development stays paused
  python3 - "$BATCH_ROOT/production-actions.jsonl" "batch-release $1" <<'PY' || fail "the $1 line in the action log does not say development stays paused"
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1])]
row = [row for row in rows if row.get('action') == sys.argv[2]][-1]
sys.exit(0 if 'development stays paused' in (row.get('note') or '') else 1)
PY
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
# While the fence waits, the owner pauses development; then the deployment fails unswitched and logs its line.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3, time
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE settings SET value=\"true\" WHERE key=\"paused\"")
    db.execute("INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)", (time.time(), "555", "pause", "", ""))
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE jobs SET state=\"failed\" WHERE id=\"a1b2c3d4e5f6\"")'
echo '{"action": "deploy feature job a1b2c3d4e5f6", "actor": "gene-deployer", "at": "2026-10-28T04:05:40-07:00", "outcome": "failed"}' >> "$BATCH_ROOT/production-actions.jsonl"
X
chmod +x "$MOCK/on-sleep"
run 1 drain 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
output_has 'an automatic deployment ran today: deploy feature job a1b2c3d4e5f6'
output_has 'development stays paused: a newer pause'
journal_has intent:pause; journal_has resumed_old; journal_lacks drained; journal_lacks done:unpause
expect old true up
noted drain

next_day  # A batch that resumed the old release doesn't go forward again: a retry is a new batch.
export BATCH_ATTEMPT=2
ST=$ST-2 JOURNAL=$ST/journal  # left to right: JOURNAL sees the new ST
owner resume
prep
journal_has intent:pause
owner pause
run 0 resume-old 2
output_has 'development stays paused: a newer pause'
journal_lacks done:unpause
expect old true up
noted resume-old

next_day
export BATCH_ATTEMPT=3
ST=${ST%-2}-3 JOURNAL=$ST/journal
owner resume
prep
owner pause
run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 2
output_has 'development stays paused: a newer pause'
journal_lacks done:unpause
expect new true up
noted resume
finish
