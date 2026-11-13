# shellcheck shell=bash
# gene #116: the host venv is an editable install of the source tree. stage refuses a pyproject.toml change in
# its build or Python settings, and a pyproject.toml it can't parse, each before building anything,
# and names what changed. A difference in metadata alone (license, description) goes through to a full release.
. "$(dirname "$0")/common.sh"
LIVE=$BATCH_ROOT/source
cp "$LIVE/pyproject.toml" "$MOCK/pyproject.toml.live"; cp "$LIVE/uv.lock" "$MOCK/uv.lock.live"
refused() {  # text: stage stops before building anything and says why; then the live manifests go back
  run 1 stage 2
  output_has "$1"
  journal_lacks staged
  ! grep -q '^build ' "$MOCK/docker.log" || fail 'stage built an image'
  cp "$MOCK/pyproject.toml.live" "$LIVE/pyproject.toml"; cp "$MOCK/uv.lock.live" "$LIVE/uv.lock"
}
run 0 baseline 0; upload
# The live release's manifests differ from the candidate's, as each change would leave them.
sed -i 's/^requires-python = .*/requires-python = ">=3.11,<3.13"/' "$LIVE/pyproject.toml"
refused 'unsupported host venv change: project.requires-python'
sed -i 's/hatchling==1.27.0/hatchling==1.26.3/' "$LIVE/pyproject.toml"
refused 'unsupported host venv change: build-system.requires'
printf '\n[tool.uv]\nindex-url = "https://example.invalid/simple"\n' >> "$LIVE/pyproject.toml"
refused 'unsupported host venv change: tool.uv'
echo 'broken = [' >> "$LIVE/pyproject.toml"
refused 'could not parse'
# Metadata alone: the live release has a license and another description, which the candidate lacks.
sed -i 's/^description = .*/description = "an older description"\nlicense = "GPL-3.0-only"/' "$LIVE/pyproject.toml"
old_python=$(sha256sum "$BATCH_ROOT/venv/bin/python")
run 0 stage 2
[[ ! -f $MOCK/uv.log && $(sha256sum "$BATCH_ROOT/venv/bin/python") == "$old_python" ]] || fail "unchanged install rebuilt venv"
output_has 'pyproject.toml differs only in metadata: project.description, project.license'
run 0 rehearse 0; run 0 drain 0; run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 0
expect new false up
cmp -s "$LIVE/pyproject.toml" <(git -C "$REPO" show "$G:pyproject.toml") || fail "the candidate's pyproject.toml is not the installed one"
finish
