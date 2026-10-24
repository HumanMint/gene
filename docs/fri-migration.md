# Independent Fri runtime

Fri is extracted from Gene `7bdf6af858a5231c6e6af0e10662aa126aa4eb2a`, including the
reviewed detailed-image request fix. The new `HumanMint/fri` package does not import
Gene or Red. Its build contains no controller, worker, deployer, database stores or
feature policy implementation. Its service credential and channel/member boundaries
remain unchanged.

## Ownership

Gene owns Red, cogs, audio, requests, approvals, feature jobs, controller APIs and all
transactional database state in this first milestone. Fri owns its Discord client,
conversation/attention/learning orchestration, images/research, UI, outbox delivery,
subscription refresh, usage spool and presence.

The existing controller uses these Fri-related modules actively, so they are not
obsolete companion code: `fri_agent_store`, `fri_context`, `fri_conversation`,
`fri_explore_store`, `fri_memory`, `fri_memory_store`, `fri_operation_store`,
`fri_pending`, `fri_research`, `fri_responses`, `fri_social_store`,
`fri_workspace_store`, and their route modules.

Old companion modules (`fri.py`, `fri_agent.py`, `fri_chat.py`, UI/tools/evaluators)
remain as a frozen compatibility baseline plus a shared process lock for migration.
They are not the production companion after cutover. Keep the prior immutable image
and configuration for initial rollback. Future runtime fixes go to `HumanMint/fri`;
controller contract fixes still go here. A later reviewed retirement can remove the
legacy snapshot once old/new compatibility evidence is no longer needed.

The additive `event-mention-chat` capability admits an explicit Discord mention in
the configured main guild without adding the channel to any allowlist. The Fri-only
`/chat/mentions/admit` route accepts a gateway-attested `ChatSource`, raw content and
the harness's bot ID. A matching literal mention outside code, quotations and URLs
can reserve the existing member public-turn allowance and obtain an opaque grant.
The grant binds the full event, author, actual channel (including a thread), public
conversation, privacy revision and settings version. It expires within five minutes
of the event and is accepted only while its guild is still the configured main
guild. Admission retries recover the same reservation; a consumed, expired
or privacy-invalidated event cannot obtain a replacement invitation.

Only `/chat/context`, `/chat/turn`, `/chat/research/start`, and
`/chat/mentions/profiles` accept that grant. The profile route takes at most 25
same-channel message sources, at most seven days old for explicit reply references,
and returns only the authors' retention-enabled flags and privacy revisions. The
runtime separately restricts nearby evidence to its short lookback. Referenced
authors' revision changes invalidate the in-flight grant. Context and profile reads
remain available after an idempotent single turn save for the final delivery fence.
All pending, personal-note, workspace, social, operational, control and request
routes keep their existing channel and owner boundaries. Lab scope stays its one
configured testing channel, including when an explicit mention is present.

Outside configured chat scope, context exposes no stored notes, turns, shared facts
or pending actions. Research permits and receipts require a public query from the
current mention itself; earlier configured-scope pending questions and permits
cannot authorize mention research. A saved mention turn contains neutral input/output
placeholders and the source event, without raw request, surrounding context, derived output,
personal notes, burst text or clarification state. The normal bounded public-web
receipts remain retrievable in that member's exact conversation; opt-out suppresses
all turn and receipt retention. Erasure, clear and preference changes invalidate
in-flight writes and reads. The authenticated harness attests Discord events and
current access; the controller does not independently fetch Discord messages.

Pure contracts are copied into Fri at extraction. Changes to shared provenance,
command grammar, request hashes, settings schema or component IDs require integration
checks against both sides. A breaking change must introduce a new supported protocol,
not silently reinterpret `gene-fri/1`. Settings revisions and Gene cog SHAs are not
protocol versions.

## Turn provenance

The additive `turn-provenance` capability makes the controller enforce erasure for saved replies
built from other members' messages.

- **Provenance on save.** `/chat/turn` accepts optional `provenance`: the other members whose
  messages shaped the reply, each with the privacy revision the companion observed. The save fails
  closed if any listed member's revision moved or their memory is off.
- **Erasure.** `personal_control('disable')`, `erase_user` and `clear_chats` delete other
  members' dependent turns. `chat_context` also drops any turn whose sources' revisions changed.
- **Pre-delivery check.** `/chat/turn/check` applies the same speaker-revision and provenance fence
  without writing. The companion calls it immediately before delivering and saves only after
  Discord accepts the reply, so the save is no longer the only fence and an undelivered reply is
  never stored as said. Mention-grant turns keep their own fences and placeholder storage.

Provenance also reaches what follow-ups inherit:

- `chat_context` returns each turn's provenance.
- Pending clarifications store their turn's provenance, are deleted by erasure, and are dropped
  when a source revision moves.
- Agent context returns `summary_provenance`: the summary authors' current revisions.
- Saved turns record Fri's delivered `reply_message_id` and whether the stored reply is
  `verbatim`. `/chat/messages/sources` resolves referenced Fri messages to their current sources
  plus the original speaker, and omits any whose sources changed or that kept only a breadcrumb,
  so the companion fails closed.
- At most 200 provenance entries are accepted.
- Channel summaries record each author's observed revision. A summary is shown only while every
  author still has it, rows without revisions fail closed, `clear_chats` removes the author's
  summaries, and `summary_provenance` carries the observed revisions.
- A consumed clarification copies only member-authored authorization fields into the Gene control
  (goal, source, request text, revision, burst marker), never the assistant's question. Older rows
  are scrubbed on startup.
- The learning overview applies the same summary revision fence, and `clear_chats` removes learning
  reports and their delivery queue.
- Saved turns record the speaker's revision. A Fri message resolves only while that revision is
  unchanged, and rows without it fail closed.
- `/requests` accepts a chat draft's `provenance` and the requester's `revision`, and checks both
  in the creating transaction. Every privacy change (erasure, memory on or off, cleared chats,
  edited notes) withdraws dependent requests the same way erasure withdraws a member's own: unfinished ones are rejected and the stored text is
  replaced. A running build loses its lease, so the worker can't write its summary back; the worker
  then deletes that job's transcript and checkout.
- **Unattributed history.** A turn counts as attributed only when it was saved with a provenance
  list, even an empty one. Turns saved before this release, and any a legacy companion saves later
  (during rollout or after a Fri-only rollback), may quote other members with no record of whom.
  `chat_context` marks them `attributed: false`, and capable companions withhold their replies.
  Pending questions saved without provenance carry no `provenance` key. Command receipts say whether
  the companion `grounded` the argument in the member's own words.
- **Not yet covered:** repair requests Fri files after noticing a Gene error quote Gene's raw error
  text, which can include a member's command. `gene_diagnose` reads them back through
  `previous_repairs`. Chat replies built from that read keep only a breadcrumb, but the stored
  repair text isn't attributed or erased yet. It needs a follow-up.

The legacy companion sends no provenance and is unaffected.

## Member requests in context

`chat_context` also returns `requests`: up to five of the speaker's own requests that are still open
or settled within the last week, with id, kind, state and timestamps. Only the requester and the
owner see request text in the panel, so a `title` is included only in the member's DM and never
for a draft that drew on other members' words. With memory off the list is empty. Older companions
ignore the field.

## Release and recovery

The trusted coordinator is `python -m gene.fri_release <full-reviewed-fri-sha>`.
It uses `ops/fri-compose.yml`, `/srv/fri/install.json`, `/srv/fri/release.env` and a
separate `fri` project. Runtime mounts are only the existing Fri data/auth directories.
Its image label must match the reviewed SHA. The companion has no release API or
host/Docker/GitHub credentials. Existing Gene job approvals cannot target Fri.

Initial migration stops `gene-fri-1`, disables its restart policy and starts the new
singleton. Both companion implementations acquire the same persistent data-directory
lock. Later releases restart only Fri. Gene/Red/controller/audio and active workers
are left alone by this coordinator. Gene's normal cog deploy/rollback never touches
Fri's pins or service.

Backups capture current controller/data state for disaster recovery. Automatic Fri
rollback changes only code/configuration and uses current state. It never restores a
stale database, erasure revision, approval, sent receipt or rotating auth session.
Failed first cutover restores the legacy process; failed subsequent release restores
the previous compatible independent image. A failed recovery needs maintainer attention.

Rolling the controller back below `turn-provenance` needs one extra step, because an older
controller cannot erase state derived from other members. First roll Fri back to a release that
does not require the capability. Then, with the controller stopped and backed up, run this from the
current release, in the controller environment, before starting the older controller:

```sh
python -m gene.provenance_downgrade "$GENE_DB"
```

It deletes saved turns and pending questions that carry provenance, withdraws requests submitted
from derived drafts, and drops `agent_summaries.revisions`, which the older controller's positional
summary insert can't handle. This loses only continuity and those derived requests. The speaker's
own turns stay. Rolling back Fri alone needs no cleanup: the newer controller keeps enforcing
provenance on everything already saved.

The complete `/srv/gene/data/fri` and `/srv/gene/secrets/fri-chatgpt` directories stay
in place initially. Identity, read/write allowlists, memory, pending controls/reviews,
reminders, reports and persistent buttons remain unchanged. See Fri's operations
README for the exact installation JSON and new-installation alternative.

Physical database separation remains a future project. In particular, command
admission currently checks privacy revisions, source deduplication and pending music
clarifications in one transaction. Splitting those tables now would weaken that
boundary without a new cross-service protocol.
