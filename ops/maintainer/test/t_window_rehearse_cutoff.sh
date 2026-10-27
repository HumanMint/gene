# shellcheck shell=bash
# The window closes during rehearse's first module probe: no copy or rehearsal container follows, the
# member-data copies are gone, and the next window's rehearse runs from the start.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload; run 0 stage 0
at 2026-10-28T04:59:00-07:00
cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
[[ \$1 == run && \$* == *find_spec* ]] || exit 1
echo "\$(/bin/date -d 2026-10-28T05:00:05-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
chmod +x "$MOCK/on-docker"
run 1 rehearse 2
[[ -e $MOCK/on-docker.ran ]] || fail 'no module probe ran'
[[ $(grep -c 'find_spec' "$MOCK/docker.log") == 1 ]] || fail 'a container started after the close'
[[ ! -e $ST/dbtest ]] || fail 'the rehearsal copies were left behind'
journal_lacks rehearsed
at 2026-10-29T04:05:00-07:00
run 0 rehearse 1
journal_has rehearsed
finish
