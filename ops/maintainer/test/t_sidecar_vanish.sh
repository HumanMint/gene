# shellcheck shell=bash
# The controller's SQLite removes a DB journal file between the script's look and its use, as it did
# right after the new controller started on 2026-10-28: baseline's owner check sees the WAL vanish at
# its stat, and after the switch one file vanishes at the stat and another at the chown. Nothing fails.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
RACE=$MOCK/race-bin
mkdir -p "$RACE"
for tool in stat chown; do  # One call on the armed file finds it already gone, then the real tool runs.
  cat > "$RACE/$tool" <<X
#!/usr/bin/env bash
for file; do :; done
if [[ -f \$MOCK/vanish-at-$tool && \$file == "\$(cat "\$MOCK/vanish-at-$tool")" ]]; then rm -f "\$file" "\$MOCK/vanish-at-$tool"; fi
exec $(command -v "$tool") "\$@"
X
done
cat > "$RACE/docker" <<X
#!/usr/bin/env bash
# Once the new controller and Red are up, the controller's SQLite has a WAL that is gone by the stat,
# and a root-owned SHM (as a root SQLite client leaves one) that is gone by the chown.
[[ " \$* " == *" up "*" controller "* ]] || exec "$H/bin/docker" "\$@"
"$H/bin/docker" "\$@" || exit
if [[ \$(sed -n 's/^GENE_RELEASE_SHA=//p' "\$BATCH_ROOT/release.env") == "\$G" ]]; then
  : > "$DB-wal"; chown 10001:10001 "$DB-wal"; echo "$DB-wal" > "\$MOCK/vanish-at-stat"
  : > "$DB-shm"; echo "$DB-shm" > "\$MOCK/vanish-at-chown"
fi
X
chmod 755 "$RACE"/*
export PATH=$RACE:$PATH G

as_owner touch "$DB-wal"; echo "$DB-wal" > "$MOCK/vanish-at-stat"
prep
[[ ! -e $MOCK/vanish-at-stat ]] || fail 'baseline never met the vanishing WAL'
run 0 gene 3
[[ ! -e $MOCK/vanish-at-stat && ! -e $MOCK/vanish-at-chown ]] || fail 'the switch never met the vanishing journal files'
output_has 'gene.sqlite3-shm was removed or replaced meanwhile; nothing to repair'
journal_lacks gene_rolled_back
run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 0
expect new false up
finish
