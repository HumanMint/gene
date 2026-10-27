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
- **Attention's author check.** The optional `author-fence` capability adds `/agent/authors/check`
  (Fri credential): `guild_id`, `channel_id` and up to 200 `authors`, each a `user_id` and the
  `revision` Fri read their words under. It answers `{"ok": true}` only if every author is still at
  that revision with memory on, and otherwise refuses with a 409 that names nobody. It reads them all
  under the controller's write lock, so no erasure through Gene, Red or Fri lands between two authors'
  reads, which one `/agent/context` call per author couldn't promise (fri #84). It takes the same
  channels as `/agent/context`. An older controller lacks it and Fri keeps its per-author reads.

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
or settled within the last week, open ones first, with id, kind, state and timestamps. Only the
requester and the owner see request text in the panel, so a `title` is included only in the member's
private conversation (DM or `/fri` panel) and only for a request known to hold just their own words:
submitted with an empty provenance list. Requests saved before this release or by an older companion
are unattributed and never show a title. A request whose text was withdrawn after a privacy change
carries `withdrawn: true` and no title, so its `rejected` state isn't mistaken for the owner's
decision. Withdrawal refreshes `updated`, so it stays listed for a week. With memory off the list is
empty. Older companions ignore the field.

## Open loops in context

`chat_context` also returns `loops`, the speaker's own unfinished things that belong to that
conversation, so the companion knows them without a tool call. Each is scoped exactly like the tool or
card that already shows it there:

- `reminders`: in a private conversation, the speaker's reminders whose `dm_id` matches it (as
  `reminder_list`), upcoming first, plus any due in the last day with their delivery state, up to five.
  A pending, scheduled or claimed reminder whose privacy revision no longer matches is
  `will_not_send`, since confirming and delivering both check it. A `/fri` panel conversation is
  private too and gets an empty list; the companion nulls it outside DMs.
- `saved_work`: open items saved in this exact conversation (as `work_list`), title and `updated`.
- `game_nights`: in a public conversation, cards the speaker organized in that channel or thread that
  haven't happened (or started under three hours ago), with the card's own availability and RSVP
  counts. Never who voted or what they chose.

A kind that can't show in the conversation is `null`. With memory off, every list is empty. It runs
the same prune steps as the list endpoints, so states match. No schema change, and older companions
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

Rolling the controller back below `member-profiles` also needs one step, because an older
controller can neither show nor edit profile notes. With the controller stopped and backed up,
run this from the current release, in the controller's environment (it reads `FRI_CHANNEL_IDS`
and `FRI_HISTORY_CHANNEL_IDS`; `--channels 1,2,3` adds more), before starting the older controller:

```sh
python -m gene.profile_downgrade "$GENE_DB"
```

It deletes profile notes and marks every saved exchange as staying in the conversation it was said
in. An older controller checks a removal only in the channel it learns into, so each profile
removal is first copied to every configured channel and every public conversation the database
knows: an older controller then saves no note, in any category, from a member's messages before
their removal. If profile removals exist and no channels are given, or a channel isn't a numeric
Discord id, it changes nothing and exits with an error. It works on the database as it is and never
applies this release's migrations: a database that never had member profiles is left as it was, and
when rolling back below both `member-profiles` and `turn-provenance` the two commands can run in
either order without one undoing the other. An older controller's channel summaries and archive observations don't check
removals, and it doesn't remove a note when a member it quoted erases, as before member profiles. Conversation notes and exchanges are kept. Rolling back Fri
alone needs no cleanup: an older Fri saves nothing to profiles and doesn't ask for them, so the
controller shows it only each conversation's own notes, leaving out any that quote another member.
Notes saved before this release keep no provenance (it's unknown, not empty), so they stay in the
conversation where they were said, as before, and an erasure doesn't reach one that quoted someone.
Profile notes stay stored, unseen, until a
newer Fri returns or they expire; "forget me" still erases them.

Rolling the controller back below `member-summaries` needs one step too, because an older controller
can neither show, edit nor erase Fri's member summaries. With the controller stopped and backed up,
run this from the current release before starting the older controller (`batch-release.sh` does it
as a recorded downgrade):

```sh
python -m gene.fri_member_summary purge "$GENE_DB"
```

It drops the summaries and their daily call stamps; exchanges and notes stay. Rolling back Fri alone
needs no cleanup: an older Fri doesn't send `summaries`, so the controller never gives it one and no
builder runs. Summaries already stored stay unseen until a newer Fri returns, they expire (30 days),
or the member's "forget me", memory off or Forget my chats removes them; run the purge as well if they
should go at once.

The complete `/srv/gene/data/fri` and `/srv/gene/secrets/fri-chatgpt` directories stay
in place initially. Identity, read/write allowlists, memory, pending controls/reviews,
reminders, reports and persistent buttons remain unchanged. See Fri's operations
README for the exact installation JSON and new-installation alternative.

Physical database separation remains a future project. In particular, command
admission currently checks privacy revisions, source deduplication and pending music
clarifications in one transaction. Splitting those tables now would weaken that
boundary without a new cross-service protocol.
