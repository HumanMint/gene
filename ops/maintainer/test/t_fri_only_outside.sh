# shellcheck shell=bash
# After a Fri-only release, another Fri release lands. rollback-fri refuses without changing anything.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
release
echo "{\"sha\": \"$(printf '4%.0s' {1..40})\", \"previous\": \"$F\"}" > "$BATCH_FRI_ROOT/release.json"
run 1 rollback-fri 1
output_has 'outside the recorded old/new pair (fri release.json)'
journal_lacks intent:rollback; journal_lacks intent:fri_restore
finish
