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
of the event. Admission retries recover the same reservation; a consumed, expired
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
cannot authorize mention research. A saved mention turn contains neutral input/output placeholders
and the source event, without raw request, surrounding context, derived output,
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

The complete `/srv/gene/data/fri` and `/srv/gene/secrets/fri-chatgpt` directories stay
in place initially. Identity, read/write allowlists, memory, pending controls/reviews,
reminders, reports and persistent buttons remain unchanged. See Fri's operations
README for the exact installation JSON and new-installation alternative.

Physical database separation remains a future project. In particular, command
admission currently checks privacy revisions, source deduplication and pending music
clarifications in one transaction. Splitting those tables now would weaken that
boundary without a new cross-service protocol.
