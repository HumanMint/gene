# shellcheck shell=bash
# The script is killed right after a mutation (the worker pin), then rolled back.
. "$(dirname "$0")/common.sh"
prep
mkdir -p "$MOCK/killbin"
cat > "$MOCK/killbin/sed" <<'X'
#!/usr/bin/env bash
case "$*" in *GENE_WORKER_IMAGE=gene-worker*) kill -9 $PPID ;; esac
exec /usr/bin/sed "$@"
X
chmod +x "$MOCK/killbin/sed"
PATH=$MOCK/killbin:$PATH run 137 gene 1
journal_has intent:worker
run 0 rollback-gene 3
purges 0
expect old true down
finish
