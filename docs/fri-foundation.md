# Fri's agent workspace

Discord supplies authenticated events and renders results. The conversational runner
chooses from explicitly offered tools, can take up to four steps in one turn, and
stops on an uncertain result or after an operational Gene action. A tool receipt is
evidence, never permission. Gene, its deployment controller, feature approvals and
the existing-features-v1 repair policy retain their existing authority.

## Reply context and short message bursts

Fri can use the actual message a member replied to, including another member's message
or Fri's reply to someone else. The adapter fetches the current message from the same
channel, checks the requesting member's access, and supplies its author ID and exact
bounded text as untrusted context. It does not import anyone else's personal notes or
private conversation. Targets older than seven days, deleted messages, webhooks, other
bots, unreadable channels, and opted-out members' messages are omitted. Source edits,
access changes and privacy revisions are checked again before a tool or final reply.
Reply targets are temporary context. Saved turns keep a neutral breadcrumb in place
of an answer derived from a target, so neither the target nor quoted findings are
retained in someone else's memory. Reply to the original message again when its
details are needed for a later follow-up.

The same retention fence applies before tools are offered and remains active for
the turn. Read reply targets cannot become personal notes, request drafts, saved
work, posts, voice settings or model-written clarification state. Saving needs a
fresh message containing the member's own input. An explicit current play/volume
request can still receive a fixed, target-free song/volume question, backed only by
that member's fresh Discord source. Music arguments must likewise come from their
own current request or a verified continuation, so command receipts cannot retain
someone else's referenced text.

Messages from the same person in the same channel can form one short exchange: up to
five messages and 4000 total characters, spanning at most five seconds. The existing
1.5-second pause still produces one response to the newest message. For example,
“fri help me pick a movie” followed by “something funny” keeps both inputs, including
in a member DM or with memory off. Each part retains its own real Discord source;
the adapter never invents a combined source event. The in-flight buffer is removed
after the exchange or privacy erasure. With memory on, the source-linked burst has
the same six-turn/seven-day retention and member/conversation scope as ordinary chat.

An immediately preceding incomplete “fri play” or “fri volume” request and its exact
answer can use the normal music handoff without another clarification question.
“fri play” followed by “blind melon no rain” is supported. Both sources must be fresh,
from the same author and actual channel, and within five seconds of each other.
Complete older commands, examples, cancellations, another person's words, and different
arguments do not qualify. Fri re-fetches both inputs; Gene also re-fetches both public
inputs and runs current Red member, voice, DJ and cooldown checks. The controller
consumes the origin atomically and preserves the existing replay fence across restarts
and erasure. Other tool permissions and public-research export rules still use their
existing current-message checks; ordinary burst context does not grant new authority.

## Editing personal preferences

Personal memory keeps up to ten exact, harmless first-person disclosures per member
and conversation, with at most three in each category. Separate game or music
preferences can coexist. Exact duplicates renew their timestamp; each item expires
after 90 days. Old single-note profiles are migrated without changing their existing
IDs, evidence or dates. Historical learning cannot evict newer preferences to make
room for an older disclosure.

Open **My memory** in the private Fri panel, or ask what Fri remembers and choose
**Edit my memory**. Select an entry to edit or remove it. Controls bind the current
member, channel and memory revision, and remain usable without a model call. An edit
requires a fresh explicit first-person preference, such as “I prefer Terraria”. It
keeps the selected category and changes only that conversation's item.

Editing or removing an entry clears recent saved turns and pending clarifications
in that conversation, invalidates in-flight derivations, and clears cached context
and derived summaries that could repeat it. A scoped category hash and removal date
prevent background learning from adding older disclosures in that category, including
shorter excerpts of the removed note. Existing other items remain; a later explicit
disclosure can renew a preference. Hashes expire with the 90-day learning horizon and
are erased by **forget me**.
Other saved preference items remain. Existing Discord posts and backups are separate.

`tests/test_fri_conversation_context.py` exercises real offline Red command handling,
DM/public burst continuity, memory-off behavior, source edits/deletion, identity,
access, expiry and replay. `tests/test_fri_memory_items.py` covers migration, item
limits, persistence, individual controls, revision races, erasure, expiry and old
learning. These use fictional Discord/model transports and prove neither live
delivery nor audible playback.

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

### Conversation endings

In ordinary message conversations, Fri can reply, choose one acknowledgment reaction,
or stay quiet. The existing Sol conversation call makes nuanced choices; no extra
Luna classifier runs for each follow-up. A short whitelist of complete closing phrases
can skip inference only after the previous verified, same-member reply marked the
exchange complete, within two minutes, without a pending question, an intervening
unanswered message, or signs of serious distress. Yes/ok, song titles, compliments
and banter still reach the normal conversation path. Pending tool clarifications,
tool outcomes and private panel interactions require text or a tool action.

Reactions are bounded Unicode choices on the triggering message only, subject to
the existing conversation write scope and Discord permissions. The adapter rechecks
the message for edits/deletion and existing Fri reactions. It permits at most one
acknowledgment per member/channel in 90 seconds and one per channel in 15 seconds.
Failures stay quiet. Neither reactions nor silence renew the follow-up window or
authorize anything. Decisions are stored as typed turns, never fabricated speech;
reaction delivery is not claimed by the history record. Normal retention, scope
isolation and erasure apply. Meme attention has its own unchanged controls.

`tests/test_fri_endings.py` covers the shortcut, pending-request fences, typed
history, schema validation and Discord delivery checks. `python -m gene.fri_eval
--suite endings` checks actual model choices on isolated fictional conversations,
including a playful follow-up, closing acknowledgments, clarifications and distress.
It writes only anonymous usage receipts, never Discord messages or member history.

### Continuing requests

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
request IDs are fenced in both clarification and direct command paths. A hash of
the original event ID and guild survives text erasure for 24 hours to prevent replay;
it retains no message text, names or user ID. Privacy erasure cancels existing pending requests.
Explicit cancellation clears an armed clarification before inference, admission or
busy checks, and retires its origin against late/replayed replies. It does not undo
a command Gene already accepted; an active player still needs the normal stop command.

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
