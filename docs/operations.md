# Operations

For a new installation, start with [setup](setup.md). Fri now runs from
[its own repository](https://github.com/HumanMint/fri); use its independent release
coordinator and [migration boundary](fri-migration.md). Older Fri setup sections below
describe the retained legacy installation and are not the new companion deployment path.

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

### Gene Docker image retention

`scripts/gene_image_retention.py` is a standalone, root-owned host maintenance tool.
It does not rebuild or restart Gene, change controller approvals, delete backup/source/data
directories, or touch another application's images, containers, volumes or build cache.
The only removable tags are `gene-runtime:<full-sha>` and `gene-worker:<full-sha>`.
Inactivity, age and an empty tag list are never retirement evidence.

**Release history.** Releases are recorded two ways, and the tool reads both:

- `batch-release.sh` (the daily release): `staging/batch-<G>-<F>[-N]/manifest.json` plus its
  `journal` (`staging/batch-<G>/` before `1ad1493`, whose manifest names no old pair). A batch is
  in service when its journal has `gene_recorded`, `fri_switched` and `resumed`, and neither
  `intent:rollback` nor `resumed_old`. A batch that changed Gene is one rollback level; a Fri-only
  batch moves no Gene. These releases write no controller `deployments` row.
- Older installs: the controller's successful `deployments` rows, with `backups/maintainer-<sha>`
  or, for cog-only feature/repair/rollback jobs, `backups/<job>`.

From the controller's `active_sha`, each step back is the latest earlier release of that SHA in
either record; two at the same time refuse as ambiguous. The tool keeps two complete rollback
levels behind the live release. Cog-only steps are traversed without counting. A batch level is
complete by the checks its own `rollback-gene` makes before changing anything: the controller's
`previous_sha` names its old Gene (first step), the live runtime and worker pins are its new
images (first level), every tag in its manifest (old and new runtime, worker and Fri) still names
the recorded image ID, its switch backup under `backups/` has `release.env`, `worker.env`,
`fri-release.env`, `fri-release.json`, `db/controller.sqlite3` and `installed-source/`, the
backup's pins are the manifest's old pair, and `releases/<old Gene>/cogs` exists. The live Fri
pins must be the Fri of the latest batch in service. A legacy maintainer level is complete with
`installed-source/`, `controller.sqlite3`, `red.tar.gz`, `fri.tar.gz`, pins naming the previous
release, and its cogs. An incomplete level inside the two protected levels refuses and names what
is missing; levels beyond them are not consulted.

A malformed, unreadable or symlinked record, an unrecognized `staging/batch-*` directory, a missing
or retagged image, an incomplete backup, disagreeing pointers or pins, a live release whose batch
rolled back or never recorded its switch, and ambiguous history all refuse (exit 1). A batch in
flight (drained or paused without resuming, or mid-rollback), a held `batch-release.lock` (read
from `/proc/locks` without taking it), a Gene build or deployment, a Docker build, a pending
deployment receipt or another retention run skip the run instead (exit 4): nothing changes and the
next window tries again.

**What it keeps and what it removes.** It removes a release tag only with supersession evidence
past a 24-hour grace period: a successful deployment's backup, a batch in service (its old Gene
images retired when it resumed, dated by its journal), a configured version the tool saw replaced,
or an explicit `retired_images` entry. It always keeps container references (including stopped
containers), the current pins and running services' images, every image of both rollback levels,
pending proposal/approval versions, holds, configured references, audio and other repositories.
It also keeps every image in the manifests of the three most recently resumed batches and of any
unfinished batch, which is `batch-release.sh cleanup`'s own keep rule (`KEEP_RELEASES`, default 3),
so the two tools never disagree about a rollback. Reactivation renews the grace period.

Ownership between the two cleanups (owner request, gene #76):

- `fri-runtime:<sha>` release tags belong to `batch-release.sh cleanup`. This tool never removes
  them; its plan lists them as kept, with that reason.
- `cleanup` removes release tags with a plain `docker image rm`, so Docker also prunes that image's
  unnamed parents. This tool removes with `--no-prune`, then only the exact unnamed ancestors it
  recorded from a retired Gene image before untagging it. Tagged ancestors stay. If Docker keeps a
  retired runtime object after its last managed tag for worker children, that recorded ID stays
  eligible for a later plan. Exact-ID removal intent is synced before invoking Docker.
- Untagged objects neither tool recorded (dangling rebuilds, intermediates of kept images) are never
  removed automatically. The plan names each one either the parent of a kept image or unclassified,
  for a maintainer to review separately rather than widening the deletion rule.
- There is no `docker system prune`, no forced removal, and no implicit parent prune. Docker's normal
  dependency checks keep any parent with remaining children or containers.

**The plan.** Every run writes `image-retention-plan.json` (version 2) before it removes anything:
`delete` (exact image IDs, their tags, evidence and retirement time), `delete_parents` (exact
unnamed ancestor IDs), `keep` (every image object `docker image ls -a` lists, with the reasons it
stays), `keep_summary` (counts by first reason), `lineage` (each rollback level and its record),
`live`, `deletion` (`enabled`, `disabled in the policy` or `plan only`) and `bytes`. A planned image
a kept image is built on carries `docker_keeps`. `bytes` counts the layer bytes the removals free
without counting a shared layer twice: images share a layer only at the same place in their chain,
an image's size fixes the cumulative size where its chain ends, and the removals free the chain
prefixes no kept image uses. Where no image ends at a prefix (BuildKit leaves no intermediates)
its size is bounded by the images around it, so the plan gives `lower_bytes` and `upper_bytes`,
equal (`exact`) once every prefix is known. They assume every removal succeeds, and they are
Docker's uncompressed sizes: with the containerd image store, compressed blobs are freed too.
Measure `df` before and after. Docker's own reclaimable figure is not used.

**Schedule and the window.** The timer starts the service daily at 04:40 America/Los_Angeles, with up
to five minutes' delay and no catch-up (`Persistent=false`). Planning and removal both run only
inside the maintenance window, [04:00, 05:00): a plan run takes the tool's lock and writes the plan
and status files, so it is a production action too. Outside the window a run takes no lock, writes
nothing but its log line and exits 4; `MAINTENANCE_EXCEPTION='<who, when, why>'`, on the owner's
explicit words, lets it run and is logged. The window is checked again before each removal; at the
close the rest wait for the next window (`deferred`). Removal needs both `--apply` (the service
passes it) and `"apply_enabled": true` in the policy. Without the policy flag every run only plans,
so a newly installed or repaired tool removes nothing until a maintainer has reviewed its plan.

**Health.** Each run inside the window updates `image-retention-status.json`: the last run (mode,
outcome `ok`, `skipped` or `failed`, detail), the last success with its exact plan and removals, and
the failures since. `gene_image_retention.py --status` is read-only (no lock, no writes, no
container), so it can run at any hour. It combines that record with the timer's and service's
systemd state and reports `status`: `ok`, `failing` (the last service run or recorded run failed),
`disabled` (the timer is not enabled and active), `never-run`, `stale` (no success for 50 hours, two
missed windows) or `unknown`, with `healthy`, `reasons` and `last_success_at`. It exits 0 only when
healthy. An enabled timer whose service keeps failing reads as failing, not healthy.

**Production-action log.** Every run, refused, skipped, failed or successful, appends one line to
`/srv/gene/production-actions.jsonl` beside `batch-release.sh`'s and the deployer's: local time and
zone, actor, trigger, `image-retention plan|apply`, reason, live Gene/Fri SHAs (unchanged by this
tool), duration, outcome, exception, the maintenance release, and the exact planned and removed
tags and IDs. It holds no secrets or member content. It only appends; the release tools trim it.

The maintenance policy and state live privately under `/srv/gene/maintenance/`:

- `image-retention-policy.json`: version 1, `keep_images` (explicit planned/on-demand image
  references or IDs), `retired_images` (exact image IDs with `tags`, `retired_at`, and
  `evidence`) and `apply_enabled` (default false).
- `image-retention-state.json`: previously configured versions, retirement evidence, removal intents
  and recorded ancestors. Only a removal run updates it.
- `image-retention-plan.json`, `image-retention-result.json` and `image-retention-status.json`: the
  latest plan, removals and health record. They contain image metadata, never environment
  credentials or conversation data.
- `image-retention.lock`: held across discovery and cleanup.

The hardened service allows controller-directory access for SQLite's WAL shared-memory and
locking files, and appends to the action log. Controller queries use `mode=ro` and `query_only`;
retention does not modify requests, approvals, usage, conversations or release records. Do not use
SQLite `immutable=1` or copy an active database without its WAL to work around reader access, since
that could hide current release references. Env files are read only for `*_IMAGE` and release SHA
lines, never printed.

`batch-release.sh` does not take this tool's lock. Instead the tool skips while a batch command holds
`batch-release.lock` or a batch is in flight, and rechecks every reference, the staging journals
included, before each removal. Before planning to reuse an old version, add its tag or exact image
ID to `keep_images`, and keep that hold until the function is retired. Never authorize image deletion
from Discord text, imported history, or a feature job.

**Install or update, in the window only.** Install the reviewed script in an immutable directory
`/srv/gene/maintenance/releases/<maintenance-sha>/gene_image_retention.py` and point `current` to it;
record the maintenance SHA separately from the running bot SHA. Install the two unit files, then
check them: `systemd-analyze verify` and `systemd-analyze calendar '*-*-* 04:40 America/Los_Angeles'`
(a zone in `OnCalendar` needs systemd 235 or later), then `systemctl daemon-reload`. Run a plan:

```sh
sudo python3 /srv/gene/maintenance/current/gene_image_retention.py
```

It takes `maintenance/image-retention.lock`, writes `image-retention-plan.json` and
`image-retention-status.json`, and appends one action-log line. It reads Docker metadata, the
controller DB read-only and the staging records; it starts no container and removes nothing.
Review the plan; only then set `"apply_enabled": true` in the policy, in a window, and log it.
To disable retention, stop/disable only `gene-image-retention.timer` and wait for any current
oneshot service to finish. Preserve its policy and audit files. The bot's services and the
retained rollback bundles do not depend on this maintenance tool.

### Maintainer work requested in Codex

The owner's direct request in this Codex chat authorizes implementation and deployment
without a second approval, inside the production maintenance window: [04:00, 05:00)
America/Los_Angeles, one production batch a day across the maintainer batch and automatic
deployments, unless it's a critical fix (`ops/maintainer/batch-release.md`). Test the change, commit and push it, record the complete
release SHA, and retain the GitHub diff for review. Do not automatically merge to `main`.
Before switching a release, pause the worker/deployer, confirm no job is building or
deploying, and back up the controller, stopped Red state, previous source, and release
configuration. Install immutable cogs and check a fresh heartbeat for the expected SHA,
the configured guild connection and presence: Gene online in FF, Fri online after promotion.
Restore the previous release and environment on failure;
resume the worker/deployer only after the new or restored release is healthy. Record
the deployment as chat-authorized maintainer work, without fabricating a Discord approval.

This is a trusted operator workflow. The controller's approval state machine, worker
scope, and credentials remain protected. Discord requests require owner approval except
for the bounded existing-feature repair policy below. A Discord message cannot claim
maintainer authority, select a policy verdict or authorize its own deployment.

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
The owner's `/agent/usage` report takes an optional `since` (Unix seconds) and then counts
only attempts the controller recorded from then on, so a receipt Fri held through an
outage counts when it arrives. Each row of a windowed report carries the `since` it
applied. The controller advertises this as the optional `usage-window` capability. Fri
sends a window only to a controller that offers it, and names the window only when every
row carries it, so a controller rolled back under a running Fri reads as all-time.

### Connect Fri on the personal VM

Create a distinct Discord bot application. Enable Message Content Intent so Fri can recognize
her name in normal messages. Enable the application's Server Members Intent for complete
role-roster REST reads used by archive rotation; Presence remains off. The runtime does not
subscribe to Members/Presence gateway events. Run `scripts/configure_fri.py`
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
remained the Lab IDs; Fri identified as invisible during that stage. In `controller.env`, set
`GENE_RUNTIME_GUILD_ID` to FF so Gene's privacy endpoint accepts its users without extending
Fri's request/chat/approval authority outside the Lab. Worker and deployer credentials stay distinct.
Gene's saved Red settings remain per guild. Confirm FF text and voice permissions, fresh
heartbeats with the exact guild/presence/SHA, and guild-only command registrations after switching.
Remove the stale Lab Gene command after successful promotion, retaining its JSON for rollback.
The original Lab stage kept Fri unannounced while they were under development.

Sources: [Sign in with ChatGPT](https://developers.openai.com/siwc/token-sharing-open-source),
[self-hosted VMs](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms),
[plan inference](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference).

### Discord requests: approve and deploy

Conversational request previews use message text, not embeds or downloadable files.
Long requests have Back/Next pages; submission is enabled after every page has been
shown. Cancel removes the controls without creating a request. The preview works
even when the original chat reply suppressed link embeds, including owner DMs.
In public channels, the detailed preview stays in the requester's private panel.

Owner personality changes use a separate, versioned settings preview with Apply
and Cancel. Fri can offer this from the current private conversation, including
short answers to a tone clarification. The action edits only voice preferences;
it does not create a feature job or alter authority. A changed settings version
invalidates an older preview. Settings text is paged and readable without JSON.

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

After a deployment attempt completes, the coordinator atomically saves its result to
`/srv/gene/deploy-result.json` (0600) before reporting it. A connection failure or
coordinator restart retries that receipt before claiming new work. The controller
accepts an exact duplicate lease/outcome once and never reapplies an older release.
Changed outcomes or leases are rejected. Retrying a receipt never reruns deployment.
If the coordinator dies before recording a known result, the existing deployment
lease remains blocked for operator reconciliation; do not invent or replay its outcome.

Approved feature and repair jobs deploy only in the maintenance window, and only while the day's one
production batch is unused: an automatic deployment counts once claimed, whatever its outcome, and a
maintainer batch once it drains. A held job stays approved for a later day. A fix for a crash, data
loss, a privacy leak or a broken rollback can skip the cap, never the window: on the owner's explicit
words, mark it with `python -m gene.maintenance critical <job id> --reason '<P0 reason>'` on the host
(`critical-list` shows the marks). Rollbacks run at any hour and never count.

**Pause development** in Fri’s owner settings stops new work; an already-running job/release is allowed to finish.
For an emergency, stop `gene-worker` / `gene-deployer` through systemd. The existing bot
continues running. A deployment interrupted by a host crash remains `deploying`; it is
not automatically retried. Inspect `release.env`, the heartbeat SHA, and the job record;
restore a known healthy release or complete the matching deployment result using the
deployer credentials. Never manually mark an unknown runtime healthy.
Check for `deploy-result.json` when reconciling a stuck release. A valid pending result
will normally clear itself after controller connectivity returns. Preserve and inspect
an invalid or conflicting receipt privately instead of deleting it to unblock claims.

A deployment that stopped Red and then failed without any release proving healthy, its
restore included (or whose restore couldn't start), reports `runtime_unknown` with its
failure. While that result waits for the controller, `deploy-result.json` keeps the four
fields every deployer reads and `deploy-runtime.json` beside it names the same job and lease,
so a deployer downgraded below this change still finishes the receipt (without the runtime
state) and claims again; the next receipt replaces the leftover file. Treat an invalid
`deploy-runtime.json` like an invalid receipt. The controller keeps `active_sha` as the
last release a health check proved and records the runtime as unknown: `/status` shows
`runtime_unknown` (the job, the release it tried, the one it tried to restore and since
when). Until it's cleared, approved feature and repair jobs wait unclaimed, and the
reporter reply for a shipped repair refuses. An owner
rollback can still go out at any hour: it backs up, proves its release healthy or restores
the recorded one, as usual. A deployment that proves its release healthy makes the runtime
known again. Otherwise, once a maintainer has the release `active_sha` names running with a
fresh healthy heartbeat, they clear it on the host, which writes an audit row:

```sh
sudo /srv/gene/venv/bin/python -m gene.runtime_state status /srv/gene/data/controller/gene.sqlite3
sudo /srv/gene/venv/bin/python -m gene.runtime_state clear /srv/gene/data/controller/gene.sqlite3 \
  --sha <active_sha> --actor maintainer:<name> --reason '<what was checked>' \
  --health /srv/gene/data/red/gene-health.json --log /srv/gene/production-actions.jsonl
```

It refuses another SHA, a stale or unhealthy heartbeat, one written before the runtime
became unknown (or before a newer failure marked it again), or a deployment in flight. It
reads the heartbeat under the controller's write lock, so no failure lands between. If a
different release is what's running, record it through a maintainer release first.
`batch-release.sh` doesn't read or clear the state; after its `resume`, clear it with the
release it recorded. A deployer from before this change reports no runtime state, as before;
an older controller ignores the field, and a Gene rollback below it leaves the setting in
place, unread. Receipts compare on success and detail alone in every version, so a replay
crosses an upgrade or rollback either way. When an older controller committed a receipt
whose acknowledgement was lost, the upgraded one records the replay's `runtime_unknown`
once, unless a later claim, a maintainer clear or a recorded release has come since.

The trusted `gene` control cog should remain loadable. If a candidate breaks it, use
SSH for recovery; Discord commands are not the only recovery path.

Maintainer control-plane updates build a versioned runtime image and set
`GENE_RUNTIME_IMAGE` in `release.env` before recreating the controller and Red. Ordinary
cog releases preserve that trusted image setting. Retain the previous image and source
for rollback; do not overwrite the `bootstrap` image tag with a new control plane.

Release archives may be unpacked under a private service umask. Normalize only the
verified Git source to readable files and traversable directories before building;
keep staging, logs, backups, credentials and state private. The runtime and worker
images retain root ownership of checked-in code while making `/app/src`, `/app/cogs`
and worker `/app/tests` readable by UID 10001. Before fencing workers or switching
services, run an isolated image import check as UID 10001, with no network,
credentials or state mounts, and verify the candidate controller contract. Image
revision labels alone do not establish that the runtime can import its source.

Maintainer merge checks include a fresh-context independent review of the candidate,
then offline reproductions and regressions for confirmed issues. Record the reviewed
SHA, functional outcomes, and limits separately from the overall test count. Deployment
receipt tests exercise real controller/database state with failed delivery and lost
acknowledgements, but substitute actual host activation. Thread-control tests connect
Fri's action handler, the controller, and Red's command checks with fictional Discord
events; they do not establish audible music playback.

Fri can hand ordinary Gene controls to public threads under approved FF channels.
The controller retains the actual thread as the destination; Gene rechecks its parent,
current member/bot thread permissions, source author, and original message before
execution. Private threads, a different parent, and redirected public requests fail
closed. Lab controls still cannot operate FF music, and Red's own command checks apply.

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
Fri retains six exchanges per person and conversation for seven days, plus short personal notes for
90 days. The notes are exact, validated self-disclosures for nickname, games, music, hobbies,
conversation style, or projects (what the member is working on, building, maining or has been
playing), with at most three separate entries per category. Each member has one server
profile (up to fifteen notes) for what they told Fri in a channel everyone in the server can read;
Fri decides that from Discord's permissions when it saves, and the profile appears in every
conversation with that member in the server, including their DMs. Notes said in a DM, the private
/fri panel, a restricted channel or a private thread stay in that conversation (up to ten each), as
do all notes saved before member profiles. A projects note always stays in the conversation where
it was said, even in a channel everyone can read; only the member's own Share server-wide in My
memory moves one into their profile. Fri also sees the member's own latest two messages to it
from other everyone-readable channels in the past twelve hours, without its replies there, since a
reply can draw on a note that stays in its own conversation; private conversations never reach
another conversation. A Fri that doesn't ask for profiles (an older release) gets only the current
conversation's notes, as before, so its lists stay within the bounds it was built for, and none
that was learned while another member's words were in view, since it can't carry their provenance
into a reply. Users and
guilds stay separate. The attention adapter additionally
keeps a 15-minute, 30-message RAM buffer per configured channel and bounded recent-history
summaries as described below; it does not import an unrestricted server archive.
“fri what do you remember about me?” inspects the notes the current conversation can see; in a DM it
lists every note Fri holds about the member, labelled by where it shows. “fri forget me”
erases all that user's Fri notes and chat turns and disables retention; “fri remember me again”
re-enables it. These controls run without inference and invalidate in-flight replies so deleted
data cannot be written back. **Forget my chats** clears recent turns while retaining personal notes.
Anonymous usage counts survive erasure. Personal notes are never shared with the development worker.
**My memory** edits or removes an individual entry without inference; in a DM it covers every
note, in any scope. An edited note keeps its place: the new wording shows wherever the note did, as
the list labels it. A note learned while other members' words were in view keeps their provenance, like
the turn that saved it, and goes when any of them erases or changes their privacy settings. A note
saved before this, or learned from channel history, has no record of whose words were in view: it stays
a conversation note, shown only where it was said, as before, and a profile note always has one. Rewording
a note makes it the member's own words. Changes are revision-fenced, clear stale recent context and research receipts
(every conversation in the server for a profile note), and prevent historical learning from restoring
the removed disclosure: nothing the member said there before the change becomes a note, archive
observation or channel summary, and a message or outside @mention sent before it but answered after
keeps nothing (`/chat/turn` answers `kept: false`); `/agent/context` reports the member's latest edit as
`removed_at`, so Fri's own short-lived channel caches drop what they said before it. Other
excerpts of the same words (one containing the other) go with the note, under any category; for a
profile note, wherever a single conversation kept them, and its removal covers older messages in every
conversation of the server. A fresh disclosure afterwards still counts.
**Member summary.** Fri also keeps one short summary per member (at most 500 characters) of what they
talked about with it lately, beside the six recent exchanges, so a topic that fell out of them can be
picked back up. It is the only model-written text about a member the controller stores, and Fri's model
sees it labelled as Fri's own untrusted summary, never instructions. Fri builds it in its background
loop with the internal summary model (`summary_model` / `summary_effort`, Luna at medium by default),
only while background work and learning are on, at most once per member a day and 50 times a day in
all, once the member's latest exchange is an hour old and at least two new ones are kept
(`/agent/member-summary/claim`, then `/finish`). It reads only the member's own words in exchanges saved
from channels everyone in the server could read, at their current privacy revision, never Fri's replies,
DMs, the /fri panel or a restricted channel, plus the previous summary, and Fri drops any whose channel
is no longer readable by everyone before the call. The finish is fenced: it keeps nothing if the member's
revision or that of anyone those words drew on changed since the claim (409). The summary carries that
provenance and goes when any of them erases or changes their privacy settings; the member's own
revision change (forget me, memory off, Forget my chats, any note edit) removes it too. It expires 30
days after its last refresh. Fri shows it to its model in DMs and in channels everyone can read, while
every channel it came from still is, and never in another member's turn; replies built on it inherit
its provenance. In a DM, "what do you remember" and My memory show it, and My memory edits or clears it
(`/chat/memory/summary`, private conversations only), which, like a profile note edit, clears the
member's saved chats across the server and moves their revision. Only a Fri that sends `summaries`
gets it in `/chat/context` and `/chat/memory/notes`; the contract advertises `member-summaries`.
The item-storage upgrade preserves existing IDs and dates in one SQLite savepoint. A rollback to
the earlier single-note release needs its matching database backup, because that older writer
expects a different uniqueness constraint. Retain the pre-release database backup with the code SHA.
New commands can still hold a five-minute clarification and bounded execution receipts
while personal memory is disabled; these operational records do not become personal notes.
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
confirmed result. Learning actions use one action and two conversation model calls per turn. The
only two-action path is a web lookup followed by one explicitly requested Gene command,
using at most three conversation calls. Every call has a separate usage receipt.

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

Fri uses they/them. They answer pronoun questions briefly and can make a light correction
in conversations they already participate in; this does not change attention/admission rules
or cause them to interrupt unrelated messages. The compact voice foundation is
`VOICE` in `src/gene/fri_chat.py`, shared with archive
answers. It uses a few contrasting tone examples rather than retaining a full style
quiz in every prompt. Owner voice notes remain a separate, versioned preference;
style examples never count as evidence of completed actions or bypass approval.

Live chat requires an actual Discord `Message` or `Interaction` object at the
harness boundary. The controller also requires matching author, channel and guild
source metadata before saving a turn. Each new turn retains its event ID, display
name at the time, and event timestamp; message sources include a Discord link.
The current speaker's ID and current server display name are supplied explicitly.
IDs distinguish members with identical nicknames; names provide natural addressing.
Old records without source receipts are marked `legacy_unverified`, not silently
promoted into verified quotations. Previous bot replies and style examples never
prove a member said something. The voice should stay brief for casual check-ins
and verify disputed attribution rather than arguing from uncertain memory.

Never call `Fri.talk` with fabricated owner messages for a live smoke test. Run
`docker exec gene-fri-1 python -m gene.fri_eval` for six isolated voice/attribution
cases using fictional members. This evaluator calls `Chat.respond` directly with
empty server facts, disabled personal memory and no actions. It has no conversation,
note, settings, learning or Discord-message write path; only anonymous usage is
saved under `evaluation`. Inspect the replies against the printed expectations.
Keep evaluation output private. Production membership/permission checks are
read-only; do not turn real users or channels into synthetic conversation fixtures.

The October 2026 repair removed confirmed maintainer test rows and their observed
contaminated replies from the active database, retaining a private pre-repair backup.
Do not restore those rows into active chat context during rollback. Test scripts
that previously injected owner conversations are retired; source receipts and the
isolated evaluator are required for future validation.

Fri now has a persistent Hostinger heartbeat and a Discord adapter. Gene's Red
extensions and separate audio services are unchanged. Promote Fri and the
controller's primary guild/workshop together to FF; preserve the prior Lab env
and command registration for rollback. Set `FRI_PRESENCE=online` in fri.env for
public presence (omitting it keeps new installations invisible). No global
commands are registered.

### Daily public statuses

Enable `status_enabled` through a versioned owner preview or an audited maintainer change.
One `gpt-6-luna` / `low` task creates a daily batch of up to four short lines; the cached
batch rotates every six hours without further inference. `status_model`, `status_effort`
and `status_rotation_minutes` are visible and owner-adjustable. Generation is limited
to one attempt per rolling 24 hours, including failures/restarts, and avoids the previous
12 generated lines. Failures keep a cached/neutral line, with bounded gateway retries.
Usage appears as "daily statuses." Invisibility or disabling status generation clears the
activity and avoids source reads/inference. This never changes the online/invisible setting.

Set `FRI_STATUS_CONTEXT_CHANNEL_IDS` in controller.env to the explicit read-only source
allowlist, such as memba-chat and public-chat. Once per daily batch, read at most 20 human
messages per channel from the past seven days, in at most two channels. Existing membership,
opt-out, source permissions and NSFW checks apply and are rechecked after inference.
Only canonical labels from a small game/music/meme vocabulary enter the model.
Raw messages, identities, quotes, histories, owner voice notes and learning reports do not.
Private memba-chat may inform broad topics only because the owner explicitly allowed it;
the resulting public status must not expose its conversations. A quiet server gets generic
FF flavor rather than a fabricated recent event. This vocabulary deliberately omits unknown
new topics; expand it through normal maintainer review when useful.

Giving Fri additional Discord read permissions does not expand their status-source,
learning, reply, command or reaction allowlists. Status-context access has a separate
privacy-only endpoint and grants no chat-write capability. The private
`/data/rotating-status.json` stores only public lines, daily reservations and delivery state.
It survives restart, and its reserved schedule must not be reset to force retries.

Statuses use Discord's [custom activity state](https://docs.discord.com/developers/events/gateway-events#activity-object).
The generator uses the existing subscription with [structured output](https://developers.openai.com/api/docs/guides/structured-outputs),
local output validation, and no tools. No API key is needed. `fri-health.json` includes a
status receipt; a sent gateway payload alone does not establish how every Discord client renders it.
Use `python -m gene.fri_eval --suite pronouns` for isolated pronoun/voice checks.

Set `FRI_CHANNEL_IDS` in both controller.env and fri.env to the explicit allowed
channels. Set `FRI_HISTORY_CHANNEL_IDS` and `FRI_LEARNING_ROLE_ID` in controller.env.
Set `FRI_CHAT_CHANNEL_ID` and `GENE_BOT_ID` in fri.env. Give Fri only the necessary
View Channel, Read Message History, and Send Messages permissions in those channels.
The application's Server Members Intent must be enabled for archive rotation's
bounded roster reads. The runtime keeps Members/Presence gateway subscriptions off.
Only current FF ADMEN IDs/nicknames enter rotation; historical author membership
is rechecked with bounded individual member lookups before reading history.

Fri's optional meme attention is separate from text learning and ambient help.
Set `FRI_MEME_CHANNEL_IDS` in controller.env to explicitly approved text channels,
then enable `meme_enabled` through the owner's versioned behavior settings or an
audited maintainer change. Give Fri View Channel, Read Message History and Add
Reactions on those channels. This does not expand ordinary chat, commands, history
learning, or development scope. It requires no upstream Red changes.

The default is a 30-minute check of up to 20 messages from the last 24 hours,
inspecting at most two unseen eligible posts per pass and 24 per rolling day.
Luna/medium receives the caption, up to two small still images, Fri's base voice
and current owner voice preferences. No other members' histories or personal
profiles enter this task. Its only choices are quiet or a bounded emoji palette.
It first decides whether it enjoys the joke, then chooses a contextual callback
(for example, 💩 for a poop punchline), without defaulting to laughter faces or
reacting just because an object has a matching emoji.
Quiet is normal; only a high-confidence matching joke can get a reaction, at most
one per hour and four per rolling day. These are ceilings, never targets.
`meme_model`, `meme_effort`, `meme_interval_minutes`, `meme_daily_checks`, and
`meme_daily_reactions` are visible and owner-adjustable with the normal preview/Apply
flow. Channels and permissions remain maintainer-only configuration.

Only actual PNG/JPEG/WebP attachments or Discord-proxied still-image embeds are
loaded, in memory, up to 4 MiB each. The [image input format](https://developers.openai.com/api/docs/guides/images-vision)
uses the existing subscription OAuth connection; no separate API key is used.
CDN requests have no auth headers and follow no redirects. GIFs, videos, spoilers,
oversized files and external link-only posts are skipped, rather than judging a
video from its thumbnail. Images/captions are untrusted evidence with no tools,
commands, settings or approval powers. Current membership, opt-out, source edits,
channel access and runtime settings are checked again before reacting.

`/data/meme-attention.json` keeps private, atomic message-ID receipts and reserved
budgets across restarts. A reserved or ambiguous send is never retried. It retains
no captions, images, model observations or member profiles; old IDs expire after
three days. Usage appears separately as "meme checks." The health file and owner
DM context include actual check/image/reaction times, not invented observations.
Other conversations only receive status for their own channel. A successful
model check does not prove a Discord reaction was delivered; only a completed
Discord call records `reacted`.

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
receipt. Fri keeps up to two useful verified inline links, with no numbered source
footer. The last three public research receipts (query, answer, up to eight source
URLs, and checked time) stay in the requesting member’s conversation for 24 hours.
A later source question can retrieve them without searching again. These are public
evidence, never hidden reasoning. Sources are dated, and opt-out, clear, and erasure
remove them and invalidate in-flight saves. Explicit source requests may show a
bounded list; unusually long lists indicate how many links remain. Failures do not
produce a searched answer. A webpage cannot authorize an action or a feature job.
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
owner-adjustable within validated bounds. Channels rotate each pass. Once the
initial backward scan finishes, a separate forward cursor checks new messages in
chronological batches. Full batches advance only through the newest examined
message, so later posts remain queued for another pass. Empty/short successful
scans return unused reservations. Failed passes preserve their cursor and reserved
allowance. Cursors and the scan allowance survive restarts. Legacy completed
cursors resume from enablement, bounded by the seven-day retention floor.

A trusted maintainer can set a versioned temporary learning window with
`AgentStore.start_learning_window(minutes=30, hours=24, version=..., actor=...)`.
This uses the current settings revision, records an audit event, and makes the next
pass immediately eligible. It does not reset history, quotas, or enablement time.
During the window, the remaining daily scan allowance is spread across scheduled
passes (still at most 30 messages per pass). Expiry automatically restores the
normal cadence; an owner interval change also supersedes the temporary window.
The active window is visible in settings, learning overviews and reports. This is
a local maintainer operation, not a new Discord or model-controlled endpoint.
`fri-health.json` separately reports heartbeat task state and last tick timestamps;
a Discord connection alone does not establish that the learning loop is running.

Every history pass queues a compact owner DM: who/channel, search term, outcome,
brief findings, rotation progress and next check. Empty or previously checked
results explicitly say there are no new notes. **Details** opens the full
record in the same message, including date limits, exact saved disclosures,
model/effort, measured usage and scan allowance. **Summary**, **Back**, and **Next**
navigate without extra messages or attachments. The buttons survive restarts;
only the configured owner with current server membership can open them.
Presentation adds no model call. Old reports without summary metadata still
open under Details, and cleared/expired reports explain that they are unavailable.
No eligible source means no model call. An undelivered report pauses further
history learning. A crashed claim reserves its original allowance and produces
an interruption report rather than replaying indefinitely. Reports are retained
until delivered and seven days old; detail buttons expire seven days after the
pass. Privacy erasure deletes the report and its companion presentation metadata.
Discord delivery can be repeated after a crash
between sending and acknowledgement; the batch identifier makes this visible.

### Autonomous archive exploration

With `exploration_enabled=true`, learning passes actively investigate existing
human conversations instead of waiting for new posts. `exploration_days=365`
sets the owner-approved one-year boundary independently of the seven-day recent
context window. The channel allowlist, FF ADMEN eligibility, opt-outs, per-pass
reservation, daily allowance and report delivery gate still apply. No old Fri/bot
replies, private threads, DMs, or images enter this learning path.

Each pass refreshes the complete current FF ADMEN roster through Discord's
[List Guild Members endpoint](https://docs.discord.com/developers/resources/guild#list-guild-members).
This requires the application's Guild Members privileged intent to be enabled;
a failed/incomplete roster never falls back to a partial gateway cache. The
controller chooses a member from a persistent round shared across learning channels.
Every currently eligible, opted-in member gets a turn before anyone repeats.
Existing successfully read member history counts once toward the first round.
New role members join the current round; departed members and opt-outs do not block it.

A member with no previous readable history gets an author-only search, without
guessing a keyword or spending a planning call. Later passes use the summary model
(initially Luna/medium) to choose a short optional query and purpose for the assigned
member. The planner sees their own bounded, dated excerpts, query progress and current
nickname. It favors natural wording such as watched/playing or evidenced titles;
generic category labels and exhausted keyword queries fall back to broad conversation.
The controller binds the member and chooses a channel with unread history before the harness
uses Discord's documented [Search Guild Messages endpoint](https://docs.discord.com/developers/resources/message#search-guild-messages),
with one fixed approved channel, author/type filters and a hard date/result limit.
One plan, one search (at most one short indexing retry), and one summary are allowed
per pass; no general tools, shell, web search or administrative actions are offered.
Planner usage appears separately as `exploration_plan` when a planning call is needed.

Searches retain a cursor per channel/member/term/depth. Short result pages do not
mean exhaustion; an empty fully indexed page completes that query. Checked message
IDs are retained across searches and restarts, so overlaps are not summarized or
saved again. Failed/incomplete indexing or inference never advances coverage.
Reports show the focus, purpose, range, duplicate count, new historical notes and
round/turn count. A successful empty search advances the turn and tries the member's
other approved channel on a later round; it is explicitly reported as checked, not
learned. Failed passes do not finish the turn. A fully exhausted member uses no
additional model/search call during that day's remaining turns. An exhausted broad
query becomes eligible for a fresh head check after 24 hours, retaining checked IDs
so old matches do not generate duplicate observations.

Small per-member notes retain exact source excerpts, author IDs, source dates and
harmless topic labels. These describe historical discussion, not present-day
preferences or inferred personality. At most eight source messages per member and
channel are retained, expiring 90 days after learning. They are supplied only for
the current speaker in the same FF channel, or through a scoped private owner
overview. They never replace the current channel summary or enter Lab/DM chat
context. Memory opt-out erases their notes, member metadata and checked IDs, clears
derived query terms, and invalidates an in-flight archive pass. Coverage metadata
contains no raw message bodies; selected source excerpts remain private local data.

`PLANNER` and `SUMMARIZER` in `src/gene/fri_explore.py` define this task's prompts.
The owner can guide future exploration through versioned `learning_notes` previews
without changing Fri's conversational voice or any authority boundary.

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
status. With the owner-authorized repair policy enabled, a proposed repair may enter
isolated verification. Otherwise it uses stored-request owner review. The controller
still binds every release to an exact SHA. No shell or Docker credentials are exposed
to Fri's conversation model.


### Reply checks

Off by default. The owner turns them on through the normal settings preview
(`reply_check_enabled`); `reply_check_private` adds DMs and private panel conversations,
`reply_check_minutes` (10–720, default 30) and `reply_check_daily_pairs` (10–400, default 120)
bound cost, and `reply_check_model`/`reply_check_effort` default to Luna/low. The controller
advertises the `reply-checks` capability; Fri only runs checks against a controller that does.

The controller offers Fri pairs from saved turns in the main server: Fri's verbatim reply, then the
same member's next turn in that conversation within 15 minutes. Both turns must come after reply checks
were last switched on (turning either `enabled` or `reply_check_enabled` back on moves that boundary and
drops any batch claimed before it).
Only conversations Fri may chat in right now qualify: a public conversation in a channel listed in
`FRI_CHANNEL_IDS` (turns don't record a thread's parent, so threads under a configured channel are
skipped) or, with `reply_check_private`, a private one while the workshop is configured. Reactions,
breadcrumbs, unattributed turns and members whose privacy revision moved are never offered.
Turns are walked in `(at, id)` order, so equal timestamps are neither skipped nor repeated. Each message
is cut to 600 characters with Discord mentions, channel and role links, emoji ids, message links and long
numbers replaced by neutral placeholders, and the offered pairs carry no member, channel or message
identifiers. Fri claims a batch only once its background model slot is free.

Fri's model returns only clear cases of a member being unhappy with the reply (a correction, a re-ask,
an ignored request, a failed action, frustration aimed at Fri), as four labels from fixed lists:
category, severity, topic and suggested fix. No model text is accepted or stored. The controller
rereads both turns (matching id, time, member and conversation, so a reused id doesn't stand in) and
applies the current settings and channel scope before keeping a finding: a pruned or erased turn,
a privacy change since the claim, checks or `enabled` turned off, or private checks turned off for a
private pair drops it. A privacy change also removes the member's pairs from a pending batch, and a
batch Fri abandons is settled as interrupted by housekeeping after ten minutes. A failed batch is
retried as the same batch twice, while newer turns wait, and is then skipped; a retry the allowance
cuts short keeps the rest of the batch and its attempt count. Every model call counts
against the daily allowance, retries included; nothing is refunded.

Findings live 30 days in `fri_reply_findings` with the member and provenance revisions, so any
privacy change by the member or anyone the pair drew on deletes them, and reads re-check the fence.
Expiry runs with the controller health check, whether or not checks are on. The owner can ask Fri for
a summary in DMs (“show reply checks”). Maintainers read the anonymized digest on the host without
starting anything:

```bash
sudo /srv/gene/venv/bin/python -m gene.fri_reply_checks export /srv/gene/data/controller/gene.sqlite3 7
```

It prints exact totals by category, severity and space, and recurring themes (category, topic, fix)
ranked by severity-weighted count with the last day seen. Before rolling the controller back to a
release without reply checks, stop it and run `python -m gene.fri_reply_checks purge <database>`:
older releases can't erase findings, and purge also removes the `reply_check_*` settings and state so
Fri stops asking for checks.

## Conversational Gene controls

Fri can use `play`, `pause`, `resume`, `skip`, `stop`, `volume` (0–100), `queue`,
`now_playing`, `status`, and `version` when Gene reports them available. Ordinary
existing functions do not need owner development approval. Commands outside this
audited catalog, including Red administration, configuration, shell, and deployment,
are not exposed to the model. New Discord feature/fix requests retain both approval gates.

Use “fri play Sad Machine by Porter Robinson”, “fri pause”, or “what’s queued?”.
A clear request to play an unidentified song may use one public web lookup followed
by one play action. Examples and capability questions do not execute. Every action
must originate in a fresh, verified Discord message. Gene re-fetches public messages,
checks current membership and text-channel permissions, and invokes the existing Red
command pipeline as that member. Voice/DJ checks, roles, cooldowns, hooks and the
existing audio download/cache/playback path still apply. Queue/now-playing return a
bounded read of the same player after Red access checks, without opening reaction menus.

Public actions stay in their configured source channel. Verified member DMs use a
readable approved FF channel, preferring the existing workshop. Joining the appropriate
FF voice channel is still required by Red. Lab chat cannot control FF, and panel-modal
conversations do not execute commands. The model cannot select the server, member,
source, destination channel, or arbitrary Red command string.

The controller stores a short-lived command receipt, polls through the separate runtime
credential, and claims each Discord message at most once. Unclaimed requests expire in
30 seconds; execution must start within 45 seconds. Claims are never replayed after
restart or uncertain delivery. Fri waits at most two minutes for a receipt, then reports uncertainty. The runtime
lets Red finish under its own timeouts and cleanup, and claims no further command
until it finishes. This avoids cancelling a download while Red holds its queue lock.
Clearing or forgetting chat redacts the request and cancels pending work; an action
already executed cannot be undone. Receipts expire after 24 hours. Fri reports the
actual result, distinguishing rejection, votes, enqueueing and confirmed completion.

`gene-health.json` reports `controls_running`; `/controls/catalog` (Fri credential)
is fresh for 20 seconds after the last runtime poll. Deployment checks both readiness
and the offered command catalog. `python -m gene.fri_eval --suite controls` tests model
choices with fictional context and inert receipts: it never submits a Gene command,
writes a member conversation, or sends a Discord message. Usage is recorded as evaluation.

### Selected-track preparation and Fri request previews

Gene's trusted runtime adapter prepares only the selected YouTube file before sending
Lavalink's play operation. This avoids its ten-second stuck detector expiring during a
cold download. The Mac remains the temporary download relay; decoding, cached files
and voice transmission remain on Hostinger. `/prepare` uses the existing authenticated
resolver, a 100-second client bound and a three-minute cache handoff pin. Stop, skip,
disconnect, pause and seek still apply while preparation is pending. A failed preparation
uses Red's existing track-error handling; it never sends a play operation. The adapter
dispatches a Lavalink track exception through red-lavalink, which parses its severity as
`ExceptionSeverity` (uppercase values only). Red's Audio cog then posts the message as a
"Track Error" embed in the command's channel and skips to the next queued track. Red and
Lavalink's installed package files remain unchanged.

A resolver refusal names its reason only as a fixed code in the `X-Gene-Refusal` header:
`too_long` (over 30 minutes), `too_large` (over 64 MiB) or `unsupported` (live or not an
ordinary public stream). Hostinger's resolver forwards only a known code from the Mac relay,
never the relay's text, and the runtime maps it to a fixed member line. Any other failure
shows the generic line. The three parts deploy separately: a feature release updates only
the runtime, so the specific reason needs the resolver image (`GENE_AUDIO_RESOLVER_SHA`) and
the Mac relay to run a release that includes it. Until then members see the generic line.

Set `GENE_AUDIO_PREPARE_URL=http://gene-audio:2334` and `GENE_AUDIO_PREPARE_TOKEN` in
`runtime.env`. The latter is the resolver service credential, never a Discord token.
The resolver must bind `0.0.0.0` inside its Docker network namespace; do not publish
port 2334 on the host. Keep its token and namespace isolated. Build the resolver image
from the same tested SHA and pin it with `GENE_AUDIO_RESOLVER_SHA` in `audio-release.env`.
`GENE_AUDIO_SHA` continues to pin the unchanged audio node. Roll back the resolver
image/environment alongside the bot runtime.

Set `GENE_COMMAND_CHANNEL_ID` in controller configuration to the approved bot commands
channel. Fri sends member-DM controls there, with member/channel access rechecked.
Public controls stay in their original channel. For verified DM commands, a normal
Red before-invoke hook updates an existing player's notification destination after
permission checks pass. The development workshop is never an implicit DM fallback.
Red registers additive before-invoke hooks. Register only this adapter's callback and
remove it with `remove_before_invoke_hook` on close. Never call Red's `_before_invoke`
dispatcher from a registered hook: that recursively schedules itself for every command
and can exhaust the container's memory. The offline command tests use the installed
Red dispatcher as well as its command/permission pipeline.

### Silent Gene failures

Gene publishes a bounded runtime heartbeat to `/runtime/health` using its runtime
credential every ten seconds, independently of the command runner. The controller
stores only the latest start time, release, guild connection and runner state, with a
controller timestamp. Fri can read it through the scoped `/controls/health` endpoint
even when Gene's command catalog has expired. Fresh heartbeats and polls do not prove
audible playback; stale polls can also mean a command is busy. A new runtime start
ends an interrupted command wait as uncertain without replaying it or changing a
previously confirmed receipt.

For a current failure report, Fri can choose `gene_diagnose`. Luna triages the report
and the member's readable receipts, then Sol at xhigh diagnoses when warranted.
Unconfirmed commands also take this path. It has no shell, logs, restart or deployment
access and must not invent an outage cause. A proposed repair survives the action
response as a real **Review request** preview when automatic verification is unavailable.
With the standing repair policy enabled, the harness can submit the checked report to
`/repairs`; an accepted receipt means verification was queued, not that a defect was
confirmed or fixed. Current same-member, same-conversation repair receipts are available
on follow-up diagnoses. Failed acknowledgements remain uncertain and never claim success.
Run `python -m gene.fri_eval --suite diagnosis` for isolated model choices with
fictional reports and inert evidence. It never writes member conversations or commands.

Fri's request draft has a **Review request** button in the current chat, including DMs.
The full preview offers **Submit & allow coding** only to the verified owner; other
members submit for owner review. The request is stored before the separate hashed
intake decision, and exact-commit release approval remains mandatory. Buttons check
the original member, Fri-authored message, server scope and current DM membership.
Automatic error requests identify Fri as reporter and Gene as error source. Older
reports are displayed correctly without rewriting their approved request hash.

Recent Gene command receipts are retained for at most 24 hours and scoped to the
same member and conversation, with current channel access rechecked. They distinguish
an earlier enqueue from a later denied attempt; they never prove audible playback.
Erasure removes these receipts. A proposal based on an older release gets an
informational notice with its saved diff, without an active approval button.

Attention still uses Luna. Investigations use `gpt-6.1-sol` at `xhigh`; the conversation
and coding defaults remain `gpt-6.1-sol` at `high`. The investigation prompt still gets
bounded error evidence and release state, with no added shell or log-reading authority.


## Verified automatic repairs

The owner has authorized `existing-features-v1` for repairs of existing Gene extension
behavior. This is a controller setting (`settings.repair_policy`), disabled by default
for new installations and enabled only by a maintainer. Discord messages and Fri voice
preferences cannot change it. Owner **Pause development** stops new claims and releases.
New features and changes to Fri's controller, dependencies, infrastructure, permissions
or deployment remain on the normal owner-approved path.

A current member report or a fresh error from Gene may queue a verification job. Luna
only triages; Sol/xhigh diagnoses. The authenticated harness checks the live source,
member/channel scope and erasure revision. The controller requires a fresh source and
binds the request hash to the current release and policy. Retrying the same Discord
event returns its existing job; distinct reports remain separate. At most three
automatic verification jobs may start in a rolling day;
excess reports retain the normal review path. Old requests are never retroactively approved.

The trusted worker then:

1. Runs the unchanged base suite without credentials or network.
2. Asks Sol/xhigh to create only one isolated regression test for an existing cog fault.
   Feature source remains unchanged. The test must actually fail on that base.
3. Runs a separate fresh Sol/xhigh verifier with a read-only checkout. It must confirm
   the existing contract and reachable defect from source, not just the request's claim.
4. Freezes the regression file hash. The usual coding worker can repair only existing
   cog function bodies. Imports, globals, signatures, commands, class structure,
   authorization helpers, new files and unrelated tests are protected. These structural
   checks supplement review; they do not prove arbitrary Python is safe.
5. Requires the frozen test, full suite and lint to pass in credential-free, networkless
   containers, then obtains a separate fresh Sol/xhigh review of the exact candidate SHA.
6. Sends the complete evidence to the controller. Only the controller can authorize that
   exact SHA under the standing policy. Revoked policy, an edited/erased request, a changed
   live base or evidence older than six hours routes the work back to owner review.

Review model invocations use the existing subscription and record usage for every attempt.
They receive no GitHub, Discord, controller or deployment credentials. They do not execute
candidate code; tests run separately. The wrapper publishes a reviewable branch; repair
branches are not automatically merged into main. See the [official OpenAI repair-loop
example](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex)
for the structured independent-review pattern; the release authority here is local code.

Unreproduced faults and protected changes stop for owner intake review. A tested candidate
that fails independent review remains a proposal requiring explicit owner SHA approval.
Automatic jobs use policy attribution, never a fabricated owner click. Owner DMs show
verification, progress and outcome without requiring an approval click on eligible repairs.
The same stored notices and workshop panel provide delivery recovery.

The deployer rechecks the restricted commit and frozen test hash, backs up state, switches
only the cog release, and requires a fresh guild connection and functioning control loop.
An unhealthy release restores the previous code. Health alone does not prove audible music
or every Discord UI path; the failing-then-passing functional regression provides the
feature evidence. Problems that need live credentials, external-service changes or cannot
be reproduced safely are deliberately escalated rather than guessed at.

### Telling the reporter

When a repair a member reported ships, the owner's deployed DM can offer **Tell reporter**.
Pressing it sends one fixed reply to the member's report, in its channel or DM, with mentions
off and no request text. The controller keeps a mark per job (`requester_notices`), so a
second press, a restart or a redelivered DM never replies twice. A reply Discord refused can be
tried again. One that may have posted (a timeout, a server error, a crash mid-send) is never
retried. Only deployed jobs that repair intake recorded for a member's report qualify, including ones the
owner finished after the automatic checks handed them over. Those now get the same status DMs
(approved, deploying, deployed) as other requests, since they carry no intake approval. An erased report, a rollback
deployed after the repair, any newer release now live, another release still going out, or an approved rollback
of this release still waiting for the deployer refuses the reply: "fixed now" has to describe what's running. Fri claims the mark only after its Discord reads, right before the send,
so the claim is the send-time check. Claiming and recording the outcome both carry the owner and
this server, and the controller refuses either one for anyone else. A Fri older than that (a
Fri-only rollback) sends only the lease on the outcome, which is still recorded.
Older controllers lack the table and the `requester-notices` capability, and Fri then shows no
button. A downgrade leaves the table in place, unread.
