# shellcheck shell=bash
# The window closes while stage's first check container runs: the next one, in the same group, doesn't start.
# The next window's stage repeats the phase.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload
at 2026-10-28T04:59:00-07:00
cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
[[ \$1 == run && \$* == *'gene imports ok'* ]] || exit 1
echo "\$(/bin/date -d 2026-10-28T05:00:05-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
chmod +x "$MOCK/on-docker"
run 1 stage 2
[[ -e $MOCK/on-docker.ran ]] || fail 'the import check never ran'
output_has 'the window closed before the next stage container'
! grep -q 'codex --version' "$MOCK/docker.log" || fail 'the worker check started after the close'
at 2026-10-29T04:05:00-07:00
run 0 stage 1
finish
