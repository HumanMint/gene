# Operations

## Hosts and credentials

The trusted checkout and service files live under `/srv/gene` on Hostinger. Docker
containers run as UID 10001. The small trusted worker/deployer wrappers run as root
to orchestrate isolated containers, backups, and release mounts. They are part of
the control plane, never editable by automated feature jobs. Existing Hostinger
applications and OpenClaw are separate and are not managed by this project.

- `source/`: installed control plane; update deliberately as a maintainer.
- `secrets/{controller,runtime,worker,deployer}.env`: separate service credentials.
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
   Development starts paused. `gene pause false` resumes new jobs.

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
Discord Lab connection, and invisible presence. Restore the previous release on failure;
resume the worker/deployer only after the new or restored release is healthy. Record
the deployment as chat-authorized maintainer work, without fabricating a Discord approval.

This is a trusted operator workflow. The controller's approval state machine, worker
scope, and credentials stay unchanged. Discord-originated requests, fixes, and repairs
always follow the exact-SHA approval workflow below; a Discord message cannot claim
this maintainer authorization, even if it quotes these instructions.

### Worker model

The initial model is `gpt-6-astra`. Owner commands `gene model`, `gene models`,
`gene models true`, and `gene model <model-id> [effort]` show the selection, list the
account catalog, request a refresh, and select an available model/effort. Refreshes
use the Codex app-server `model/list` interface without creating an inference turn.
They run between jobs and remain available while development is paused. A failed
refresh keeps the old catalog and displays the error. It never silently substitutes
another model. The selected ID and effort are fixed when each job starts and shown
in `gene status <id>`; changing the selection affects subsequent jobs.

The CLI is pinned; a protocol change may require a deliberate CLI/worker-image update.
Catalog discovery does not grant model access or bypass account rate limits.

### Discord requests: approve and deploy

New Discord requests enter `awaiting_owner`. They cannot reach the coding worker until
the owner reviews the complete stored request and selects **Allow coding**. The controller
binds that decision to a hash of the request and its source; changed requests and stale
buttons fail closed. Existing queued requests without intake approval are held during
migration. Intake approval never authorizes a live release.

Gene sends the configured owner a DM with an untrusted-text preview and the full request
as a text attachment. Once a tested proposal is ready, it edits that same DM with the
code link and complete SHA for release approval. Notifications use persisted message IDs,
short delivery leases, and ten-minute retries when DMs are unavailable. The lab's
**Requests** panel remains a fallback. Delivery never grants approval. Only owner clicks
on Gene-authored review components are admitted from DMs; ordinary DM messages, commands,
other users' clicks, forged references, and stale approvals remain rejected.

An allowed request produces a `codex/request-<id>` branch. The wrapper checks changed paths,
rejects symlinks and likely secrets, and runs tests/lint without credentials or network.
Failed tests or lint are returned to Astra for up to two repairs in the same worktree.
Every attempt records its usage. Scope or credential violations stop the job immediately;
three unsuccessful coding attempts leave a failed request and never publish a branch.
Inspect the GitHub diff from `gene status`, then approve its full 40-character SHA.
An intervening deployment invalidates older proposals. Re-submit/rebuild those requests
against the new release. Approving another SHA or approving as a member fails.

The coordinator snapshots state, stops Red, mounts the approved code, and requires a
fresh heartbeat with the expected SHA and a live connection to the configured guild.
If that fails, it restores the preceding code and checks it again. Code rollback does
not undo state; ordinary feature changes must remain backward compatible. State restore
is a deliberate operator action from the stopped Red backup if needed.

`gene pause true` stops new work; an already-running job/release is allowed to finish.
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

Normal memory is explicit owner-approved facts plus operational request/release records.
Ordinary chat is not collected. `gene memories` and `gene forget` expose the saved facts.
`gene forgetme true` removes the caller's request text, links and approved facts from
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
