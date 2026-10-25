# shellcheck shell=bash
# A Fri phase dies whole, leaving root-owned DB journal files that the owner cannot open (as a
# SQLite build that doesn't hand them to the DB owner would). Rollback recovers them first.
. "$(dirname "$0")/common.sh"
prep
run 0 gene 0
MOCK_FRI_CRASH=$F run 1 fri 1
DB=$BATCH_ROOT/data/controller/gene.sqlite3
for file in "$DB-wal" "$DB-shm"; do  # Keeps any content; only the owner and mode change.
  [[ -e $file ]] || : > "$file"
  chown 0:0 "$file"; chmod 600 "$file"
done
run 0 rollback-gene 3
journal_has fri_restored; purges 1
expect old true down
finish
