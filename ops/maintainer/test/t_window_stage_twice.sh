# shellcheck shell=bash
# gene #63: stage is cut off twice. The first time after recording its images, during the import checks; the
# second time after rebuilding the runtime image to a new ID, before the worker build. The records an unfinished
# stage left are provisional: the third window's stage builds, records and checks every image again, and from
# staged on the manifest check is strict again.
. "$(dirname "$0")/common.sh"
ids() { python3 -c 'import json, sys; i = json.load(open(sys.argv[1]))["images"]; print(" ".join(i[t]["id"] for t in sys.argv[2:]))' \
  "$MOCK/docker.json" "gene-runtime:$G" "gene-worker:$G" "fri-runtime:$F"; }
recorded() { python3 -c 'import json, sys; m = json.load(open(sys.argv[1]))["new"]; print(" ".join(m[k]["id"] for k in ("runtime", "worker", "fri")))' \
  "$ST/manifest.json"; }
close_on() {  # pattern: the window closes during the first docker command whose arguments match it
  cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
[[ \$* == $1 ]] || exit 1
echo "\$(/bin/date -d 2026-10-28T07:30:05-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
  chmod +x "$MOCK/on-docker"
}
run 0 baseline 0; upload
at 2026-10-28T07:29:00-07:00
close_on "run*'gene imports ok'*"
run 1 stage 2
output_has 'the window closed before the next stage container'
first=$(ids)
[[ $(recorded) == "$first" ]] || fail 'the first stage did not record its images'
at 2026-10-29T07:29:00-07:00
close_on 'build*'
run 1 stage 2
output_has 'the window closed before the Gene worker build'
journal_lacks staged
[[ $(ids | cut -d' ' -f1) != $(recorded | cut -d' ' -f1) ]] || fail 'the rebuilt runtime image kept its ID; the test needs a new one'
at 2026-10-30T04:05:00-07:00
: > "$MOCK/docker.log"
run 0 stage 2
journal_has staged
[[ $(recorded) == "$(ids)" ]] || fail 'staged records are not the images the tags name'
# Every image was built again and checked after its last build, in the run that staged them.
python3 - "$MOCK/docker.log" <<'PY' || fail 'stage did not check every image after building it'
import sys
lines = open(sys.argv[1]).read().splitlines()
last_build = max(i for i, line in enumerate(lines) if line.startswith('build '))
assert sum(line.startswith('build ') for line in lines) == 3, lines
assert any('gene imports ok' in line for line in lines[last_build:]), lines
assert any('codex --version' in line for line in lines[last_build:]), lines
assert any('fri imports ok' in line for line in lines[last_build:]), lines
PY
# Staged images are strict again: one that changes after staging stops the next phase.
python3 - "$MOCK/docker.json" "gene-worker:$G" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
saved = state['images'][sys.argv[2]]['id']
state['images'][sys.argv[2]]['id'] = 'sha256:' + '0' * 64
json.dump(state, open(sys.argv[1], 'w'))
open(sys.argv[1] + '.saved', 'w').write(saved)
PY
run 1 rehearse 2
output_has "image gene-worker:$G changed since it was recorded"
python3 - "$MOCK/docker.json" "gene-worker:$G" <<'PY'
import json, sys
state = json.load(open(sys.argv[1]))
state['images'][sys.argv[2]]['id'] = open(sys.argv[1] + '.saved').read()
json.dump(state, open(sys.argv[1], 'w'))
PY
run 0 rehearse 0; run 0 drain 0; run 0 gene 0; run 0 fri 0; run 0 evals 0; run 0 verify-off 0; run 0 resume 0
expect new false up
finish
