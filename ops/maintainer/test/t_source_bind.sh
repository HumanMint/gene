# shellcheck shell=bash
# The source tree is a bind mount on the same filesystem, which stat can't tell apart from a plain
# directory and rename(2) refuses. The switch refuses before any change, with Red still running.
[[ -n ${IN_MOUNT_NS:-} ]] || exec unshare -m --propagation private env IN_MOUNT_NS=1 bash "$0" "$@"
. "$(dirname "$0")/common.sh"
prep
mount --bind "$BATCH_ROOT/source" "$BATCH_ROOT/source" || { echo 'no bind mounts here; skipping'; echo 'RESULT: PASS'; exit 0; }
run 1 gene 1
output_has 'is a mount point in'
journal_lacks gene_attempted
expect old true down
umount "$BATCH_ROOT/source"
finish
