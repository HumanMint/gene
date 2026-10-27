# shellcheck shell=bash
# The drain pauses at 04:50 and a job keeps building past its wait, so it stops before `drained`, with
# development paused. After 05:00 a new drain is refused, but resume-old still restores service.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
at 2026-10-28T04:50:00-07:00
as_owner "$GENE_PY" - "$BATCH_ROOT/data/controller/gene.sqlite3" <<'PY'
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES ('job-t','444','1','src-t','','in flight','building',?,?)", (time.time(), time.time()))
PY
run 1 drain 2
output_has 'still building or deploying'
journal_has intent:pause; journal_lacks drained
as_owner "$GENE_PY" -c "
import sqlite3
with sqlite3.connect('$BATCH_ROOT/data/controller/gene.sqlite3') as db:
    db.execute('UPDATE jobs SET state=\"failed\" WHERE id=\"job-t\"')"
at 2026-10-28T05:30:00-07:00
run 4 drain 1
output_has 'Run resume-old.'
run 0 resume-old 1
expect old false up
finish
