# shellcheck shell=bash
# A release that changes only Gene, with Fri live at F: Fri is not uploaded, rebuilt or restarted, and
# is checked against the new Gene. Then a rollback, which leaves Fri alone.
export MOCK_OLD_F=F
. "$(dirname "$0")/common.sh"
release
expect new false up
[[ ! -e $ST/fri.tar ]] || fail 'fri.tar was uploaded'
! grep -q -e '-t fri-runtime:' -e 'project-name fri' "$MOCK/docker.log" || fail 'a Gene-only release touched Fri'
run 0 rollback-gene 3
journal_lacks intent:fri_restore; purges 1
expect old true down
run 0 resume-old 1
expect old false up
finish
