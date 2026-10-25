# shellcheck shell=bash
# A player appears between the cog move and the Red stop: rollback before any restart.
. "$(dirname "$0")/common.sh"
prep
rm -f "$MOCK/player-calls"
MOCK_PLAYERS_SEQ=0,1 MOCK_PLAYING=1 run 1 gene 3
journal_has gene_attempted; journal_has gene_rolled_back; journal_lacks intent:red_stop
purges 0
expect old true down
finish
