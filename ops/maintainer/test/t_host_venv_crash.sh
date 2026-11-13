# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload
echo '# old lock' >> "$BATCH_ROOT/source/uv.lock"
run 0 stage 0; run 0 rehearse 0; run 0 drain 0
mkdir -p "$MOCK/killbin"
cat > "$MOCK/killbin/ln" <<'X'
#!/usr/bin/env bash
/bin/ln "$@"
kill -9 $PPID
X
chmod +x "$MOCK/killbin/ln"
PATH=$MOCK/killbin:$PATH run 137 gene 1
journal_has intent:venv_swap
run 0 rollback-gene 2
[[ ! -L $BATCH_ROOT/venv && -x $BATCH_ROOT/venv/bin/python ]] || fail 'interrupted switch did not restore venv'
expect old true down
run 0 rollback-gene 0
finish
