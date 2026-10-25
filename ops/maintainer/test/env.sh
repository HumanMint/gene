# shellcheck shell=bash
# Sourced by the tests with MOCK set: run batch-release.sh against a mock host.
# Needs FRI_REPO (a fri checkout with its .venv) and this repo's .venv (uv sync --frozen --extra red).
# The live release defaults to the one before the first batched release; MOCK_OLD_G / MOCK_OLD_F pick
# another, and G or F there means the candidate itself is live.
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
G=$(git -C "$REPO" rev-parse HEAD) F=$(git -C "${FRI_REPO:?set FRI_REPO}" rev-parse HEAD)
FIRST_G=f66700140d91dc80df0bca8ae29ca671ca67f80e
OLD_G=${MOCK_OLD_G:-$FIRST_G} OLD_F=${MOCK_OLD_F:-6a1d410cbb7f04f36cd5be629cc48b05a221d653}
[[ $OLD_G == G ]] && OLD_G=$G
[[ $OLD_F == F ]] && OLD_F=$F
OLD_SOURCE=unmarked
[[ $OLD_G == "$FIRST_G" ]] || OLD_SOURCE=$OLD_G  # Releases from this script mark their source tree.
export OLD_G OLD_F OLD_SOURCE BATCH_ROOT=$MOCK/srv/gene BATCH_FRI_ROOT=$MOCK/srv/fri GENE_PY=$REPO/.venv/bin/python
export FRI_PY=$FRI_REPO/.venv/bin/python OLD_SRC=${OLD_SRC_ROOT:-$MOCK.old-src}/$OLD_G
export PATH=$HERE/bin:$PATH PROBE_WINDOW=${PROBE_WINDOW:-20} WAIT_WINDOW=${WAIT_WINDOW:-2}
export S=$REPO/ops/maintainer/batch-release.sh ST=$BATCH_ROOT/staging/batch-$G-$F
if [[ ! -d $OLD_SRC/src ]]; then  # The old release's code, run by the mock for gene-runtime:OLD_G.
  mkdir -p "$OLD_SRC"; chmod 755 "$(dirname "$OLD_SRC")" "$OLD_SRC"
  git -C "$REPO" archive "$OLD_G" src | tar -x -C "$OLD_SRC"
fi
upload() {  # What the release changes, as the operator uploads it.
  if [[ $G != "$OLD_G" ]]; then
    git -C "$REPO" archive --format=tar "$G" > "$ST/gene.tar"
    (cd "$ST" && sha256sum gene.tar > gene.tar.sha256)
  fi
  if [[ $F != "$OLD_F" ]]; then
    git -C "$FRI_REPO" archive --format=tar "$F" > "$ST/fri.tar"
    (cd "$ST" && sha256sum fri.tar > fri.tar.sha256)
  fi
}
