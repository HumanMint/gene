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
6. Archive the initial approved commit's cogs into `releases/<sha>/cogs`, write
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

## Audio on the Mac

Java 21 is installed independently. Run `uv run --no-sync python scripts/install_audio.py`.
The installer pins Red's `3.7.13+red.5` Lavalink jar by SHA-256 and uses Red's matching
YouTube plugin configuration. Lavalink binds loopback port 2333; a separate TCP relay
binds the Mac's Tailscale address and accepts only the configured VPS source IP.
Lavalink additionally requires its generated password. No public interface is opened.

LaunchAgents are `local.dan.gene.audio` and `local.dan.gene.audio.proxy`; files and
logs live in ignored `.audio/`. Existing Cobalt ports, agents, and cookies are untouched.
The runtime sets Red's external-node settings directly without posting passwords in chat.
YouTube may still fail due to source changes or IP checks. Do not claim playback works
until it actually plays in a Discord voice channel. Mac offline means music is offline;
the bot, controller, memory, and development queue remain independent.

## Releases and recovery

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

### Approve and deploy

Each request produces a `codex/request-<id>` branch. The wrapper checks changed paths,
rejects symlinks and likely secrets, and runs tests/lint without credentials or network.
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
