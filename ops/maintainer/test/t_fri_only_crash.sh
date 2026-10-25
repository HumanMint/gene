# shellcheck shell=bash
# A Fri-only release whose coordinator dies mid-switch: new pins and container, old record. A retry
# fails without retaking the backup. rollback-fri restores the old Fri from it; a repeat changes nothing.
export MOCK_OLD_G=G
. "$(dirname "$0")/common.sh"
prep
run 0 gene 1
output_has 'Gene stays at'
MOCK_FRI_CRASH=$F run 1 fri 2
output_has 'rollback-fri restores it'
run 1 fri 1
[[ $(find "$BATCH_ROOT/backups" -maxdepth 1 -name "maintainer-fri-$F-*" | wc -l) == 1 ]] || fail 'the Fri backup was retaken'
run 1 resume 1
run 0 rollback-fri 2
journal_has fri_restored
sed -i '/^fri_restored$/d; /^intent:fri_restore$/d' "$JOURNAL"
run 0 rollback-fri 2
journal_lacks intent:fri_restore
expect old true down
run 0 resume-old 1
expect old false up
finish
