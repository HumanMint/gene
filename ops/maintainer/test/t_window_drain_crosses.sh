# shellcheck shell=bash
# The drain starts at 07:29 and waits for a building job; the window closes during the wait. The drain
# finishes its fence but switches nothing: it resumes the old release itself, and the batch is done.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
at 2026-10-28T07:29:00-07:00
as_owner "$GENE_PY" - "$BATCH_ROOT/data/controller/gene.sqlite3" <<'PY'
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES ('job-b','444','1','src-b','','in flight','building',?,?)", (time.time(), time.time()))
PY
cat > "$MOCK/on-sleep" <<X
#!/usr/bin/env bash
# The job finishes, and by now it is past 07:30.
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$BATCH_ROOT/data/controller/gene.sqlite3") as db:
    db.execute("UPDATE jobs SET state=\"failed\" WHERE id=\"job-b\"")'
echo "\$(/bin/date -d 2026-10-28T07:30:20-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
chmod +x "$MOCK/on-sleep"
run 1 drain 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the drain never waited'
output_has 'the window closed during the drain'
journal_has drained; journal_has resumed_old; journal_lacks gene_attempted
expect old false up
at 2026-10-29T04:05:00-07:00
run 4 gene 1
output_has 'already resumed the old release'
finish
