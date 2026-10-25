# shellcheck shell=bash
# The probes that decide which downgrades a rollback needs fail to run (docker exits 125). The
# rehearsal stops rather than read that as "module absent". Repeated, it records the list, and the
# rollback runs exactly that list without probing again.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload; run 0 stage 0
MOCK_PROBE_FAIL=1 run 1 rehearse 3
output_has 'could not check the image for gene.fri_reply_checks'
journal_lacks rehearsed
run 0 rehearse 0
output_has 'a rollback downgrades: gene.fri_reply_checks gene.profile_downgrade'
run 0 drain 0; run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 0
as_owner "$GENE_PY" -c '
import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("INSERT INTO personal_note_removals VALUES (?,?,?,?,?)", ("444", "server", "9", "removed-after-release", 1))' \
  "$BATCH_ROOT/data/controller/gene.sqlite3"
MOCK_PROBE_FAIL=1 run 0 rollback-gene 3
kept=$("$GENE_PY" -c '
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
print(db.execute("SELECT group_concat(channel_id) FROM personal_note_removals WHERE digest=?", ("removed-after-release",)).fetchone()[0])' \
  "$BATCH_ROOT/data/controller/gene.sqlite3")
[[ $kept == public:777 ]] || fail "profile removal after rollback: $kept"
journal_has done:downgrade; purges 1
expect old true down
run 0 resume-old 1
expect old false up
finish
