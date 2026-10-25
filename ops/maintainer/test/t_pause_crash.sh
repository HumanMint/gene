# shellcheck shell=bash
# Killed right after the pause commits, before anything else is journaled: the release still unpauses.
. "$(dirname "$0")/common.sh"
run 0 baseline 0; upload; run 0 stage 0; run 0 rehearse 0
MOCK_KILL_AFTER_PAUSE=1 run 137 drain 1
journal_has intent:pause
run 0 drain 1
run 0 gene 0; run 0 fri 0; run 0 resume 1
expect new false up
finish
