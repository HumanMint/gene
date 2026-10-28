# shellcheck shell=bash
# Stage starts at 07:29 and the window closes during the first image build: no further build starts,
# and the next window's stage starts over from what the cut-off one left.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload
at 2026-10-28T07:29:00-07:00
cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
[[ \$1 == build ]] || exit 1
echo "\$(/bin/date -d 2026-10-28T07:30:05-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
chmod +x "$MOCK/on-docker"
run 1 stage 2
output_has 'the window closed before the Gene worker build'
[[ $(grep -c '^build ' "$MOCK/docker.log") == 1 ]] || fail 'a build started after the close'
at 2026-10-29T04:05:00-07:00  # The next window repeats the phase over what the cut-off one left.
run 0 stage 1
run 0 rehearse 0
finish
