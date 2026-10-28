# Quickstart: run Gene on your machine

This runs Gene's controller locally, for development or as the backend for a local
[Fri](https://github.com/HumanMint/fri). Gene's own Red bot is an optional add-on, and
music is an advanced one. Everything stays in this checkout: configuration in the
git-ignored `.env.local`, data under `.state/`. Installing on a server is a separate
guide: [host installation](setup.md).

## What you need

| To | You need |
| --- | --- |
| Run the controller | `git` and [uv](https://docs.astral.sh/uv/getting-started/installation/). uv fetches Python 3.11 if you don't have it. No Discord account or tokens. |
| Pair it with a local Fri | A Discord server you own for testing, plus what Fri itself needs: its own bot application and model access ([Fri's README](https://github.com/HumanMint/fri#readme)). |
| Run Gene's Red bot (optional) | A second Discord bot application, for Gene. |
| Play music (optional) | A Red-compatible Lavalink node. See [music](#music-advanced). |

## 1. Set up and check

```sh
git clone https://github.com/HumanMint/gene.git
cd gene
uv sync --frozen
uv run python scripts/dev.py init
```

`init` copies [`.env.example`](../.env.example) to `.env.local` (mode 600) and lists the four
service tokens it needs, one per service that talks to the controller. It never changes a
file that already exists. Give each token its own value: run this once per token and paste
what it prints after that token's `=` in `.env.local`:

```sh
uv run python scripts/dev.py token
```

The script never writes a token into a file itself, so `.env.local` stays the one place they
live. It's git-ignored in this repository; keep it that way. The comments in the file explain
each value. Then:

```sh
uv run python scripts/dev.py check
```

`check` validates the file and starts the controller in-process on a throwaway database,
without opening a port or touching the network. It never prints a value:

```
Checking /path/to/gene/.env.local. Nothing leaves this machine and no value is printed.
  ok     service tokens: four distinct, 32+ characters
  ok     controller dry run: /health ok, gene-fri/1 with 19 capabilities (10 required), /status ok
  ok     database: /path/to/gene/.state/gene.sqlite3
  todo   pair with Fri: set GENE_GUILD_ID, GENE_OWNER_ID, GENE_WORKSHOP_CHANNEL_ID
  todo   Red (optional): needs DISCORD_TOKEN, GENE_GUILD_ID, GENE_OWNER_ID, the red extra (uv sync --frozen --extra red)
check: the controller is ready. Start it with `uv run python scripts/dev.py controller`.
```

## 2. Run the controller

```sh
uv run python scripts/dev.py controller
```

It listens on `http://127.0.0.1:8787` only (`--port` changes the port). From another
terminal:

```sh
curl http://127.0.0.1:8787/health
# {"ok":true,"service":"gene-controller"}
```

Every other route needs a service token. The database is `.state/gene.sqlite3`; stop the
controller and delete `.state/` to start from scratch. The controller reads `.env.local`
when it starts, so restart it after editing the file. Don't expose it beyond your
machine: it holds conversations and memory.

## 3. Pair it with a local Fri

Fri reads three Discord IDs at startup, and the controller scopes everything to them: the
server, the owner who approves things, and the channel Fri chats in. Fri must also present
the controller's `GENE_FRI_TOKEN`.

1. In Discord, create a server for testing (**+** > **Create My Own**). Turn on
   **Developer Mode** (User Settings > Advanced) so you can copy IDs.
2. Fill these in `.env.local`:

   | Key | Where to find it |
   | --- | --- |
   | `GENE_GUILD_ID` | right-click the server icon > **Copy Server ID** |
   | `GENE_OWNER_ID` | right-click your own name > **Copy User ID** (you're the owner) |
   | `GENE_WORKSHOP_CHANNEL_ID` | right-click the text channel where Fri should chat > **Copy Channel ID** |

   Optionally list more chat channels in `FRI_CHANNEL_IDS`, comma separated.
3. Run `uv run python scripts/dev.py check` until it reports `Fri pairing` as ok, then
   restart the controller.
4. Clone Fri next to this checkout, and print the values Fri shares with the controller:

   ```sh
   git clone https://github.com/HumanMint/fri.git ../fri
   uv run python scripts/dev.py fri-env
   ```

   `fri-env` prints these lines for Fri's git-ignored `.env.local`, which Fri's setup creates.
   It never prints the token: copy `GENE_FRI_TOKEN` from this `.env.local` yourself.

   | Key in Fri's env | Value |
   | --- | --- |
   | `GENE_CONTROLLER_URL` | `http://127.0.0.1:8787` (`--port` changes it) |
   | `GENE_FRI_TOKEN` | the same as this `.env.local` |
   | `GENE_GUILD_ID`, `GENE_OWNER_ID`, `GENE_WORKSHOP_CHANNEL_ID` | the same as this `.env.local` |

5. Continue with [Fri's quickstart](https://github.com/HumanMint/fri#readme) in `../fri`.
   Its setup creates Fri's `.env.local`; paste the lines from `fri-env` into it.

When Fri starts it reads the controller's `/fri/contract` and refuses to run if the
contract or token doesn't match. Fri also needs its own Discord bot token and model
access; that part is in Fri's guide.

## 4. Add Gene's Red bot (optional)

Gene's own bot runs Red with this repository's cogs: the `/gene menu` panel, Red's
commands and meme highlights. It also runs the Gene commands Fri queues for members.

1. Install Red: `uv sync --frozen --extra red`.
2. In the [Discord Developer Portal](https://discord.com/developers/applications),
   create a new application for Gene (not Fri's). Under **Bot**, reset and copy the
   token, and turn on **Message Content Intent**. Leave Server Members and Presence off.
3. Invite it to your test server, with your application ID in place of the
   placeholder:

   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_APPLICATION_ID&scope=bot%20applications.commands&permissions=36785472
   ```

4. Put the bot token in `.env.local` as `DISCORD_TOKEN`. `GENE_GUILD_ID` and
   `GENE_OWNER_ID` from step 3 are also required. Set `GENE_PRESENCE=online` if you want
   to see the bot online; it's invisible by default.
5. With the controller running, start Red in another terminal:

   ```sh
   uv run python scripts/dev.py red
   ```

Red keeps its instance under `.state/red/`, not in your home directory. It answers only
in `GENE_GUILD_ID`. Try `/gene menu` or `!help` (`GENE_PREFIX` sets the prefix).

## Music (advanced)

Playback needs a Red-compatible Lavalink node (`GENE_AUDIO_HOST`, `GENE_AUDIO_PORT`,
`GENE_AUDIO_PASSWORD`). The repository builds one in `ops/Dockerfile.audio`; the
resolver, the cache and an optional download relay are covered in the
[operator runbook](operations.md). There's no scripted local path yet. Without those
settings Red runs without its audio cog.

## Tests and lint

The full suite needs the Red extra and runs without credentials or network access:

```sh
uv sync --frozen --extra red
uv run --no-sync pytest -q
uv run --no-sync ruff check .
```

## Troubleshooting

- **`Four distinct service tokens of at least 32 characters are required`**: run
  `uv run python scripts/dev.py token` for each empty or repeated token and paste the values in.
- **Fri gets 401 from the controller**: Fri's `GENE_FRI_TOKEN` differs from this
  `.env.local`. Copy it again exactly.
- **Fri gets 403 "This server is not configured for Gene"**: the IDs differ between the
  two env files, or the controller wasn't restarted after you edited `.env.local`.
- **Port 8787 is busy**: `dev.py controller --port 8788`, and
  `dev.py fri-env --port 8788`.
- **Starting over**: stop everything and delete `.state/`. For new tokens, run `token` again
  for each and paste them in; then copy the new `GENE_FRI_TOKEN` into Fri's `.env.local`.
