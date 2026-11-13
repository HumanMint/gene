# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload
# A candidate lock different from live; the archive itself remains an exact git archive.
echo '# old lock' >> "$BATCH_ROOT/source/uv.lock"
old_python=$(sha256sum "$BATCH_ROOT/venv/bin/python")
MOCK_UV_FAIL=1 run 1 stage 2
journal_lacks staged
[[ $(sha256sum "$BATCH_ROOT/venv/bin/python") == "$old_python" ]] || fail 'stage changed live venv'
run 0 stage 2
journal_has host_venv_built
[[ $(wc -l < "$MOCK/uv.log") == 2 ]] || fail 'retry did not rebuild'
[[ $(cat "$BATCH_ROOT/host-venvs/${ST##*/}/lib/python3.11/site-packages/_editable_impl_gene_control.pth") == "$BATCH_ROOT/source/src" ]] || fail 'editable points at staging'
run 0 rehearse 0; run 0 drain 0; run 0 gene 2
[[ $(readlink "$BATCH_ROOT/venv") == "$BATCH_ROOT/host-venvs/${ST##*/}" ]] || fail 'venv not switched'
# Prove fri executes this interpreter, rather than the old stub.
sed -i '/^import fcntl/i open(__import__("os").environ["MOCK"]+"/new-venv-used", "w").close()' "$BATCH_ROOT/host-venvs/${ST##*/}/bin/python"
run 0 fri 2
[[ -f $MOCK/new-venv-used ]] || fail 'fri did not use new venv'
run 0 rollback-gene 2
[[ ! -L $BATCH_ROOT/venv && $(sha256sum "$BATCH_ROOT/venv/bin/python") == "$old_python" ]] || fail 'old venv not restored'
expect old true down
run 0 rollback-gene 0
run 0 resume-old 0; run 0 cleanup 0
[[ ! -e $BATCH_ROOT/host-venvs/${ST##*/} ]] || fail 'cleanup kept an unused rolled-back environment'
finish
