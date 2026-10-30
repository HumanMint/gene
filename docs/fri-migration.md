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
- **Withdrawn messages.** The optional `turn-withdrawal` capability adds `/chat/turn/withdraw`
  (Fri credential, the scope of `/chat/turn` without a mention grant): `guild_id`, `channel_id`,
  `conversation_id`, `user_id` and a Discord `message_id`. It deletes that member's saved turns in
  that conversation whose source, or a message of whose burst, is that message, with any pending
  clarification or research permit the message or those turns left (a burst's name its final
  message, gene #84). The member's summary, and a build of it in flight, go too if they could have
  read the message: it was said in a conversation the summary or build drew on, before the newest
  exchange it read (with five minutes' allowance for clock skew), even if its turn has been trimmed
  since (gene #83). It answers `{"ok": true, "removed": n}`, so a repeat is harmless, and moves no
  revision. Fri calls it when a member's message it saved a turn for is edited or deleted while
  withdrawn, so the old words stop coming back as history (fri #129). Other members' summaries that
  drew on the message are untouched, as is a summary the member wrote in My memory (but see gene #141
  below for notes and other members' replies).
  An older controller lacks it and Fri doesn't ask. The same transaction records a fence: the guild,
  member and message ID, never its words, kept a week (as long as a saved exchange). A save that lands
  afterwards for a turn whose source or burst holds that message keeps nothing and answers
  `{"ok": true, "first_memory": false, "kept": false}`, as a turn sent before a note removal does, so a
  save Fri timed out on can't bring the words back (gene #90). `/chat/turn/check` refuses such a turn
  with a 409 that holds no text, and `/chat/research/start` refuses its research. Memory off and
  "forget me" drop the member's fences; their revision moves, so a late save fails on that instead.
- **Withdrawn messages Fri can't place.** Fri knows only the turns it saved since it started, so a
  member's edit or delete of a message saved before a restart would leave that exchange's old words
  until it expires (fri #174). The optional `saved-turn-withdrawal` capability adds
  `/chat/turn/withdraw/saved` (Fri credential): `guild_id`, the configured server or the test server,
  and up to 10 Discord `message_ids`. Each saved turn in that server whose source, or a message of
  whose burst, is one of them goes exactly as `/chat/turn/withdraw` takes it, under the member and
  conversation it was saved with: its fence, its pending clarification and research permit, and the
  member's summary if it could have read the message. A message no saved turn holds changes nothing:
  no fence is written and no summary goes, so the many edits and deletes Fri can't place cost nobody
  their summary. That lookup reads only; one that finds a turn takes the write lock and reads again.
  It answers `{"ok": true, "removed": n}`. An older controller lacks it and Fri asks only about the
  messages it saved since it started, as before.
- **What else a withdrawn message reached (gene #141).** Both withdrawals above also take:
  - **Notes learned from it.** Each personal note records the Discord message it was learned from
    (`personal_notes.sources`, a JSON list: the message whose words hold the note's, so in a burst only
    the one that says it, or a learning batch's message; `[]` for words typed in My memory; NULL, unknown,
    for a note saved before this or by an older controller), stamped with the note's time then
    (`personal_notes.sources_at`). An older controller that learns the same note again moves its time and
    leaves both, so a note whose time moved since counts as one of unknown source, never judged by those
    stale messages. A note naming the withdrawn message goes
    wherever it shows, profile included, whether the message was edited or deleted. One of unknown or
    stale source stays, as before: nothing shows which message supplied it (an older controller may have
    learned it again from another), so it isn't traced to this one, and it expires as before (gene #149
    tracks a sound way to trace it). Share and Keep carry the sources; the same note learned again comes from the newer
    message, so withdrawing the older one leaves it. By ID, a note whose exchange was trimmed goes too
    (a note outlasts six exchanges), with a fence for the member and message, so a learning batch already
    reading the message keeps nothing from it, as a late save already did (gene #90).
  - **Replies that carried it, once deleted.** With the optional `withdrawal-sources` capability,
    `/chat/turn` and `/chat/turn/check` take `drew_on`: up to 1000 Discord message IDs the turn's model
    requests carried besides its own (the channel's view, what it answers or quotes, what a read showed,
    saved history). A save keeps them (`chat_turns.drew_on`, NULL for none). Both withdrawals take
    `deleted` (default false: an edit). Once a message is deleted, any member's saved reply that carried
    it, or carried Fri's reply to it, or a reply of Fri's that did, keeps only a breadcrumb, as one drawn on
    a withdrawn summary does: not verbatim, so a reference fails closed, and any question it left goes.
    The member's own words stay. An edit leaves those replies, as before (gene #150). A deletion of a
    message whose author is known (one their exchange or note held) is kept a week by message ID, author
    and the author's privacy revision then (`chat_turn_deletions`: never words; apart from the fences,
    which an older controller writes positionally). Their "forget me" takes it with their fences, and a
    start drops one whose author's revision has moved since (an older controller's erasure leaves this
    table alone). A check whose `drew_on` holds one refuses with the 409 above, and such a save keeps
    nothing. By ID, a deletion that only such a reply holds retires that reply there and then and keeps
    no record and no fence: its author is unknown there.
  - **Nothing about a member after a privacy change.** `/chat/turn/withdraw` also takes `revision`, the
    member's privacy revision when the exchange was saved. One that arrives after the member's revision
    moved ("forget me", say), or with their memory off, still takes anything that holds the message but
    writes no fence or deletion, since a late save under that revision is refused anyway. An older Fri
    sends no revision; memory off still writes nothing.
  - **Where it came from, when asked.** `/chat/context` with `sources: true` (sent only by a Fri that
    knows the capability) adds `note_sources`, a list beside `personal_memory.notes` in the same order
    (each note's message IDs, or null), and a `messages` list on each `elsewhere` row. So does
    `/chat/memory/notes`, for My memory: a `sources` list on each entry. Without it the answer is as
    before. Fri uses them to keep that context out of a request, and out of memory views, while a
    withdrawal is still on its way (fri #215).
  - **Start.** An older controller withdraws exchanges and fences their messages but keeps their notes:
    the next start here drops notes naming a fenced message, one query per fence over that member's
    notes. It doesn't record which messages were deleted, so replies that carried them stay, as on main.
  - The mention fingerprint leaves `drew_on` out. An older Fri sends none and asks nothing, so it gets
    what it did before, plus notes going with withdrawn messages.
- **Summary generations.** The optional `summary-generations` capability closes the gap for a reply
  that already drew on the member summary when a withdrawal dropped it (gene #91). With `summaries`,
  `/chat/context` returns `member_summary.generation`, an integer that moves each time a withdrawal
  drops that member's summary (a repeat drops nothing and moves nothing). A Fri that gives its model the
  summary sends that same integer back as a top-level `summary_generation` on every `/chat/turn/check`
  and `/chat/turn` for that turn, and on any other check whose text drew on it. Both refuse with a 409
  (no text) once the generation has moved, so Fri withholds the reply as for other withdrawn evidence.
  Fri omits it when no summary reached the model; a turn without it is checked as before, so an older
  Fri keeps working, with the gap. An older controller returns no generation, so Fri has nothing to
  send (and an extra field would be ignored). Privacy changes delete the member's generation with
  their summary; their revision moves at the same time, so an older turn still fails. The purge below
  drops the generations too. A summary that expired since the turn read it, with nothing pruning it
  yet, goes in the check's or save's own transaction before the integer is compared, so its generation
  has moved and the turn is refused (gene #113).
- **What a summary-derived turn keeps.** A saved turn keeps the `summary_generation` it was sent (fri
  #172). Whenever the member's generation moves (a withdrawal, an expiry, another member's privacy
  change, or a rebuild that wasn't sent the earlier summary or came out empty), and when their own
  privacy change erases the summary, every saved reply of theirs that drew on a summary is replaced in
  the same transaction by a breadcrumb ("Earlier answer drawing
  on an older summary of them. Nothing from it is retained here.") and marked not verbatim, and a
  question it left pending goes. History, a reference to Fri's message and reply checks then carry
  none of its words; the member's own words stay. A turn saved without a generation (an older Fri, or no summary in its request) is untouched.
  `/chat/messages/sources` checks the summary first as well: one that lapsed with nothing pruning it
  yet goes in the lookup's own transaction, so a reply that drew on it is reported as not verbatim
  (gene #120). Only a lookup that finds such a reply takes the write lock.
  A controller start retires any an older controller left behind during a rollback window: with a
  moved generation, with none on record (its privacy changes delete the generation, so a save records
  generation zero too), or saved under a revision of the member's that has moved since (gene #114). The
  purge below retires all of them first. A non-empty rebuild that was sent the
  earlier summary keeps its origins, provenance and a later `through`, so a withdrawal of anything the
  old one drew on still finds it: those replies move to the new generation instead of being retired,
  and the routine daily rebuild doesn't erase Fri's side of a DM.
- **Summary dependencies.** A reply can repeat another reply of Fri's that drew on someone else's
  summary: an echo of it in the channel's view, or a reply of Fri's the member answered. The optional
  `summary-dependencies` capability lets that reply depend on the summary too (fri #193).
  `/chat/messages/sources` adds `summaries` to a reply it reports when that reply drew on one, its
  speaker's own or one it depended on: each a `user_id` and the `generation` it was saved with. Ordinary
  replies' answers are unchanged. A Fri that carried such a reply into a request sends those as
  `summary_dependencies` (up to 200, each `user_id` and `generation`) on `/chat/turn/check` and
  `/chat/turn`. Each is checked as the turn's own `summary_generation` is: a 409 (no text, naming nobody)
  once it has moved or the summary has lapsed. A save keeps them in `chat_turns.summary_dependencies`
  (JSON, or NULL for none) and records each generation as above. Whenever one of those summaries' generation moves (a drop, an expiry, a
  rebuild that wasn't sent the earlier summary) and when that member's privacy change erases it, the reply is retired to the
  same breadcrumb in the same transaction, even where its provenance didn't name them. A rebuild that was
  sent the earlier summary moves the dependency to the new generation instead. A sources lookup
  checks each summary for a lapse first, under the write lock. A controller start retires any whose
  generation moved or went, or whose speaker's revision moved, while an older controller ran, and the purge below
  retires all of them. Chat context never shows the dependencies. An older Fri sends none, which is checked as before; an older
  controller ignores the field, so Fri sends it only when offered. Mention turns leave both fields out
  of their fingerprint.
- **Summary-derived writes.** The optional `summary-generation-writes` capability extends the check to
  a tool's writes (fri #171). `/workspace/save` and `/social/reminders/prepare` take the same optional
  top-level `summary_generation` and refuse with a 409 (no text) once it has moved or the summary has
  lapsed, in the write's own transaction, so saved work or a reminder can't keep the summary's words after their source went. It
  stays out of their idempotency digests, so a retry with or without it answers as the first did. A
  controller without the capability rejects the unknown field, so Fri sends it only when offered.
  Work and reminders saved while the summary was current stay: the member asked for them.
- **Saved dependencies.** The optional `saved-dependencies` capability carries summary dependencies
  through the member's own saved history and through a tool's writes (fri #216).
  - `/chat/context` with `dependencies: true` adds `summaries` to each saved exchange whose reply drew on
    a summary (its speaker's own, or one it depended on), as the sources lookup reports them, so a turn
    reading that exchange as history depends on them in turn. It first checks each of those summaries
    for a lapse, as the sources lookup does. Without the flag the answer is unchanged.
  - `/workspace/save` and `/social/reminders/prepare` take `summary_dependencies` (the turn's, up to 200),
    checked inside the write's transaction as `/chat/turn` checks them, and left out of the idempotency
    digests. A reminder, or a draft in a DM (in a channel a draft is the member's own words), keeps
    those of other members in the new tables `fri_work_dependencies` and `fri_reminder_dependencies`,
    whose rows go with their item. When one of those summaries goes as a saved reply's would retire it,
    the draft's title and words become a breadcrumb (its version moves, and posts not yet sending are
    cancelled) and the reminder loses its text, cancelled unless it is already going out. A covered
    rebuild moves them on instead, and the purge below retires them all. Each row also keeps that
    member's privacy revision when it was written. A controller start retires the item and drops the row
    when the generation moved or went, or when that member's revision moved since. The writer's own
    summary is still checked but not kept, as above.
  - Rollback: the tables are new, so an older controller's positional inserts into `fri_work` still fit,
    and its deletes of work and reminders cascade to them. It ignores them otherwise: its erasure of the
    member they depend on leaves the rows and the words until the next start of this code, which retires
    them by the moved revision, even once a summary built again has brought the same generation back (as
    `chat_turn_deletions` are dropped at start).

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

It drops the summaries, their daily call stamps and their generations, and leaves each saved reply that drew on a
summary only its breadcrumb; exchanges and notes stay. Rolling back Fri alone
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
