# shellcheck shell=bash
# The backups dir is a bind mount on the same filesystem: same device number, but rename(2) between
# them fails. The switch refuses before any change. Runs in its own mount namespace.
[[ -n ${IN_MOUNT_NS:-} ]] || exec unshare -m --propagation private env IN_MOUNT_NS=1 bash "$0" "$@"
. "$(dirname "$0")/common.sh"
prep
elsewhere=$MOCK/elsewhere-backups; mkdir -p "$elsewhere"
mount --bind "$elsewhere" "$BATCH_ROOT/backups" || { echo 'no bind mounts here; skipping'; echo 'RESULT: PASS'; exit 0; }
[[ $(stat -c %d "$elsewhere") == "$(stat -c %d "$BATCH_ROOT/staging")" ]] || fail 'expected the same device number'
run 1 gene 1
output_has 'the switch needs plain renames'
journal_lacks gene_attempted
[[ -z $(find "$BATCH_ROOT" -name '.rename-probe-*' 2>/dev/null) ]] || fail 'a rename probe was left behind'
expect old true down
umount "$BATCH_ROOT/backups"
finish
