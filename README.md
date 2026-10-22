# Gene

Gene provides established Discord commands, music and meme highlights through
**upstream Red-DiscordBot 3.5.24**. The custom interface and integrations live in
separate cogs and adapters; Red's installed source is not forked or edited.

[Fri](https://github.com/HumanMint/fri) is the separate conversational companion.
Gene continues to host the trusted controller, coding worker and deployment authority.
Fri now owns its runtime, dependencies and releases in its own private repository.

## Features

- `/gene menu`: music and server tools with one private, reusable panel per member.
- Red's normal playback, queue, permissions, DJ checks and cooldowns.
- Meme voting/highlights with unique voters, self-vote exclusion and persistent IDs.
- A private controller with scoped service credentials and SQLite persistence.
- Owner-reviewed feature requests, isolated development, exact-commit releases,
  backups and code rollback. Verified automatic repairs stay within existing features.
- Optional Hostinger audio services and a lightweight home-network download relay.
  The home machine downloads and transfers files; playback and the bounded week-long
  media cache stay on the server. The relay deletes temporary files after transfers.

For music, join a voice channel before queuing a track. Fri can also invoke Gene's
existing non-admin commands on a member's behalf, with the same permission checks.
Discord Lab cannot control another server's music.

## Choose your setup

**Gene without Fri:** run Red and the controller. Chat, background learning and the
Fri subscription connection are not required for music or meme tools. Leave the
coding worker/deployer stopped unless you want the development workflow.

**Gene with Fri:** add the independent Fri runtime and its distinct Discord
application. The first split retains Fri's settings, context and delivery records
in Gene's controller so existing approvals and privacy transactions remain intact.

Start with [setup](docs/setup.md). [Operations](docs/operations.md) describes the
current private host, audio pipeline, worker setup and recovery. The project's
scripts target Linux/systemd and `/srv/gene`; adapt those paths deliberately on
other hosts. Neither repository is a general hosted bot service.

## Development

Use Python 3.11 and `uv`:

```sh
uv sync --frozen --extra red
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

- `cogs/`: live Red extensions and panels.
- `src/gene/`: trusted controller, worker, deployer and audio adapters.
- `audio-plugin/`: external Lavalink source and playback probes.
- `ops/`, `scripts/`: deployment and local/host configuration.
- `tests/`: state, authorization, failure recovery and functional checks.

The feature worker may change only `cogs/`, `tests/features/` and `docs/features/`.
It cannot change its own approvals, controller, dependencies or deployment code.
New Discord feature requests still need owner intake approval and exact-commit
release approval. The controller's existing-feature repair policy requires a proven
baseline failure, frozen regression and independent reviews. Discord messages,
imported history and reactions never grant authority.

Changes requested directly by the owner in the maintainer chat can be tested,
reviewed and deployed without a second approval. Tests/review run without production
credentials or network access. User-facing success must be verified separately from
a healthy process or a queued track.

## Fri migration boundary

The current protocol is `gene-fri/1`, exposed only to Fri's credential at
`/fri/contract`. The new client checks compatibility before starting and includes the
version on subsequent calls. The Gene feature deployer only restarts Red; Fri uses a
separate release file and a maintainer-only coordinator, `gene.fri_release`.

Some `fri_*` modules remain active controller contracts and persistence. The old
companion implementation is retained temporarily as a compatibility/rollback
baseline; future companion features belong in the Fri repository. See
[ownership and migration](docs/fri-migration.md) for the precise boundary.

Tokens, auth sessions, raw history, databases and private context stay in ignored
private paths. `gene forgetme true` erases stored user context and cancels pending
proposals; existing Discord/GitHub messages, code and backups are separate records.
