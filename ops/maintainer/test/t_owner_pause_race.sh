# shellcheck shell=bash
# gene #71 review S1: the owner pauses through /pause after the drain's fence read the setting and before the
# batch's pause. The batch's own transaction finds development already paused, so it takes no pause and lifts
# none: after resume-old development stays paused. gene #78: resume-old says so in its log, its action-log
# note and its exit message.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
# The batch's pause is the DB helper run with only its staging dir's name: the owner pauses right before it.
[[ \${*: -2} == '- ${ST##*/}' ]] || exit 1
setpriv --reuid=10001 --regid=10001 --clear-groups "$GENE_PY" -c '
import sqlite3, time
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE settings SET value=\"true\" WHERE key=\"paused\"")
    db.execute("INSERT INTO audit(at,actor,action,target,detail) VALUES (?,?,?,?,?)", (time.time(), "555", "pause", "", ""))'
X
chmod +x "$MOCK/on-docker"
run 0 drain 2
[[ -e $MOCK/on-docker.ran ]] || fail 'the owner never paused'
journal_has intent:pause; journal_has skipped:pause; journal_has drained
! grep -q '^paused:' "$JOURNAL" || fail 'the batch journaled a pause it did not take'
run 0 resume-old 1
journal_lacks done:unpause
expect old true up
output_has 'development stays paused: someone else paused it first (audit'
tail -n 1 "$MOCK/out-$RUNS-resume-old" | grep -q '^== development stays paused' || fail 'the exit message does not say development stays paused'
noted resume-old 'development stays paused: someone else paused it first'
finish
