# shellcheck shell=bash
# Sourced by the tests with MOCK set: run batch-release.sh against a mock host.
# Needs FRI_REPO (a fri checkout with its .venv) and this repo's .venv (uv sync --frozen --extra red).
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
OLD_G=f66700140d91dc80df0bca8ae29ca671ca67f80e OLD_F=6a1d410cbb7f04f36cd5be629cc48b05a221d653
export OLD_G OLD_F BATCH_ROOT=$MOCK/srv/gene BATCH_FRI_ROOT=$MOCK/srv/fri GENE_PY=$REPO/.venv/bin/python
export FRI_PY=${FRI_REPO:?set FRI_REPO}/.venv/bin/python OLD_SRC=${OLD_SRC:-$MOCK.old-src}
export PATH=$HERE/bin:$PATH PROBE_WINDOW=${PROBE_WINDOW:-20} WAIT_WINDOW=${WAIT_WINDOW:-2}
G=$(git -C "$REPO" rev-parse HEAD) F=$(git -C "$FRI_REPO" rev-parse HEAD)
export S=$REPO/ops/maintainer/batch-release.sh
if [[ ! -d $OLD_SRC/src ]]; then  # The old release's code, run by the mock for gene-runtime:OLD_G.
  mkdir -p "$OLD_SRC"; chmod 755 "$OLD_SRC"
  git -C "$REPO" archive "$OLD_G" src | tar -x -C "$OLD_SRC"
fi
upload() {
  local st=$BATCH_ROOT/staging/batch-$G
  git -C "$REPO" archive --format=tar "$G" > "$st/gene.tar"
  git -C "$FRI_REPO" archive --format=tar "$F" > "$st/fri.tar"
  (cd "$st" && sha256sum gene.tar > gene.tar.sha256 && sha256sum fri.tar > fri.tar.sha256)
}
