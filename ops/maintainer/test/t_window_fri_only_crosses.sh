# shellcheck shell=bash
# A Fri-only release: the Fri phase's fence waits past 05:00. Fri is never switched; the phase resumes
# the old release itself.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
at 2026-10-28T04:55:00-07:00
prep
run 0 gene 0
as_owner "$GENE_PY" - "$BATCH_ROOT/data/controller/gene.sqlite3" <<'PY'
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO jobs(id,guild_id,user_id,source_id,source_url,body,state,created,updated) "
               "VALUES ('job-f','444','1','src-f','','in flight','building',?,?)", (time.time(), time.time()))
PY
cat > "$MOCK/on-sleep" <<X
#!/usr/bin/env bash
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$BATCH_ROOT/data/controller/gene.sqlite3") as db:
    db.execute("UPDATE jobs SET state=\"failed\" WHERE id=\"job-f\"")'
echo "\$(/bin/date -d 2026-10-28T05:01:00-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
chmod +x "$MOCK/on-sleep"
fri_before=$(cd "$BATCH_ROOT/../fri" && sha256sum release.env release.json)
run 1 fri 2
[[ -e $MOCK/on-sleep.ran ]] || fail 'the fence never waited'
output_has 'the window closed before the Fri switch'
journal_lacks intent:fri; journal_has resumed_old
[[ $fri_before == "$(cd "$BATCH_ROOT/../fri" && sha256sum release.env release.json)" ]] || fail 'Fri was changed'
expect old false up
finish
