# shellcheck shell=bash
# gene #121 round 2's B2: two rollback-gene → resume-old cycles on one batch, then a third rollback-gene whose fence
# gives up while a deployment is still deploying, which then lands Gene H. Each successful resume-old is journaled,
# so the retried rollback-gene sees development resumed after the second rollback's restore and resumes as it is,
# at H, instead of refusing with development paused.
. "$(dirname "$0")/common.sh"
H=$(printf 'e%.0s' {1..40})
release
run 0 rollback-gene 1
run 0 resume-old 1
run 0 rollback-gene 1
run 0 resume-old 1
[[ $(grep -cx resumed_old "$ST/journal") -ge 2 ]] || fail 'the second resume-old was not journaled'
deployment_in_flight job-h "$H" "$OLD_G"
run 1 rollback-gene 2
output_has 'still building or deploying'
[[ $(setting paused) == true ]] || fail 'the fence did not pause development'
deployment_lands job-h "$H" "$OLD_G"
at 2026-10-28T08:00:00-07:00
run 1 rollback-gene 3
output_has "resuming development as it is, at Gene ${H:0:7}"
[[ $(setting paused) == false ]] || fail 'development was left paused'
units up
finish
