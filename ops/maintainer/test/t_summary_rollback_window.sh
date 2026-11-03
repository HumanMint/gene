# shellcheck shell=bash
# A rollback to a release that has member summaries but not summary dependencies (the live 34dbbb3), so no downgrade
# runs. In the rollback window the old controller withdraws a member's message their summary drew on: the database's
# triggers retire, then and there, another member's reply depending on that summary with the question it left
# pending, and that member's draft and reminder depending on it (gene #147, #161). Their own words and an ordinary
# reply stay, and the old controller's start leaves the three triggers in place (gene #170 added the third).
export MOCK_OLD_G=34dbbb3799715328772b0ae68ab2fb1d81a68fdb
. "$(dirname "$0")/common.sh"
DB=$BATCH_ROOT/data/controller/gene.sqlite3
SETTINGS="Settings(Path(sys.argv[1]), '123', '9', 'runtime-'+'x'*32, 'worker-'+'x'*32, 'deploy-'+'x'*32,
                    workshop_channel_id='50', fri_token='fri-'+'x'*32, agent_channels='51,52')"
release
expect new false up
[[ $(python3 -c 'import json, sys; print(repr(json.load(open(sys.argv[1]))["downgrades"]))' "$ST/manifest.json") == "''" ]] \
  || fail 'the rehearsal planned a downgrade the old release does not need'
# What the new controller keeps, written with its own code: member 20's summary, member 30's reply depending on it with
# a question pending, their draft and reminder depending on it, and member 40's ordinary reply.
as_owner env PYTHONPATH="$REPO" PYTHONDONTWRITEBYTECODE=1 "$GENE_PY" - "$DB" > "$MOCK/taken" <<PY || fail 'the new controller could not write its data'
import sys
from pathlib import Path
from fastapi.testclient import TestClient
from gene.api import create_app
from gene.config import Settings
from tests.test_fri_endings import source
from tests.test_saved_dependencies import dm, reminder, work, written
from tests.test_summary_dependencies import depending
from tests.test_summary_generation_retention import summarized
from tests.test_turn_withdrawal import MARKER, post_turn, turn
client = TestClient(create_app($SETTINGS))
taken, generation = summarized(client, client.app.state.store)
asked = 'the '+MARKER+' song from their raid, or another one?'
assert post_turn(client, turn(source(age=1, user='30', channel='51'), 'fri play a song', user='30', reply=asked,
                              reply_message_id='800', verbatim=True, summary_dependencies=depending(generation),
                              provenance=[{'user_id': '20', 'revision': 0}],
                              clarification={'goal': 'play', 'topic': '', 'question': asked})).status_code == 200
assert post_turn(client, turn(source(age=2, user='40', channel='51'), 'hi', user='40', reply='hey',
                              reply_message_id='802', verbatim=True)).status_code == 200
written(client, '/workspace/save', work(dm(1), depending(generation)))
written(client, '/social/reminders/prepare', reminder(dm(2), depending(generation)))
print(taken)
PY
run 0 rollback-gene 3
journal_has done:downgrade; purges 0
expect old true down
# The old controller starts on it and withdraws member 20's message, as Fri asks it to after an edit.
left=$(as_owner env PYTHONPATH="$OLD_SRC/src" PYTHONDONTWRITEBYTECODE=1 "$GENE_PY" - "$DB" "$(cat "$MOCK/taken")" <<PY
import sqlite3, sys
from pathlib import Path
from fastapi.testclient import TestClient
import gene.fri_member_summary as summaries
from gene.api import create_app
from gene.config import Settings
assert not hasattr(summaries, 'retire_writes'), 'this is not the old release'
client = TestClient(create_app($SETTINGS))
answer = client.post('/chat/turn/withdraw', headers={'Authorization': 'Bearer fri-'+'x'*32}, json={
    'guild_id': '123', 'channel_id': '51', 'conversation_id': 'public:51', 'user_id': '20', 'message_id': sys.argv[2]})
assert answer.status_code == 200 and answer.json()['removed'] == 1, answer.text
db = sqlite3.connect(sys.argv[1])
one = lambda sql: db.execute(sql).fetchone()
reply, verbatim, said = one("SELECT reply, verbatim, input FROM chat_turns WHERE reply_message_id='800'")
print('retired' if 'older summary' in reply and 'marker' not in reply else 'kept', verbatim, said,
      one("SELECT count(*) FROM chat_pending WHERE user_id='30'")[0], one('SELECT title FROM fri_work')[0],
      *one('SELECT state, length(body) FROM fri_reminders'),
      one('SELECT (SELECT count(*) FROM fri_work_dependencies)+(SELECT count(*) FROM fri_reminder_dependencies)')[0],
      *one("SELECT reply, verbatim FROM chat_turns WHERE reply_message_id='802'"),
      one("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'member_summary_%'")[0], sep='|')
PY
) || fail 'the old controller could not withdraw the message'
[[ $left == 'retired|0|fri play a song|0|Withdrawn draft|cancelled|0|0|hey|1|3' ]] \
  || fail "the old controller's withdrawal left what depended on the summary: $left"
run 0 resume-old 1
expect old false up
finish
