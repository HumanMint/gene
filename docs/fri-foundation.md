# Fri's agent workspace

Discord supplies authenticated events and renders results. The conversational runner
chooses from explicitly offered tools, can take up to four steps in one turn, and
stops on an uncertain result or after an operational Gene action. A tool receipt is
evidence, never permission. Gene, its deployment controller, feature approvals and
the existing-features-v1 repair policy retain their existing authority.

## Saved work

Ask Fri to save a draft or task, list saved work, edit it, or mark it completed or
cancelled. The controller stores the exact body, title, version and state in SQLite,
separately from temporary conversation history. Work survives a restart or chat
clear for 30 days from its last edit, with a maximum of 30 items per member.
This is a record of work, not permission to run arbitrary future jobs.

Work belongs to its author, server and conversation. A private DM draft is available
again in that DM; it is never automatically added to public context. Names and
nicknames do not establish identity. Forgetting a member removes saved work and
pending deliveries too. Text already posted on Discord and backups are separate.
Retrieved member history cannot be copied into saved work by the same tool turn.
In public channels, new text must appear verbatim in the requesting member’s own
message; titles are derived from it. Existing bodies may be kept unchanged when
marking work complete. This also prevents automatically supplied recent messages
from being copied into someone else’s durable memory. DMs support free drafting.

## One-off publication

In the owner's actual DM, ask Fri to send a saved draft to a server channel. Fri can
inspect current channel capabilities and prepare an inline preview showing the
exact text and destination. **Send once** authorizes only that preview; Cancel
does nothing. Preview buttons survive a restart. They expire after 15 minutes;
an authorized delivery must start within five minutes. Editing the draft cancels
unstarted previews and grants. The maximum post length is 1900 UTF-16 units.

Both the owner and Fri must currently be allowed to view and send in the target.
The adapter refreshes members, roles and channel overwrites before approval and
again before sending. Only ordinary server text/announcement channels qualify.
This does not change the ambient conversation allowlist, publish announcements to
followers, ping anyone, send to other users' DMs, or grant administration tools.
Configured history access is separate from ambient conversation participation.

The controller stores the draft version, exact text, target, source event and bound
preview message. A matching fresh owner interaction is the only authorization path.
The heartbeat can deliver an authorized outbox item without any additional model
call. A durable send fence, a bounded single-attempt HTTP transport, and a Discord nonce
prevent repeat attempts. The transport does not follow redirects or retry after
a server error, rate limit or connection reset. A crash after
that fence or an ambiguous response leaves delivery uncertain; it is not retried.
Successful Discord message IDs are retained and receipt delivery to the controller
can retry without repeating the post. Ask to read the saved work for its delivery
status. Re-check Discord before explicitly preparing another uncertain post.

## Verification

Clarification requests are separate from saved work and old conversation history.
The `clarify` action holds an explicit music request or public research question
for five minutes, scoped to the same author, guild and actual channel. A song or
volume answer can complete that request without repeating the command verb. The
controller consumes the permission in the same transaction that queues the action;
Gene rechecks both public source messages and still runs Red's current member,
voice, DJ and cooldown checks. Cancellation, unrelated replies, expiry, changed
sources, chat clear and forgetting invalidate the continuation. A restart does not
lose it or authorize a second command. This grants no admin or repair authority.
This short-lived operational state also works with chat memory disabled. Consumed
request IDs are fenced by command receipts, independently of retained chat turns.
Privacy erasure cancels existing pending requests.

Public research receives only a current public question or the explicit pending
question and current clarification. The assistant question, saved preferences and
ambient member messages are never sent to the research model. The isolated
researcher may rewrite queries and inspect up to five public search/page results.
Fri can request one refinement with up to three further operations when the first
result misses the question. Both fit within the four-action, five-minute turn
budget. A ten-minute receipt permit preserves the validated public query through a long lookup
or a subsequent command, without extending command permission. Source URLs are retained for 24 hours in the same member/conversation
scope, displayed when requested, and erased by the existing privacy controls.

`tests/test_fri_continuity.py` exercises the question/answer path through Fri,
the real controller and installed Red checks using offline Discord/model transports.
It also tests scope, cancellation, edits, privacy, expiry, restart, duplicate
delivery and bounded research recovery. `python -m gene.fri_eval --suite continuity`
checks actual model choices across turns and performs isolated live public research,
with anonymous evaluation usage only. It cannot invoke Discord or write conversations.
These checks do not prove audible playback or guarantee that every public question
has an accessible, current answer.

`tests/test_fri_workspace.py` covers persistence, scope isolation, erase/revision
fences, fresh owner approvals, stale draft versions, restart and ambiguous sends,
revoked permissions, lost controller receipts and the bounded tool chain. Existing
Gene controls and repair tests remain relevant. `python -m gene.fri_eval --suite
foundation` checks actual model choices using synthetic isolated cases: it cannot
save conversations or post to Discord. Delivery tests use a fake Discord transport;
they do not replace a member's real interaction with the new preview.
