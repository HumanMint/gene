. "$(dirname "$0")/common.sh"
prep
rm -f "$MOCK/player-calls"
MOCK_PLAYERS_SEQ=0,1 MOCK_PLAYING=1 run gene 8
summary
