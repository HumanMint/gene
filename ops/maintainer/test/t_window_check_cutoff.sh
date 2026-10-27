# shellcheck shell=bash
# Stage's last build ends after 05:00: the check containers that follow it don't start.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload
at 2026-10-28T04:59:00-07:00
cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
[[ \$1 == build && \$* == *fri-runtime:* ]] || exit 1
echo "\$(/bin/date -d 2026-10-28T05:00:05-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
chmod +x "$MOCK/on-docker"
run 1 stage 2
output_has 'the window closed before the isolated import checks'
finish
