# shellcheck shell=bash
# The batch shell is killed while its Gene compose up is still running. Rollback refuses while that
# command lives, then reconciles once it has finished.
. "$(dirname "$0")/common.sh"
prep
MOCK_SLOW_UP=6 bash "$S" gene "$G" "$F" > "$MOCK/out-orphan-gene" 2>&1 &
shell=$!
for _ in $(seq 300); do [[ -e $MOCK/up-started ]] && break; /bin/sleep 0.1; done
[[ -e $MOCK/up-started ]] || fail 'the new Gene compose up never started'
kill -9 "$shell"; wait "$shell" 2>/dev/null
run 1 rollback-gene 1
output_has 'one a killed run left behind'
journal_lacks done:downgrade; purges 0
for _ in $(seq 300); do [[ -e $MOCK/up-finished ]] && break; /bin/sleep 0.1; done
[[ -e $MOCK/up-finished ]] || fail 'the orphaned compose up never finished'
/bin/sleep 0.3
run 0 rollback-gene 3
journal_has done:downgrade; purges 1
expect old true down
finish
