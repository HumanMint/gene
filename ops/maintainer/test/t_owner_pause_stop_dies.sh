# shellcheck shell=bash
# gene #94: the owner paused development before the release, and the drain's fence dies right after it stops
# the worker and deployer. The batch journaled that stop first without taking the owner's pause for its own,
# so after the window resume-old starts the units again, leaves the owner's pause in place and says so.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
owner_sets pause
: > "$MOCK/stop-fails"
run 1 drain 2
output_has 'the worker and deployer did not stop'
units down
journal_has skipped:pause; journal_lacks drained
! grep -q '^paused:' "$JOURNAL" || fail 'the batch took a pause of its own'
[[ $(setting paused) == true ]] || fail "the owner's pause was lifted"
at 2026-10-28T08:00:00-07:00  # Restoring service runs at any hour.
run 4 drain 1
output_has 'Run resume-old.'
run 0 resume-old 2
output_has 'someone else paused it first'
noted resume-old
journal_has resumed_old; journal_lacks done:unpause
expect old true up
run 0 resume-old 1  # A repeat is safe, and the owner's pause still stays.
expect old true up
finish
