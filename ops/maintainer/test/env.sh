# shellcheck shell=bash
# Sourced by the tests with MOCK set: run batch-release.sh against a mock host.
# Needs FRI_REPO (a fri checkout) and the gene repo's .venv (uv sync --frozen --extra red).
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd "$HERE/../../.." && pwd)
export BATCH_ROOT=$MOCK/srv/gene BATCH_FRI_ROOT=$MOCK/srv/fri GENE_PY=$REPO/.venv/bin/python
export PATH=$HERE/bin:$PATH PROBE_WINDOW=${PROBE_WINDOW:-8} WAIT_WINDOW=${WAIT_WINDOW:-2}
G=$(git -C "$REPO" rev-parse HEAD) F=$(git -C "${FRI_REPO:?set FRI_REPO}" rev-parse HEAD)
export S=$REPO/ops/maintainer/batch-release.sh
upload() {
  local st=$BATCH_ROOT/staging/batch-$G
  git -C "$REPO" archive --format=tar "$G" > "$st/gene.tar"
  git -C "$FRI_REPO" archive --format=tar "$F" > "$st/fri.tar"
  (cd "$st" && sha256sum gene.tar > gene.tar.sha256 && sha256sum fri.tar > fri.tar.sha256)
}
