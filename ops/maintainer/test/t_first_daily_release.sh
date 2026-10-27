# shellcheck shell=bash
# The first daily release, from the exact pair live today (Gene 7d6e36a, Fri 261b47c), which this script's
# earlier revision installed with its own file names: staging/batch-<G> and maintainer-<G>-result.json.
# Those are left alone through a release, a rollback after resume, a retry and cleanup.
export MOCK_OLD_G=7d6e36a0e4621bbce75dd9c404b4864b90699b19 MOCK_OLD_F=261b47c9d31eb726f7f9b6de6c7713fe8dc27a91
. "$(dirname "$0")/common.sh"
legacy=$BATCH_ROOT/staging/batch-$OLD_G
mkdir -p "$legacy"
printf 'baselined\nstaged\nrehearsed\ndrained\ngene_recorded\nfri_switched\nresumed\n' > "$legacy/journal"
touch -d 2026-10-20T03:30:00-07:00 "$legacy/journal"  # It drained on an earlier day.
echo '{"gene": "'"$OLD_G"'", "fri": "'"$OLD_F"'", "old": {}, "new": {}}' > "$legacy/manifest.json"
echo '{"sha": "'"$OLD_G"'", "previous": "f66700140d91dc80df0bca8ae29ca671ca67f80e", "success": true}' > "$BATCH_ROOT/maintainer-$OLD_G-result.json"
before=$(cd "$BATCH_ROOT" && sha256sum "staging/batch-$OLD_G/journal" "staging/batch-$OLD_G/manifest.json" "maintainer-$OLD_G-result.json")
release
expect new false up
run 0 rollback-gene 1
run 0 resume-old 1
expect old false up
mv "$BATCH_ROOT/releases/$G" "$BATCH_ROOT/releases/$G.attempt-1"
next_day  # A retry is a new batch, and one batch drains per local day.
export BATCH_ATTEMPT=2
ST=$ST-2
release
expect new false up
run 0 cleanup 1
[[ $before == "$(cd "$BATCH_ROOT" && sha256sum "staging/batch-$OLD_G/journal" "staging/batch-$OLD_G/manifest.json" "maintainer-$OLD_G-result.json")" ]] \
  || fail 'the earlier release'\''s files changed'
[[ -f $BATCH_ROOT/maintainer-$G-$F-result.json && -f $BATCH_ROOT/maintainer-$G-$F-2-result.json ]] || fail 'a result file is missing'
finish
