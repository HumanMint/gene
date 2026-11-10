# shellcheck shell=bash
# Real Python command construction on a switch; real shell construction on rollback.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
overlay=$BATCH_FRI_ROOT/compose.calling.yml
printf 'services:\n  runtime:\n    stop_grace_period: 30s\n' > "$overlay"
chmod 600 "$overlay"
release
expect new false up
chmod 644 "$overlay"
run 0 rollback-fri 1
run 0 resume-old 1
expect old false up
python3 - "$MOCK/compose-commands.jsonl" "$overlay" <<'PY' || fail 'overlay lost during switch or rollback'
import json, sys
rows = [json.loads(line) for line in open(sys.argv[1])]
fri = [row for row in rows if row['project'] == 'fri']
assert fri and {'stop', 'up'} <= {row['action'] for row in fri}
assert all(len(row['files']) == 2 and row['files'][1] == sys.argv[2] for row in fri)
PY

# Invoke the production shell function with the mock Docker, without another release.
function_body=$(sed -n '/^fri_compose() {/,/^}/p' "$S")
invoke() { ROOT=$BATCH_ROOT FRI_ROOT=$BATCH_FRI_ROOT bash -c \
  'die() { echo "$*" >&2; exit 82; }; eval "$1"; fri_compose stop runtime' _ "$function_body"; }
before=$(wc -l < "$MOCK/compose-commands.jsonl")
chmod 666 "$overlay"
! invoke || fail 'writable overlay accepted'
chmod 600 "$overlay"; chown 10001:10001 "$overlay"
! invoke || fail 'non-root overlay accepted'
chown 0:0 "$overlay"
mv "$overlay" "$overlay.target"; ln -s "$overlay.target" "$overlay"
! invoke || fail 'symlink overlay accepted'
rm "$overlay" "$overlay.target"; mkdir "$overlay"
! invoke || fail 'directory overlay accepted'
rmdir "$overlay"
[[ $(wc -l < "$MOCK/compose-commands.jsonl") == "$before" ]] || fail 'invalid overlay reached Docker'
invoke || fail 'absent overlay changed launch behavior'
python3 - "$MOCK/compose-commands.jsonl" <<'PY' || fail 'absent overlay added a compose file'
import json, sys
row = json.loads(open(sys.argv[1]).read().splitlines()[-1])
assert len(row['files']) == 1
PY
# Restore the mock service stopped by the final direct-function check.
ROOT=$BATCH_ROOT FRI_ROOT=$BATCH_FRI_ROOT bash -c \
  'die() { exit 82; }; eval "$1"; fri_compose up -d --no-deps --force-recreate runtime' _ "$function_body" || fail 'restart failed'
finish
