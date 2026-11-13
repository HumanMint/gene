# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload
echo '# old lock' >> "$BATCH_ROOT/source/uv.lock"
# A complete environment whose later image build fails is abandoned.
MOCK_BUILD_FAIL=1 run 1 stage 2
journal_has host_venv_built
abandoned=$BATCH_ROOT/host-venvs/${ST##*/}
# Retry under another attempt and release it.
export BATCH_ATTEMPT=2
ST=$ST-2; JOURNAL=$ST/journal
release
first=$(readlink -f "$BATCH_ROOT/venv")
[[ $first != "$abandoned" ]] || fail 'attempt reused abandoned environment'
# A second source/dependency release must save the first stable alias for rollback.
next_day
G=$(git -C "$REPO" rev-parse HEAD^)
ST=$BATCH_ROOT/staging/batch-$G-$F-2; JOURNAL=$ST/journal
run 0 baseline 0; upload
echo '# force next environment' >> "$BATCH_ROOT/source/uv.lock"
run 0 stage 0; run 0 rehearse 0; run 0 drain 0
run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 0
live=$(readlink -f "$BATCH_ROOT/venv")
[[ $live != "$first" ]] || fail 'second release did not change environment'
KEEP_RELEASES=1 run 0 cleanup 2
[[ ! -e $abandoned ]] || fail 'cleanup kept abandoned environment'
[[ -x $live/bin/python ]] || fail 'cleanup removed live environment'
[[ -x $first/bin/python ]] || fail 'cleanup removed rollback environment'
run 0 rollback-gene 0
[[ $(readlink -f "$BATCH_ROOT/venv") == "$first" ]] || fail 'second rollback did not restore first environment'
run 0 resume-old 0
finish
