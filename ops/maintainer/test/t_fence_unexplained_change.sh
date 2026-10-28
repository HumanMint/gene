# shellcheck shell=bash
# gene #121 round 1: rollback-gene resumes development as it is only when a deployment explains everything that
# changed during its fence. Two changes no deployment explains keep the fence for the operator to reconcile:
# release.env naming another runtime image under the same Gene, and a deployment that lands beside a Fri record
# from outside the recorded pair. With the image put back, a repeated rollback-gene finishes the rollback. With
# the Fri record put back, only the deployment is left: a repeated rollback-gene, which has still changed nothing
# but its fence, resumes development as it is, at the deployed Gene. Both repeats run at any hour.
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
H=$(printf 'e%.0s' {1..40})
release
fenced() {  # the rollback stopped with development fenced and nothing resumed as it is
  units down
  [[ $(setting paused) == true ]] || fail 'development is not paused'
  journal_lacks intent:abort
}
# A deployment in flight fails, and release.env is left naming another runtime image under the same Gene.
deployment_in_flight job-1 "$H" "$G"
on_sleep <<X
as_owner "$GENE_PY" -c '
import sqlite3
with sqlite3.connect("$DB") as db:
    db.execute("UPDATE jobs SET state=\\"failed\\" WHERE id=\\"job-1\\"")
    db.execute("UPDATE deployments SET state=\\"failed\\" WHERE job_id=\\"job-1\\"")'
sed -i "s/^GENE_RUNTIME_IMAGE=.*/GENE_RUNTIME_IMAGE=gene-runtime:$OLD_G/" "$BATCH_ROOT/release.env"
X
run 1 rollback-gene 3
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
output_has 'outside the recorded old/new pair (release.env); reconcile by hand'
fenced
at 2026-10-28T08:00:00-07:00
sed -i "s/^GENE_RUNTIME_IMAGE=.*/GENE_RUNTIME_IMAGE=gene-runtime:$G/" "$BATCH_ROOT/release.env"
run 0 rollback-gene 2
expect old true down
run 0 resume-old 1
expect old false up
# A second rollback: a deployment of H over the old release lands during its fence, beside a third Fri record.
rm -f "$MOCK/on-sleep.ran"
cp "$BATCH_FRI_ROOT/release.json" "$MOCK/fri-record"
deployment_in_flight job-2 "$H" "$OLD_G"
on_sleep <<X
deployment_lands job-2 $H $OLD_G
echo '{"sha": "$(printf 'f%.0s' {1..40})", "previous": "$OLD_F"}' > "$BATCH_FRI_ROOT/release.json"
X
run 1 rollback-gene 3
[[ -e $MOCK/on-sleep.ran ]] || fail 'the deployment never finished during the fence'
output_has 'outside the recorded old/new pair (release.env, fri release.json, release settings); reconcile by hand'
fenced
cat "$MOCK/fri-record" > "$BATCH_FRI_ROOT/release.json"  # The operator puts the Fri record back.
run 1 rollback-gene 3
output_has "a deployment moved Gene to ${H:0:7}"
output_has "resuming development as it is, at Gene ${H:0:7}"
output_has 'nothing was rolled back'
journal_has intent:abort
[[ $(tail -n 1 "$JOURNAL") == resumed_old ]] || fail 'the stop was not restored'
units up
[[ $(setting paused) == false ]] || fail 'development was left paused'
[[ $(setting active_sha) == "$H" ]] || fail 'the active release is no longer H'
grep -qx "GENE_RELEASE_SHA=$H" "$BATCH_ROOT/release.env" || fail 'release.env no longer names H'
run 0 resume-old 2  # A repeat stays at H.
output_has 'already resumed development as it is'
units up
finish
