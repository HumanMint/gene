# shellcheck shell=bash
# The Gene phase starts at 07:29 and its fence waits for a job; the window closes meanwhile. Nothing
# switches: the phase resumes the old release itself instead of starting the switch after 07:30.
. "$(dirname "$0")/common.sh"
at 2026-10-28T07:20:00-07:00
prep
at 2026-10-28T07:29:00-07:00
as_owner "$GENE_PY" - "$BATCH_ROOT/data/controller/gene.sqlite3" <<'PY'
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES ('job-g','444','1','src-g','','in flight','deploying',?,?)", (time.time(), time.time()))
PY
cat > "$MOCK/on-sleep" <<X
#!/usr/bin/env bash
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$BATCH_ROOT/data/controller/gene.sqlite3") as db:
    db.execute("UPDATE jobs SET state=\"failed\" WHERE id=\"job-g\"")'
echo "\$(/bin/date -d 2026-10-28T07:30:05-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
chmod +x "$MOCK/on-sleep"
run 1 gene 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the fence never waited'
output_has 'the window closed before the Gene switch'
journal_lacks gene_attempted; journal_has resumed_old
expect old false up
! grep -q 'stop red' "$MOCK/docker.log" || fail 'Red was stopped'
finish
