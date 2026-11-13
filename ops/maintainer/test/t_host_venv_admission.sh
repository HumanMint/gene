# shellcheck shell=bash
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload
echo '# old lock' >> "$BATCH_ROOT/source/uv.lock"
# Fake only sizing, retaining the real admission grouping and reserve arithmetic.
mkdir -p "$MOCK/spacebin"
real_python=$(command -v python3)
cat > "$MOCK/spacebin/python3" <<SHIM
#!/usr/bin/env bash
if [[ \$1 == - ]]; then
  code=\$(cat)
  if [[ \$code == *'need = {}'* ]]; then
    { cat <<'PY'
import os, types
partial = os.path.exists(os.environ['BATCH_ROOT']+'/host-venvs/'+os.path.basename(os.environ['ST']))
free = int(os.environ['MOCK_FREE']) - (2**30 if partial else 0)
os.statvfs = lambda path: types.SimpleNamespace(f_bavail=free, f_frsize=1)
PY
      printf '%s\n' "\$code"
    } | "$real_python" "\$@"
  else
    printf '%s\n' "\$code" | "$real_python" "\$@"
  fi
else
  exec "$real_python" "\$@"
fi
SHIM
cat > "$MOCK/spacebin/du" <<'SHIM'
#!/usr/bin/env bash
for path; do :; done
if [[ $path == "$BATCH_ROOT/venv" ]]; then
  printf '1073741824\t%s\n' "$path"
else
  exec /usr/bin/du "$@"
fi
SHIM
chmod +x "$MOCK/spacebin/"*
# 3 GiB images plus 2 GiB venv and the reserve cannot fit in 6 GiB.
cp "$MOCK/docker.json" "$MOCK/docker.original"
python3 - "$MOCK/docker.json" <<'PY'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
for image in d['images'].values():
    image['size'] = 2**30
json.dump(d, open(p, 'w'))
PY
MOCK_FREE=6442450944 PATH=$MOCK/spacebin:$PATH run 1 stage 2
output_has 'not enough disk'
[[ ! -f $MOCK/uv.log ]] || fail 'combined budget admitted a venv build'
# With tiny images, a partial 1 GiB build must be reclaimed before either admission.
cp "$MOCK/docker.original" "$MOCK/docker.json"
mkdir -p "$BATCH_ROOT/host-venvs/${ST##*/}"
MOCK_FREE=4831838208 PATH=$MOCK/spacebin:$PATH run 0 stage 2
journal_has host_venv_built
finish
