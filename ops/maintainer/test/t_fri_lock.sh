# shellcheck shell=bash
# A Fri coordinator still holds its lock (its batch shell was killed) while the old Fri looks
# pinned: rollback refuses before touching Fri or Gene, then succeeds once the lock is free.
. "$(dirname "$0")/common.sh"
prep
run 0 gene 0
flock -o "$BATCH_FRI_ROOT/release.lock" /bin/sleep 120 &
holder=$!
/bin/sleep 0.5
run 1 rollback-gene 1
output_has 'a Fri release holds its lock'
journal_lacks intent:fri_restore; journal_lacks done:downgrade; purges 0
[[ $(cat "$BATCH_ROOT/source/.release-sha" 2>/dev/null) == "$G" ]] || fail 'Gene was changed while the lock was held'
kill "$holder"; wait "$holder" 2>/dev/null
run 0 rollback-gene 2
purges 1
expect old true down
finish
