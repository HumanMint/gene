# shellcheck shell=bash
# gene #70, the same stale pair without the abort: the drain's fence gives up while an automatic deployment
# the controller claimed after its gate is still deploying, so development stays paused before `drained`.
# The deployment then lands Gene H. The old pair is gone, and this batch changed nothing but its fence, so
# after the window resume-old resumes development at H, where the deployment left Gene.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
H=$(printf 'e%.0s' {1..40})
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
run 1 drain 2
output_has 'still building or deploying'
journal_has intent:pause; journal_lacks drained
[[ $(setting paused) == true ]] || fail 'the fence did not pause development'
as_owner "$GENE_PY" -c '
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE jobs SET state=\"live\" WHERE id=\"a1b2c3d4e5f6\"")
    db.execute("UPDATE deployments SET state=\"live\" WHERE job_id=\"a1b2c3d4e5f6\"")
    db.execute("UPDATE settings SET value=? WHERE key=\"previous_sha\"", (sys.argv[2],))
    db.execute("UPDATE settings SET value=? WHERE key=\"active_sha\"", (sys.argv[3],))' "$DB" "$OLD_G" "$H"
sed -i "s/^GENE_RELEASE_SHA=.*/GENE_RELEASE_SHA=$H/" "$BATCH_ROOT/release.env"
at 2026-10-28T05:30:00-07:00
run 4 drain 1
output_has 'Run resume-old.'
run 0 resume-old 2
output_has "at Gene ${H:0:7}"
journal_has intent:abort; journal_has resumed_old; journal_has done:unpause
units up
[[ $(setting paused) == false ]] || fail 'development was left paused'
grep -qx "GENE_RELEASE_SHA=$H" "$BATCH_ROOT/release.env" || fail 'release.env no longer names H'
finish
