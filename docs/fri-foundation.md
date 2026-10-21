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

`tests/test_fri_workspace.py` covers persistence, scope isolation, erase/revision
fences, fresh owner approvals, stale draft versions, restart and ambiguous sends,
revoked permissions, lost controller receipts and the bounded tool chain. Existing
Gene controls and repair tests remain relevant. `python -m gene.fri_eval --suite
foundation` checks actual model choices using synthetic isolated cases: it cannot
save conversations or post to Discord. Delivery tests use a fake Discord transport;
they do not replace a member's real interaction with the new preview.
