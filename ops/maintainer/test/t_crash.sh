# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
prep
mkdir -p "$MOCK/killbin"
cat > "$MOCK/killbin/sed" <<'X'
#!/usr/bin/env bash
case "$*" in *GENE_WORKER_IMAGE=gene-worker*) kill -9 $PPID ;; esac
exec /usr/bin/sed "$@"
X
chmod +x "$MOCK/killbin/sed"
PATH=$MOCK/killbin:$PATH run gene 3
echo '== state after crash'; summary
run rollback-gene 10
summary
