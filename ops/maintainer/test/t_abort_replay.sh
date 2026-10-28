# shellcheck shell=bash
# gene #70: an automatic deployment the controller claims after the drain's gate lands Gene H during the
# fence and uses today's batch. The drain journals its abort, then the worker and deployer fail to start, so
# development stays paused with the units stopped. A new drain is refused and names resume-old, which after
# the window resumes development at H, where the deployment left Gene, rather than refusing the moved release.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
H=$(printf 'd%.0s' {1..40})
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
read -r base set < "$MOCK/clock"
cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
# The drain's first docker call, past its gate: the controller claims a feature deployment of H.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3, time
with sqlite3.connect("$DB") as db:
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) VALUES (?,?,?,?,?,?,?,?,?)",
               ("a1b2c3d4e5f6", "444", "1", "src-a", "", "in flight", "deploying", time.time(), time.time()))
    db.execute("INSERT INTO deployments VALUES (?,?,?,?,?)", ("a1b2c3d4e5f6", "$H", "$OLD_G", "deploying", $(( base + $(/bin/date +%s) - set ))))'
X
chmod +x "$MOCK/on-docker"
cat > "$MOCK/on-sleep" <<X
#!/usr/bin/env bash
# The deployment lands H, as the deployer leaves a feature: release.env and the release settings move.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE jobs SET state=\"live\" WHERE id=\"a1b2c3d4e5f6\"")
    db.execute("UPDATE deployments SET state=\"live\" WHERE job_id=\"a1b2c3d4e5f6\"")
    db.execute("UPDATE settings SET value=\"$OLD_G\" WHERE key=\"previous_sha\"")
    db.execute("UPDATE settings SET value=\"$H\" WHERE key=\"active_sha\"")'
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$H/" "$BATCH_ROOT/release.env"
X
chmod +x "$MOCK/on-sleep"
: > "$MOCK/start-fails"
run 1 drain 3
[[ -e $MOCK/on-docker.ran ]] || fail 'the deployment was never claimed'
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never landed during the fence'
output_has "today's batch was used during the drain (the controller claimed an automatic deployment today"
journal_has intent:abort; journal_lacks resumed_old; journal_lacks drained
units down
[[ $(setting paused) == true ]] || fail 'the fence did not pause development'
at 2026-10-28T05:30:00-07:00  # Restoring service runs at any hour.
run 4 drain 1
output_has 'Run resume-old: it resumes development as it is'
run 0 resume-old 2
output_has "at Gene ${H:0:7}"
journal_has resumed_old; journal_has done:unpause
units up
[[ $(setting paused) == false ]] || fail 'development was left paused'
[[ $(setting active_sha) == "$H" ]] && grep -qx "GENE_RELEASE_SHA=$H" "$BATCH_ROOT/release.env" || fail 'Gene is no longer H'
python3 - "$BATCH_ROOT/production-actions.jsonl" <<'PY' || fail 'the resume-old line in the action log is not ok'
import json, sys
row = [json.loads(line) for line in open(sys.argv[1])][-1]
sys.exit(0 if row['action'] == 'batch-release resume-old' and row['outcome'] == 'ok' else 1)
PY
run 0 resume-old 1  # A repeat is safe.
units up
[[ $(setting paused) == false ]] || fail 'the repeat paused development'
at 2026-10-29T04:05:00-07:00
run 4 gene 1
output_has 'already resumed the old release'
finish
