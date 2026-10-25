# shellcheck shell=bash
# A release from one this script installed: the live source is marked, and the old release already
# has reply checks and member profiles. A rollback downgrades nothing, so neither feature's data is lost.
export MOCK_OLD_G=7d6e36a0e4621bbce75dd9c404b4864b90699b19
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
release
expect new false up
[[ $(python3 -c 'import json, sys; print(repr(json.load(open(sys.argv[1]))["downgrades"]))' "$ST/manifest.json") == "''" ]] \
  || fail 'the rehearsal planned a downgrade the old release does not need'
as_owner "$GENE_PY" -c '
import sqlite3, sys, time
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO personal_note_removals VALUES (?,?,?,?,?)", ("444", "server", "9", "removed-after-release", 1))
    db.execute("INSERT INTO fri_reply_findings VALUES (1,?,?,?,?,?,?,?,?,?,?)",
               (time.time(), time.time(), "9", 0, "p", "space", "tone", "low", "topic", "fix"))' "$DB"
run 0 rollback-gene 3
kept=$("$GENE_PY" -c '
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print(db.execute("SELECT group_concat(channel_id) FROM personal_note_removals WHERE digest=?", ("removed-after-release",)).fetchone()[0],
      db.execute("SELECT count(*) FROM fri_reply_findings").fetchone()[0])' "$DB")
[[ $kept == 'server 1' ]] || fail "the rollback changed data the old release reads: $kept"
journal_has done:downgrade; purges 0
expect old true down
run 0 resume-old 1
expect old false up
finish
