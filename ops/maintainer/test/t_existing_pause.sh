# shellcheck shell=bash
# The owner paused development before the release, and root-owned DB journal files are present when
# drain starts. The release must not take ownership of that pause, so it stays paused afterwards.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
DB=$BATCH_ROOT/data/controller/gene.sqlite3
as_owner "$GENE_PY" -c 'import sqlite3, sys
with sqlite3.connect(sys.argv[1]) as db:
    db.execute("UPDATE settings SET value=\"true\" WHERE key=\"paused\"")' "$DB"
for file in "$DB-wal" "$DB-shm"; do
  [[ -e $file ]] || : > "$file"
  chown 0:0 "$file"; chmod 600 "$file"
done
run 0 drain 1
journal_lacks intent:pause
run 0 gene 0; run 0 fri 0; run 0 resume 1
expect new true up
finish
