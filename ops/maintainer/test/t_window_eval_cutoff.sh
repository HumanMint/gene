# shellcheck shell=bash
# The evals start at 07:28 and the window closes during the first suite: the next suite doesn't start.
# The phase rolls the release back and resumes the old one itself.
. "$(dirname "$0")/common.sh"
prep; run 0 gene 0; run 0 fri 0
at 2026-10-28T07:28:00-07:00
cat > "$MOCK/on-docker" <<X
#!/usr/bin/env bash
[[ \$1 == exec && \$5 == fri.fri_eval ]] || exit 1
echo "\$(/bin/date -d 2026-10-28T07:30:10-07:00 +%s) \$(/bin/date +%s)" > "$MOCK/clock"
X
chmod +x "$MOCK/on-docker"
run 1 evals 3
[[ -e $MOCK/on-docker.ran ]] || fail 'no suite ran'
output_has 'the window closed before the evals finished'
[[ $(grep -c 'python -m fri.fri_eval' "$MOCK/docker.log") == 1 ]] || fail 'a suite started after the close'
journal_lacks evals_ran; journal_has gene_rolled_back; journal_has resumed_old
expect old false up
finish
