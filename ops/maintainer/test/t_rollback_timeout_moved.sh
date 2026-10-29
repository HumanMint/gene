# shellcheck shell=bash
# gene #121 round 1, rollback-gene's version of t_timeout_moved: after resume, its fence gives up while a deployment
# is still deploying, so development stays paused with the worker and deployer running. The deployment then lands
# Gene H. The old pair is gone, and the rollback changed nothing but its fence, so a repeated rollback-gene, at any
# hour, resumes development as it is, at H, rather than refusing the moved release with development paused.
. "$(dirname "$0")/common.sh"
H=$(printf 'e%.0s' {1..40})
release
deployment_in_flight job-h "$H" "$G"
run 1 rollback-gene 2
output_has 'still building or deploying'
journal_has intent:rollback; journal_lacks intent:restore
units up
[[ $(setting paused) == true ]] || fail 'the fence did not pause development'
deployment_lands job-h "$H" "$G"
at 2026-10-28T08:00:00-07:00
run 1 rollback-gene 3
output_has "resuming development as it is, at Gene ${H:0:7}"
output_has 'nothing was rolled back'
journal_has intent:abort; journal_has done:unpause
units up
[[ $(setting paused) == false ]] || fail 'development was left paused'
grep -qx "GENE_RELEASE_SHA=$H" "$BATCH_ROOT/release.env" || fail 'release.env no longer names H'
[[ $(cat "$BATCH_ROOT/source/.release-sha") == "$G" ]] || fail 'the source tree was changed'
journal_lacks intent:fri_restore; journal_lacks done:downgrade; journal_lacks gene_rolled_back; purges 0
run 0 resume-old 2  # A repeat stays at H.
output_has 'already resumed development as it is'
units up
finish
