# Batched maintainer release

`batch-release.sh` installs a reviewed Gene main (control plane plus cogs) and then a reviewed Fri main on the host, one phase at a time. Run it as root:

```sh
batch-release.sh <phase> <full gene sha G> <full fri sha F>
```

It never prints secrets: env files are passed to Docker, copied or compared, never echoed. It never starts the legacy `fri` service in Gene's compose or `gene-fri-1`.

The live release it upgrades from defaults to Gene `f667001` and Fri `6a1d410`. Override with `OLD_G` / `OLD_F`.

## Before running
1. Fetch both repos. Confirm G and F are the reviewed mains, and that CI is green on both.
2. Read F's `docs/integration-pair.json` and get `verified_controller_commit`. It must be an ancestor of G.
3. Upload the inputs to `/srv/gene/staging/batch-$G/` (root, 0700):
   - `git archive --format=tar` of each commit, as `gene.tar` and `fri.tar`
   - `sha256sum gene.tar > gene.tar.sha256`, and the same for `fri.tar`
4. Upload the script itself to a root-owned path.

## Phases
| Phase | Member impact | What it does |
|---|---|---|
| `baseline` | none | Requires a healthy live pair at OLD_G / OLD_F: settings, pins, images, labels, fresh heartbeats, controls, connections. Records it, plus the exact running image IDs, in `baseline.json` and `manifest.json`. |
| `stage` | none | Disk admission on every filesystem it will use. Checks the archive hashes and extracts into fresh dirs. Refuses dependency or ops diffs (`ALLOW_OPS_DIFF=1` after review). Prepares the immutable cog tree and records its hash. Builds the labelled images and records their IDs. Runs isolated import checks, the controller contract on synthetic state, a check that Fri accepts that contract, and a test of the audio player snapshot. |
| `rehearse` | none | Copies the live DB twice into a private dir that is always removed. Migrates one copy with G, starts the exact rollback image on it, and writes a request with old code. Migrates the other, runs the reply-check purge (and `gene.profile_downgrade` when G has it), then starts the rollback image on it. |
| `drain` | development pauses | The fence: pause through the store (audited), wait for building/deploying work and `deploy-result.json` to clear, stop gene-worker and gene-deployer, recheck. |
| `gene` | Red ~1 min, controller seconds | Re-runs the fence and the cog and disk checks, then waits until the audio node reports no players (Lavalink stats). Next it backs up into a unique dir and renames the cogs into place. It takes a fresh player snapshot, stops Red, swaps the source, and pins `release.env` and the worker image. It starts controller and red, probes health against the baseline guild and presence, then records `active_sha` / `previous_sha` plus an audit row in one guarded transaction. Any failure runs `rollback-gene`. |
| `fri` | Fri ~1 min | `gene.fri_release F` (G's coordinator), then a probe. On failure it prints the reconciliation state. The workers stay stopped. |
| `evals` | none | Live `requests`, `sass` and `controls` suites inside `fri-runtime-1`. Never rolls back by itself. |
| `verify-off` | none | Reply checks are off and have no runs. |
| `resume` | development resumes | Full consistency check: images, pins, records, settings, health and idle. Then starts the units, unpauses only if this release paused, records an audit row, and writes `maintainer-G-result.json`. |
| `cleanup` | none | Removes the staged sources and tars. Keeps the journal, manifest, baseline, evals, backup and images. |
| `rollback-gene` | yes | Reconciles from actual state, so it is safe to repeat at any point. Steps: fence, put Fri back to OLD_F if it moved, stop the new containers and downgrade the DB with G's tools, restore pins and source, start the exact old images, probe, reverse the settings (a no-op if already old), then verify. It never restores a DB snapshot; that stays an operator decision. |
| `resume-old` | development resumes | After a rollback, once the old pair verifies. |
| `status` | none | Read-only state plus the journal. |

## Compatibility boundary
OLD_G already includes turn provenance and the profile migrations of `f667001`. The controller changes after it are:
- an additive `jobs.attributed` column and index
- reply-check tables and settings, which `gene.fri_reply_checks purge` removes
- whatever G adds, which `rehearse` exercises on a copy of the live DB

Don't run `gene.provenance_downgrade` for this rollback: it would discard member continuity that the old release still supports.

## Offline tests
`FRI_REPO=<fri checkout> ops/maintainer/test/run.sh` runs the script against a mock host (stub docker, systemctl and curl, and a real controller DB). The cases:
- the full release
- a failed health check with auto-rollback, a repeated rollback, then resume-old
- a refusal mid-switch while audio plays
- a failed Fri release followed by rollback
- a rollback after resume
- a crash right after a mutation, followed by rollback

## After the release
- The owner DMs Fri "fri health" and should see the watch running, the controller up at G, and a fresh Gene heartbeat.
- The owner DMs Fri "show reply checks" and should see that they're off.
- The three older review jobs stay in review with stale notices. Completing them needs a rebuild.
