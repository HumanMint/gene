# shellcheck shell=bash
# gene #116: the host venv is an editable install of the source tree. stage refuses a pyproject.toml change in
# what the venv installs, a uv.lock change and a pyproject.toml it can't parse, each before building anything,
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
sed -i 's/^    "httpx>=0.28,<1",$/&\n    "anyio>=4,<5",/' "$LIVE/pyproject.toml"
refused 'changes what the host venv installs: project.dependencies'
sed -i 's/^requires-python = .*/requires-python = ">=3.11,<3.13"/' "$LIVE/pyproject.toml"
refused 'changes what the host venv installs: project.requires-python'
sed -i 's/hatchling==1.27.0/hatchling==1.26.3/' "$LIVE/pyproject.toml"
refused 'changes what the host venv installs: build-system.requires'
printf '\n[tool.uv]\nindex-url = "https://example.invalid/simple"\n' >> "$LIVE/pyproject.toml"
refused 'changes what the host venv installs: tool.uv'
echo '# changed' >> "$LIVE/uv.lock"
refused 'uv.lock changed'
echo 'broken = [' >> "$LIVE/pyproject.toml"
refused 'could not parse'
# Metadata alone: the live release has a license and another description, which the candidate lacks.
sed -i 's/^description = .*/description = "an older description"\nlicense = "GPL-3.0-only"/' "$LIVE/pyproject.toml"
run 0 stage 2
output_has 'pyproject.toml differs only in metadata: project.description, project.license'
run 0 rehearse 0; run 0 drain 0; run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 0
expect new false up
cmp -s "$LIVE/pyproject.toml" <(git -C "$REPO" show "$G:pyproject.toml") || fail "the candidate's pyproject.toml is not the installed one"
finish
