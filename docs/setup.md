# Setup

## Prerequisites

Use Python 3.11, `uv`, Docker with Compose, Git and an authenticated GitHub CLI.
The current host installer expects a Linux/systemd machine at `/srv/gene` and must
be reviewed before running as root. It writes service files and creates a private
repository deploy key; it does not install Docker/Python or start bots.

Create a Discord **bot application**, never a user-account token. Enable Message
Content Intent for Gene. The bundled Gene gateway does not request Members or
Presence intents. Give only the configured server/channels the permissions needed
for text, reactions and voice Connect/Speak. Start in a separate test server.

## Prepare locally

```sh
git clone git@github.com:HumanMint/gene.git
cd gene
uv sync --frozen --extra red
uv run --no-sync pytest -q
uv run --no-sync ruff check .
uv run --no-sync python scripts/configure.py
```

The interactive configuration validates the bot identity and channel IDs and writes
an ignored `.env.local`. Keep its tokens private. If using a fork, set
`GENE_REPOSITORY` in the worker/deployer environment to that fork before enabling
coding; the current installer defaults to this project's private repository.

## Install Gene's runtime/controller

1. Securely transfer the reviewed checkout to `/srv/gene/source` and `.env.local`
   to `/srv/gene/upload.env`. Install Python 3.11, Docker/Compose and `uv` on the host.
2. Run `sudo python3 /srv/gene/source/scripts/install_host.py`. It splits service
   credentials and deletes the combined uploaded file. Existing installations should
   not rerun this bootstrap installer over their configuration.
3. From the source checkout, build `ops/Dockerfile` target `runtime`, tagged
   `gene-runtime:<full-reviewed-sha>`. Archive that same commit's `cogs/` into
   `/srv/gene/releases/<full-reviewed-sha>/cogs`. Keep that tree read-only to bots.
4. Create `/srv/gene/release.env` with `GENE_ROOT=/srv/gene`,
   `GENE_RELEASE_SHA=<full-reviewed-sha>` and
   `GENE_RUNTIME_IMAGE=gene-runtime:<full-reviewed-sha>`.
5. Initialize `/srv/gene/data/controller/gene.sqlite3` once with
   `gene.store.Store(path).bootstrap(full_sha)` using the reviewed runtime package.
   Keep controller and Red data owned by runtime UID/GID `10001:10001`.
6. Start only these services:

   ```sh
   sudo docker compose --project-name gene --env-file /srv/gene/release.env \
     -f /srv/gene/source/ops/compose.yml up -d controller red
   ```

7. Check container health, Gene's fresh heartbeat and the command catalog, then test
   `/gene menu` with a real member. Music also requires the compatible Lavalink/audio
   setup below. Keep `GENE_LAB_MODE=true` until deliberate server rollout.

Leave worker/deployer services stopped for a Gene-only installation. The controller
still reserves four distinct service credentials; unused credentials do not require
starting their services. Keep `GENE_SHOW_FRI=false` until the companion is configured.

## Music

The optional [red-audio-adapter](https://github.com/HumanMint/red-audio-adapter)
repository owns the Red-compatible Lavalink source, resolver/cache and private download
relay. Install and pin its audio services separately; Gene consumes the exact Python
adapter revision recorded in [audio-adapter.json](audio-adapter.json).

Follow [Gene audio integration](audio-adapter.md) and the adapter's portable setup guide.
Keep Gene's external audio host/port/password and prepare token private. Test actual
audible playback; a loaded title or queued track alone is insufficient.

## Optional Fri and coding workflow

Use the separate [Fri repository](https://github.com/HumanMint/fri) and its operations
guide. It requires this controller's `gene-fri/1` contract. Do not launch the legacy
Fri Compose profile for a new installation. Add only Fri's own bot/service credential
and existing subscription directory to that runtime.

For coding jobs, follow [initial worker setup](operations.md#initial-installation):
create the dedicated account/tools/login, repository-specific Git access and host
Python environment; build the worker image; then enable worker/deployer units.
Do not copy personal credentials into candidate/test containers. New jobs start paused.
Subscription registration for a new Fri owner must be verified separately; the project
cannot promise that every account can authorize the current self-hosted connection.

Controller, dependency and infrastructure updates remain maintainer operations.
Discord feature jobs are limited to cogs, feature tests and feature docs. Owner
approval and the existing-feature repair policy retain their separate boundaries.
