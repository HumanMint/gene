# shellcheck shell=bash
# controller.env names no chat channels, so the profile downgrade a rollback needs would refuse.
# The rehearsal finds that on its copy, before the fence or any switch.
. "$(dirname "$0")/common.sh"
sed -i "/^FRI_CHANNEL_IDS=/d" "$BATCH_ROOT/secrets/controller.env"
run 0 baseline 0; upload; run 0 stage 0
run 1 rehearse 3
output_has 'Profile removals need the configured channels'
journal_lacks rehearsed
[[ ! -e $BATCH_ROOT/staging/batch-$G/dbtest ]] || fail "the rehearsal copies were left behind"
expect old false up
finish
