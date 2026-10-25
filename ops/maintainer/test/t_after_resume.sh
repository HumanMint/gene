# shellcheck shell=bash
# A rollback after a completed release and resume.
. "$(dirname "$0")/common.sh"
release
expect new false up
as_owner "$GENE_PY" -c '
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO personal_note_removals VALUES (?,?,?,?,?)", ("444", "server", "9", "removed-after-release", 1))' \
  "$BATCH_ROOT/data/controller/gene.sqlite3"
run 0 rollback-gene 3
kept=$("$GENE_PY" -c '
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print(db.execute("SELECT group_concat(channel_id) FROM personal_note_removals WHERE digest=?", ("removed-after-release",)).fetchone()[0])' \
  "$BATCH_ROOT/data/controller/gene.sqlite3")
[[ $kept == public:777 ]] || fail "profile removal after rollback: $kept"
journal_has fri_restored
expect old true down
run 0 resume-old 1
expect old false up
finish
