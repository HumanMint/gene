# Operations

## Hosts and credentials

The trusted checkout and service files live under `/srv/gene` on Hostinger. Docker
containers run as UID 10001. The small trusted worker/deployer wrappers run as root
to orchestrate isolated containers, backups, and release mounts. They are part of
the control plane, never editable by automated feature jobs. Existing Hostinger
applications and OpenClaw are separate and are not managed by this project.

- `source/`: installed control plane; update deliberately as a maintainer.
- `secrets/{controller,runtime,fri,worker,deployer}.env`: separate service credentials.
- `secrets/github_ed25519`: repository-specific deploy key for the trusted wrappers.
- `codex/`: dedicated subscription login owned by `gene-coder`; one worker at a time.
- `data/controller/`: SQLite requests, approved context, audit, releases, usage.
- `data/red/`: Red settings, playlists, highlight IDs, runtime heartbeat.
- `releases/<full-sha>/cogs/`: immutable feature releases.
- `work/`: private coding worktrees; successful raw event streams are deleted.
- `backups/<job-id>/`: controller snapshot and stopped Red state before a release.

Use the GitHub CLI for repository management. `gh auth status` verifies its OAuth
connection; `ssh -T git@github.com` verifies Git transport independently. Credentials
are never included in repository files, tool output, or Discord messages.

## Initial installation

1. Run `scripts/configure.py` locally with the existing **bot** token. Enable Message
   Content Intent. The pinned runtime disables Members and Presence intents; test
   any future cog that needs those separately. Do not use a user-account token.
2. Select Discord Lab, workshop and meme test channels. Preserve the friend-server
   configuration separately. Never start two runtime processes with the same token.
3. Transfer source to `/srv/gene/source` and private configuration to
   `/srv/gene/upload.env` over SSH. Run `sudo python3 /srv/gene/source/scripts/install_host.py`.
   It splits credentials and deletes the uploaded combined file; it does not start services.
4. Add the generated public deploy key to this private repo with `gh repo deploy-key
   add <public-key-file> --allow-write`. Install known GitHub host keys from an already
   trusted SSH configuration. Never disable host key checking.
5. Build `gene-runtime:bootstrap` and `gene-worker:bootstrap` from `ops/Dockerfile`.
   Create the host Python 3.11 environment from `uv.lock` at `/srv/gene/venv`.
6. Archive the initial tested maintainer commit's cogs into `releases/<sha>/cogs`, write
   `GENE_RELEASE_SHA=<sha>` into `/srv/gene/release.env`, and bootstrap `Store` with
   that SHA. Start the controller and Red with the compose command below.
7. Sign in with `sudo -u gene-coder env CODEX_HOME=/srv/gene/codex
   /srv/gene/tools/node_modules/.bin/codex login --device-auth`. Never copy or display
   another machine's auth cache. Keep the refreshed cache between jobs.
8. Enable the `gene-worker` and `gene-deployer` systemd units after checks pass.
   Development starts paused. `/fri → Owner settings → Resume development` resumes new jobs.

Compose command prefix on Hostinger:

```bash
sudo docker compose --project-name gene --env-file /srv/gene/release.env \
  -f /srv/gene/source/ops/compose.yml
```

Add `ps`, `up -d`, `logs --tail 60 red`, or `stop red` as needed. Logs can contain
request details or source-service errors; inspect privately and never paste secrets.
The controller listens only on host loopback port 8787 and its private Docker network.

## Audio on Hostinger; downloads through the Mac

Hostinger runs the pinned Red-compatible Lavalink `3.7.13+red.5` and the separate Gene
source plugin. Red remains an upstream package; its commands, player, queue, and
metadata cache are unchanged. Red's `audioset cache` manages metadata, not downloaded
music files ([upstream documentation](https://docs.discord.red/en/stable/cog_guides/audio.html#owner-only-audioset-commands)).
The downloaded-file cache below therefore belongs to the external audio service.

A direct Hostinger test with `yt-dlp[default]==2026.8.19` and Node 24 tried complete
public audio downloads for Rainbow Road and Far Away. Both required YouTube sign-in /
bot verification. The configured source uses the home connection; it does not repeat
these failed VPS attempts for every request or use personal cookies.

The private path is:

1. Red on Hostinger asks its private Lavalink node to play a YouTube ID.
2. Hostinger checks `/srv/gene/data/audio/media` for a complete cached file.
3. On a miss, the Mac downloads audio without transcoding, encoding, or voice playback.
4. The Mac transfers the complete file over Tailscale. Hostinger verifies the byte count
   and SHA-256, then atomically publishes the file. Partial or mismatched transfers cannot play.
5. The Mac deletes its temporary transfer directory after a completed or interrupted send.
   Failed downloads are also removed; process-crash debris is removed when the relay restarts.
6. Lavalink on Hostinger reads the local cached file and sends Discord voice packets.

Only the download relay remains on the Mac. It runs at background priority and is idle
between requests. Its server waits for socket events instead of running Uvicorn's normal
10 Hz housekeeping timer; termination signals still wake shutdown immediately. The relay
has no request-count limit, supervisor heartbeat, or periodically refreshed Date header.
Its independently pinned release is recorded in `.audio/relay-release.json`.
There is no Mac Java audio process, continuous audio stream, or voice
connection. Existing Cobalt agents, ports, and cookies remain independent.

### Cache policy

The persistent Hostinger cache survives audio and bot releases. A play refreshes the
track's seven-day lifetime. Hourly cleanup and request-time cleanup remove expired idle
tracks. At capacity, the least recently played unleased tracks are evicted first. Playback
leases persist across resolver restarts and are released by the source plugin; orphaned
leases expire after two hours. No unrelated files or Red metadata are managed here.

`GENE_AUDIO_CACHE_BYTES` is set at installation to 10% of then-free space on the cache
filesystem (approximately 6.5 GiB on the current VPS), not a preallocated reservation.
`GENE_AUDIO_RESERVE_BYTES=2147483648` preserves at least 2 GiB of free space. Admissions
include incomplete downloads and metadata; low space fails closed rather than filling
the disk. Per-track limits remain 64 MiB and 30 minutes. Downloads are serialized and
identical cache misses coalesce. Limits and the seven-day TTL are explicit in the private
`/srv/gene/secrets/audio-resolver.env` file.

Cached display metadata contains only the video ID, title, artist, duration, and canonical
YouTube link. A cached link can play with the Mac offline. New searches still require the
home connection; Red may also answer previously cached metadata lookups itself.

### Install and verify

Build `ops/Dockerfile.audio` targets `node` and `resolver` as `gene-audio:<full-sha>` and
`gene-audio-resolver:<full-sha>`. Audio has its own maintainer-controlled Compose project:

```sh
docker compose --env-file /srv/gene/audio-release.env \
  -f /srv/gene/source/ops/audio-compose.yml up -d
```

`GENE_AUDIO_SHA` pins that project independently of feature releases. Its node joins the
existing private `gene_default` network as `gene-audio`; no host/public port is published.
The resolver shares the node's network namespace and binds `127.0.0.1:2334`. Both containers
share only the dedicated media volume (read-only for Java). The node's password and
resolver token are separate; the Mac relay has another token.

Stage the home relay using:

```sh
uv run --no-sync python scripts/install_download_relay.py --stage .audio/relay-stage
```

The ignored `.env.local` supplies `GENE_RELAY_TOKEN`, `GENE_RELAY_BIND` (Mac Tailscale IP),
`GENE_RELAY_PEER` (VPS Tailscale IP), and optional `GENE_RELAY_PORT` (2335 by default).
Install a clean committed release with `--release-sha <full-sha>`. Its immutable source
and environment live in `.audio/relay-releases/<sha>`, served by
`local.dan.gene.download-relay`. The relay binds only the configured Tailscale address,
rejects other source IPs, requires its token, and disables forwarded-IP trust.
`scripts/install_audio.py` now routes to this download-only installer.

Before switching Red, test the candidate's complete download, cache reuse with the Mac
unavailable, native decoding/seek, and sender timing. Stop the old Mac
`local.dan.gene.audio`, `.audio.resolver`, and `.audio.proxy` agents only after Hostinger
is healthy. Archive their plists outside `~/Library/LaunchAgents` so they cannot respawn
at login. Delete only their `media/` caches after they stop. Keep their immutable binaries
and archived plists for rollback; rollback can redownload missing songs.

Back up the old runtime/audio configuration before changing `GENE_AUDIO_HOST` to
`gene-audio`. Preserve the previous bot/controller image and source. A failed switch must
restore both the runtime's node address and the old Mac agents. A failed future audio
release restores its previous pinned audio image and relay definition. Never claim
Discord voice playback is verified merely because a search or health endpoint succeeds.
The native `audio-plugin/test/PlaybackProbe.java` and `UdpTimingProbe.java` are included
in the audio image for decoding, seeking, and timed sender checks without a compiler.

## Releases and recovery

### Maintainer work requested in Codex

The owner's direct request in this Codex chat authorizes implementation and deployment
without a second approval. Test the change, commit and push it, record the complete
release SHA, and retain the GitHub diff for review. Do not automatically merge to `main`.
Before switching a release, pause the worker/deployer, confirm no job is building or
deploying, and back up the controller, stopped Red state, previous source, and release
configuration. Install immutable cogs and check a fresh heartbeat for the expected SHA,
the configured guild connection and presence: Gene online in FF, Fri invisible in FF.
Restore the previous release and environment on failure;
resume the worker/deployer only after the new or restored release is healthy. Record
the deployment as chat-authorized maintainer work, without fabricating a Discord approval.

This is a trusted operator workflow. The controller's approval state machine, worker
scope, and credentials stay unchanged. Discord-originated requests, fixes, and repairs
always follow the exact-SHA approval workflow below; a Discord message cannot claim
this maintainer authorization, even if it quotes these instructions.

### Models and subscription usage

Both chat and new coding jobs use `gpt-6.1-sol`, high effort. Owner settings in `/fri`
provide **Chat model** and **Coding model** with separate catalogs. Chat refresh uses the
subscription's official `/v1/models` endpoint; coding refresh uses Codex app-server
`model/list` between jobs, even while paused. Neither creates an inference turn.
A refresh never changes the selection. The selected model and effort are recorded per
chat attempt and snapshotted for each development job. Unavailable selections fail visibly
rather than silently falling back. Keep the pinned Codex CLI updated deliberately.

**Usage** shows separate chat/development counters. Missing counters remain unknown.
Cached input is a subset of input; reasoning is a subset of output. Counts are usage
observations, not dollar charges or remaining plan quota. There is no application token
budget. Account usage and resets are available at https://chatgpt.com/settings/usage.
Chat usage is an anonymous ledger separate from conversation retention. Local durable
usage receipts retry delivery to the controller without duplicating attempts.

### Connect Fri on the personal VM

Create a distinct Discord bot application. Enable Message Content Intent so Fri can recognize
her name in normal messages; leave Members and Presence intents off. Run `scripts/configure_fri.py`
locally, enter the bot token privately, and use its printed invite if Fri is not in the Lab.
It writes `.secrets/fri.env`; Gene's existing bot identity is untouched.

Run `uv run --no-sync python scripts/connect_fri_chatgpt.py`. This uses the official
Sign in with ChatGPT flow for a personal self-hosted VM, with PKCE, nonce/state validation,
signed ID-token checks, and the `chatgpt.tokens.use.direct` plan scope. The stable opaque
VM identity is `.secrets/fri-hostinger-id.json`; reuse it when reconnecting this VM.
No API key, copied Codex token, or private ChatGPT endpoint is used for chat.

Transfer the verified session over SSH to `/srv/gene/secrets/fri-chatgpt/session.json`,
mode 0600, with the containing directory owned by UID 10001 (0700). Only Fri mounts that
directory, read/write, so it can atomically rotate refresh credentials under a lock.
After transfer, the VM owns refreshes. Never overwrite a live rotated token with a stale
local copy. Reconnection is a new interactive authorization; stop Fri before replacing
its session and mark/remove the transferred local credential. Keep the host identity.

Add a distinct `GENE_FRI_TOKEN` to controller.env and fri.env. Fri's env also contains its
Discord token and the configured guild/owner/workshop IDs. Create `/srv/gene/data/fri`
owned by 10001. The compose `fri` profile is opt-in until setup is complete; start with
`--profile fri up -d controller red fri`. Verify both bot heartbeats and a real subscription
reply before retiring Gene's development controls. Never run two gateways for one token.

The runtime image pins Fri's trusted implementation under `src/gene/`; it does not mount
mutable feature cogs, host files, the Docker socket, or development credentials. Fri's chat
has no executable tools. Gene receives only its functional service credential. Owner UI
callbacks and the controller enforce authorization independently of model text.

### Original split deployment (before Fri attention)

Only Gene's `runtime.env` uses the reviewed FF guild, workshop, highlight, and meme-source IDs
from the private `.env.production` configuration. Set `GENE_PRESENCE=online` and
`GENE_SHOW_FRI=false` there. Fri's `fri.env` and the controller's `GENE_GUILD_ID` and workshop
remain the Lab IDs; Fri always identifies as invisible. In `controller.env`, set
`GENE_RUNTIME_GUILD_ID` to FF so Gene's privacy endpoint accepts its users without extending
Fri's request/chat/approval authority outside the Lab. Worker and deployer credentials stay distinct.
Gene's saved Red settings remain per guild. Confirm FF text and voice permissions, fresh
heartbeats with the exact guild/presence/SHA, and guild-only command registrations after switching.
Remove the stale Lab Gene command after successful promotion, retaining its JSON for rollback.
Do not announce Fri or publish her commands in FF while she is under development.

Sources: [Sign in with ChatGPT](https://developers.openai.com/siwc/token-sharing-open-source),
[self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms),
[plan inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

### Discord requests: approve and deploy

New Discord requests enter `awaiting_owner`. They cannot reach the coding worker until
the owner reviews the complete stored request and selects **Allow coding**. The controller
binds that decision to a hash of the request and its source; changed requests and stale
buttons fail closed. Existing queued requests without intake approval are held during
migration. Intake approval never authorizes a live release.

Fri sends the configured owner a DM with an untrusted-text preview and the full request
as a text attachment. Once a tested proposal is ready, it edits that same DM with the
code link and complete SHA for release approval. Notifications use persisted message IDs,
short delivery leases, and ten-minute retries when DMs are unavailable. The configured workshop's
**Requests** panel remains a fallback. Delivery never grants approval. Only owner clicks
on Fri-authored review components can approve work from DMs. Verified members can have
bounded support conversations there, but other users' clicks, forged references, and stale
approvals remain rejected.

An allowed request produces a `codex/request-<id>` branch. The wrapper checks changed paths,
rejects symlinks and likely secrets, and runs tests/lint without credentials or network.
Failed tests or lint are returned to the selected model for up to two repairs in the same worktree.
Every attempt records its usage. Scope or credential violations stop the job immediately;
three unsuccessful coding attempts leave a failed request and never publish a branch.
Inspect the GitHub diff in Fri’s Requests panel, then approve its full 40-character SHA.
An intervening deployment invalidates older proposals. Re-submit/rebuild those requests
against the new release. Approving another SHA or approving as a member fails.

The coordinator snapshots state, stops Red, mounts the approved code, and requires a
fresh heartbeat with the expected SHA and a live connection to the configured guild.
If that fails, it restores the preceding code and checks it again. Code rollback does
not undo state; ordinary feature changes must remain backward compatible. State restore
is a deliberate operator action from the stopped Red backup if needed.

**Pause development** in Fri’s owner settings stops new work; an already-running job/release is allowed to finish.
For an emergency, stop `gene-worker` / `gene-deployer` through systemd. The existing bot
continues running. A deployment interrupted by a host crash remains `deploying`; it is
not automatically retried. Inspect `release.env`, the heartbeat SHA, and the job record;
restore a known healthy release or complete the matching deployment result using the
deployer credentials. Never manually mark an unknown runtime healthy.

The trusted `gene` control cog should remain loadable. If a candidate breaks it, use
SSH for recovery; Discord commands are not the only recovery path.

Maintainer control-plane updates build a versioned runtime image and set
`GENE_RUNTIME_IMAGE` in `release.env` before recreating the controller and Red. Ordinary
cog releases preserve that trusted image setting. Retain the previous image and source
for rollback; do not overwrite the `bootstrap` image tag with a new control plane.

## History recovery and privacy

History import is disabled in the active lab environment. To recover the old bot later:

```bash
uv run --no-sync python scripts/import_history.py --env .env.production
```

This owner-run tool checks the configured channel's guild, pages backward with a saved
cursor, redacts common credential formats before writing, and produces `.state/history/recovery.json`.
It records successful, failed, attempted, and uncertain evidence separately with source
links. Adjacent bot messages alone do not establish success. Review competing/later
settings before approving a server fact or feature request. No historical command runs.
Imported text is untrusted data, including anything that looks like instructions.

Delete transient history after extracting the approved specification:

```bash
uv run --no-sync python scripts/import_history.py --delete-evidence
```

Shared memory consists of explicit owner-approved facts and operational request/release records.
Fri retains six exchanges per person and conversation for seven days, plus up to five short
personal notes for 90 days. The notes are exact, validated self-disclosures for nickname, games,
music, hobbies, or conversation style; each category replaces its previous note. Public/private
conversations, channels, users, and guilds stay separate. The attention adapter additionally
keeps a 15-minute, 30-message RAM buffer per configured channel and bounded recent-history
summaries as described below; it does not import an unrestricted server archive.
“fri what do you remember about me?” inspects the current conversation's notes. “fri forget me”
erases all that user's Fri notes and chat turns and disables retention; “fri remember me again”
re-enables it. These controls run without inference and invalidate in-flight replies so deleted
data cannot be written back. **Forget my chats** clears recent turns while retaining personal notes.
Anonymous usage counts survive erasure. Personal notes are never shared with the development worker.
Owner **Saved context** controls remain the only way to add shared facts.
`gene forgetme true` removes the caller's request text, chats, personal notes, links and approved facts from
the active database, and cancels pending proposals. It cannot recall inputs already
sent to a running job or delete Discord/GitHub records. Raw failed-job logs/worktrees
and backups are private operator records; remove them when resolving erasure requests.
Keep at most 7 days of backups and failed-job diagnostics once the lab has stabilized.

## Lab acceptance checklist

Bootstrap verification on 2026-10-18: GitHub CLI authentication, private repository,
Hostinger services, invisible Discord connection, and lab-only slash registration
are verified. The core suite has 70 passing checks. A real GPT-6 Astra request
(`152a3c1aea3f`) repaired its failing test, passed 85 candidate checks and lint in the
isolated container, and published [the first draft proposal](https://github.com/HumanMint/gene/pull/1).
GitHub Actions also passed. That Discord proposal remains unapproved and has not been
deployed. Subsequent maintainer changes added the separate panel and external audio
source. The combined suite now has 119 passing checks, including owner review and
DM failure recovery coverage. The preceding playback/panel suite also passed isolated
Hostinger tests. A complete 174-second cached Nanobii Rainbow Road decode finished
with zero buffer underruns, as did the direct-stream comparison; the reported voice
pauses were not reproduced by these source-level checks. Rollback paths have isolated
test coverage. Sustained listening in Discord remains a human acceptance step.

- Invisible initial connection; no responses or command registration in other servers.
- Requests, member rejection for owner controls, stale SHA rejection, pause/resume.
- One real Codex job publishes a tested private branch and records actual usage.
- Owner approves exact commit; it becomes healthy; a broken release restores prior code.
- Meme add/remove/dual/self votes, deleted originals, duplicate events, restart recovery.
- Play/search/queue and at least two sustained tracks in voice; then a Mac-offline test.
- Controller/worker unavailable while ordinary Red commands remain usable.
- Historical evidence reviewed before restoring any original-server behavior.

## Primary references

- [Red audio](https://docs.discord.red/en/stable/cog_guides/audio.html)
- [Red-compatible Lavalink](https://github.com/Cog-Creators/Lavalink-Jars/releases/tag/3.7.13%2Bred.5)
- [Codex trusted private automation](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
- [Codex noninteractive output and usage](https://learn.chatgpt.com/docs/non-interactive-mode)

## Fri attention and gradual learning

### Conversation access to learning

Fri can now select one bounded harness action during an ordinary conversation:
`learning_overview`, `read_recent`, or `run_learning`. The model selects a structured
request; trusted Python checks the actual Discord identity and audience, the controller
checks owner identity for learning administration, and the final Sol reply uses the
confirmed result. There is at most one action and two conversation model calls per
turn; both calls receive separate usage receipts, including failed calls.

In a private owner conversation, ask “what have you learned about us?” for up to three
recent learning reports and five current summaries, or “run a learning pass now” for
one pass. Manual passes use the existing cursor, batch size, daily reserved scan budget,
pause state and report-delivery gate. They may bypass the scheduled due time, but run
at most once every five minutes and cannot replay an exhausted archive. Reports say
“Conversation summary”; their contents remain untrusted evidence internally.

“Have a look around” or “read recent chat in memba-chat” can read up to 20 recent text
messages from one configured, non-NSFW channel the requester can currently read.
Public replies can only retrieve their own channel; private replies can use another
approved channel visible to the requester. Private threads are excluded. Bot messages
and opted-out authors are skipped. Default history is at most seven days, with fri-chat
additionally limited to messages after this agent was enabled. Explicit quoted archive
search remains available separately; old Fri output is never startup personality data.
These reads inspect text only, not meme images or attachments.

Access and source privacy are checked again after inference. Lookup-derived replies
are not persisted as chat turns or personal notes. Only a neutral action/channel receipt
is kept alongside the request, so follow-ups can fetch fresh evidence without leaving
copies of someone else's messages after source erasure. Learning reports and
administrative details are only available through these actions to the owner privately.
Questions about learning no longer go straight to the settings helper.

Fri's compact voice foundation is `VOICE` in `src/gene/fri_chat.py`, shared with archive
answers. It uses a few contrasting tone examples rather than retaining a full style
quiz in every prompt. Owner voice notes remain a separate, versioned preference;
style examples never count as evidence of completed actions or bypass approval.

Fri now has a persistent Hostinger heartbeat and a Discord adapter. Gene's Red
extensions and separate audio services are unchanged. Promote Fri and the
controller's primary guild/workshop together to FF; preserve the prior Lab env
and command registration for rollback. Fri remains invisible and no global
commands are registered.

Set `FRI_CHANNEL_IDS` in both controller.env and fri.env to the explicit allowed
channels. Set `FRI_HISTORY_CHANNEL_IDS` and `FRI_LEARNING_ROLE_ID` in controller.env.
Set `FRI_CHAT_CHANNEL_ID` and `GENE_BOT_ID` in fri.env. Give Fri only the necessary
View Channel, Read Message History, and Send Messages permissions in those channels.
Members and Presence privileged intents remain off. Historical author membership
is checked with bounded individual member lookups, not a full member import.

For a second, chat-only testing room, set `FRI_TEST_GUILD_ID` and
`FRI_TEST_CHANNEL_ID` in controller.env. Fri reads this scope from the controller.
The current Lab room is `#tests_n_stuff`; no extra slash registration is needed.
It uses the same mentions, name addressing, two-minute follow-ups, and conservative
Luna attention as shared FF channels. Conversation turns and personal preferences
use the actual Lab guild ID, and Lab cooldowns have a separate allowance. FF facts,
reports, summaries, and history never enter Lab replies. Global voice preferences
still apply so the room tests the real personality. Lab is excluded from automatic
history learning, Gene error investigation, and feature/release requests. Owner
settings remain in FF or owner DMs. Privacy opt-out is global for that user.
The Fri health file includes Lab connection and view/send/history permission checks.

Set `FRI_WEB_SEARCH_ENABLED=true` in controller.env to offer live public web lookup.
It uses the same official subscription OAuth and the selected conversation model
and effort, with `web_search` and `external_web_access: true`; no API key is needed.
Fri chooses a lookup when current or uncertain public facts warrant it. A lookup
receives only a short exact excerpt from the current message naming the public
topic, plus the current UTC date. It receives no Discord history, owner reports,
saved facts, personal notes, or credentials. If only prior/private context identifies
the topic, Fri asks the member to name it. Private identifiers and local addresses
are rejected. There is one lookup per reply, a 90-second deadline, and the stream
stops after observing a fourth search/open operation. This bounds the local attempt,
not a guaranteed provider billing limit; the subscription route has no supported
`max_tool_calls` field. No tool runs at all for ordinary chat.

Only a completed web call with provider citation annotations can produce a search
receipt. Fri rewrites it in her voice and the harness appends verified source links.
Failures are reported without inventing a searched answer; the receipt cannot
request further actions or create a feature job. Webpages remain untrusted evidence.
Live access allows fresh page retrieval, but search indexes, inaccessible sites, and
page publication dates still affect freshness. Usage appears separately as
`web_search`; the ordinary selection and reply calls remain `conversation` usage.
See the official [live web access documentation](https://developers.openai.com/api/docs/guides/tools-web-search#live-internet-access)
and [subscription route limitations](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations#responses-api-requirements).

The rollout uses memba-chat and public-chat for retrospective learning, the existing
fri-chat for fresh conversation, ff_beta for the workshop, and bot-commands for
Gene error observation. Old fri-chat history is explicitly excluded from startup
learning, even if accidentally added to the history list. No bot-authored messages
enter background personal learning. Each pass examines at most 30 messages, seven
days back, with a 360-message scan allowance per 24 hours from enablement. The
first 24 hours use a 30-minute interval; later passes use six hours. These are
owner-adjustable within validated bounds. An exhausted channel is not rescanned
for a week; its cursor and the scan allowance survive restarts.

Every history pass queues an owner DM reporting its channel, eligibility, date
range, exact saved disclosures, model/effort, measured usage, and next eligible
run. No eligible source means no model call. An undelivered report pauses further
history learning. A crashed claim reserves its original allowance and produces
an interruption report rather than replaying indefinitely. Reports are retained
for seven days after delivery. Discord delivery can be repeated after a crash
between sending and acknowledgement; the batch identifier makes this visible.

Luna low decides whether new shared-channel activity warrants participation or
investigation. Luna medium summarizes bounded source batches and suggests exact
self-disclosed preferences. Sol High handles Fri's voice, owner setting proposals,
archive answers, and read-only Gene error assessment. Background model/effort
choices are visible and configurable. Usage is grouped by role, model and effort,
including incomplete/failed attempts. API prices are not subscription quotas.

Conversations receive recent context from their own channel only. An active
exchange continues for two minutes without repeated invocation; short message
bursts are grouped. Dedicated fri-chat receives natural conversation. DMs receive
no public-channel buffer or summaries. Source text and summaries are always
untrusted input. Background preferences require a valid source ID, role, exact
first-person evidence, permitted category, and unchanged privacy revision. Older
history cannot overwrite a more recent disclosure. Summaries expire after two
days; personal notes use the established 90-day limit. Opt-out erases derived
summaries and retained reports, clears the runtime buffer, and blocks in-flight
memory resurrection. Already delivered Discord messages and backups are separate.

An explicit request such as `fri search history in #fri-chat for "Minecraft"
before 2025-01-01` checks at most 50 messages before that date and returns at most
five matches with links. The requesting member must currently be able to view
and read history in that configured channel. Old bot replies are source evidence
only. Public results stay in their source channel; cross-channel lookups require a
private DM. Archive search does not store personal notes or change Fri's personality.
This is a bounded slice, not a claim to search the entire server archive.

Members get eight DM/private-panel turns or twenty public turns per 30-minute
window before a randomized 10-20 minute conversation cooldown. Allowances are
persistent and shared across channels within each space. One prepared notice is
sent; repeated pings do not cause model calls. This is not a Discord moderation
timeout and does not affect Gene. The owner is exempt, and privacy controls work
without inference even while a member is on cooldown.

The owner can DM Fri `show settings`, `show usage`, `refresh models`, or describe
an adjustment, such as `change learning to once an hour` or `change your voice to
be less sarcastic`. Natural-language changes produce a concrete JSON preview and
an owner-only Apply button, expiring after ten minutes. The controller checks
identity, allowed keys, bounds, catalog membership, and the settings revision.
Changes are audited; stale previews fail instead of overwriting newer changes.
Voice, attention and learning notes affect only their respective tasks. Channels,
permissions, identity, release policy, and execution access are not editable here.
No new code release is needed for these narrow runtime preferences.

Only errors actually authored by Gene can trigger automatic read-only investigation.
Luna must select the error first, then Sol assesses its bounded text and controller
status. A proposed repair enters the existing stored-request owner review workflow.
No coding begins until intake approval; no deployment or repair bypasses exact-SHA
release approval. No shell or Docker credentials are exposed to Fri's model.
