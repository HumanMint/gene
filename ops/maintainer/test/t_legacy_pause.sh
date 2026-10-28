# shellcheck shell=bash
# gene #71 review B1: a batch whose first phases ran the script from before pause audit ids (8deef14): its
# pause row names no batch and its journal has no paused:<id>. This script finishes it and lifts that pause,
# as the old script would have, unless the owner paused after it. resume-old; resume-old with an owner pause,
# which stays; then resume after a full release.
. "$(dirname "$0")/common.sh"
LEGACY=$MOCK/legacy-batch-release.sh
git -C "$REPO" show 8deef142e571c2e2126605f0a5e84d1cc35eee5f:ops/maintainer/batch-release.sh > "$LEGACY"
legacy_prep() {  # baseline to drain with the old script
  S=$LEGACY run 0 baseline 0; upload; S=$LEGACY run 0 stage 0; S=$LEGACY run 0 rehearse 0; S=$LEGACY run 0 drain 0
  journal_has intent:pause; journal_has drained
  ! grep -q '^paused:' "$JOURNAL" || fail 'the old script journaled a pause id'
}
legacy_prep
run 0 resume-old 2
output_has "development resumed: this batch's pause"
journal_has done:unpause
expect old false up

next_day  # A batch that resumed the old release is done: a retry is a new batch.
export BATCH_ATTEMPT=2
ST=$ST-2 JOURNAL=$ST/journal  # left to right: JOURNAL sees the new ST
legacy_prep
owner_sets pause
run 0 resume-old 2
output_has 'development stays paused: the latest pause'
journal_lacks done:unpause
expect old true up

next_day
export BATCH_ATTEMPT=3
ST=${ST%-2}-3 JOURNAL=$ST/journal
owner_sets resume
legacy_prep
run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 2
journal_has done:unpause
expect new false up
finish
