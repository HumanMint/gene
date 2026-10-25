# Batched maintainer release

`batch-release.sh` installs a reviewed Gene main (control plane plus cogs) and then a reviewed Fri main on the host, one phase at a time. Run it as root:

```sh
batch-release.sh <phase> <full gene sha G> <full fri sha F>
```

Only one batch command runs at a time on the host: each takes `/srv/gene/batch-release.lock` (except `status`), and every command it starts inherits the lock. So if a batch shell is killed, anything it left running keeps the lock until it ends, and a rollback refuses rather than racing it. `fuser -v /srv/gene/batch-release.lock` shows the holder.

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
| `baseline` | none | Requires the tools it uses, a controller DB whose directory and journal files share its owner, and a healthy live pair at OLD_G / OLD_F: settings, pins, images, labels, an unmarked source tree, fresh heartbeats with a presence, controls, connections. Records it, plus the exact running image IDs, in `baseline.json` and `manifest.json`. |
| `stage` | none | Walks a probe directory through staging, `releases/`, the source tree's parent and the backups dir by plain `rename(2)`, and refuses if any hop fails. A matching device number isn't enough, since a bind mount shares one and still refuses renames. It also reads the kernel's mount table and refuses a mount point at or under the source tree or either staged tree, since a bind mount there looks like a plain directory. Every tree move in the switch and its rollback is then a single rename that fails rather than copying. Disk admission on every filesystem it will write: Docker, staging, both backup dirs, `releases/` and the source tree. Checks the archive hashes and extracts into fresh dirs. Refuses dependency or ops diffs (`ALLOW_OPS_DIFF=1` after review). Prepares the immutable cog tree and records its hash. Builds the labelled images and records their IDs. Runs isolated import checks, the controller contract on synthetic state, a check that Fri accepts that contract, and a test of the audio player snapshot. |
| `rehearse` | none | Copies the live DB twice into a private dir that is always removed. Migrates one copy with G, starts the exact rollback image on it, and writes a request with old code. Migrates the other, runs the reply-check purge and, when G has it, `gene.profile_downgrade` with the controller's env on a copy holding one profile removal, so a `controller.env` without the configured channels (which the downgrade needs to keep removals) stops here instead of halfway through a rollback. Whether G has a downgrade tool is asked of the image with a probe that answers yes or no, so a container that fails to start stops the phase rather than skipping the step, here and in the rollback. Then it starts the rollback image on it. |
| `drain` | development pauses | The fence: pause (audited), wait for building/deploying work and `deploy-result.json` to clear, stop gene-worker and gene-deployer, recheck. |
| `gene` | Red ~1 min, controller seconds | Re-runs the fence, verifies the old pair and the cog, rename and disk checks, then waits until the audio node reports zero players. Players left idle in voice are a stop: ask the owner. It verifies the old pair again and writes a complete backup into a unique dir: a DB copy, Red's data, and the four pin files with their modes kept. Only then does it journal the attempt. It renames the cogs into place, takes a fresh player snapshot, stops Red, swaps the source, and pins `release.env` and the worker image. It starts controller and red, probes health against the baseline guild and presence, then records `active_sha` / `previous_sha` plus an audit row in one guarded transaction. Any failure after the attempt is journaled runs `rollback-gene`. |
| `fri` | Fri ~1 min | `gene.fri_release F` (G's coordinator), then a probe. On failure it prints the state; `rollback-gene` puts the old Fri back from any partial state. The workers stay stopped. |
| `evals` | none | Live `requests`, `sass` and `controls` suites inside `fri-runtime-1`, then Fri's heartbeat stages (failing ones by exception class) and its rejected usage receipt count. Exits nonzero if any suite failed, after reporting all of them. Never rolls back by itself: that is a call to make with the owner. |
| `verify-off` | none | Reply checks are off and have no runs. |
| `resume` | development resumes | Full consistency check: images, pins, source, records, settings, health and idle. Then starts the units, unpauses only if this release holds the pause, records an audit row, and writes `maintainer-G-result.json`. Safe to repeat after a crash: the result file is written in full to a temporary of its own and then linked into place, and a repeat accepts its own result. A repeat after it finished checks the new release and running units again before it says so. |
| `cleanup` | none | Removes the staged sources and tars. Keeps the journal, manifest, baseline, evals, backup and images. |
| `rollback-gene` | yes | Reconciles from actual state, so it is safe to repeat at any point. It first checks that every live piece is from the recorded old or new release, and refuses without changing anything otherwise. Then it fences, takes `gene.fri_release`'s lock for the rest of the run, and checks again, since the fence may have waited for a deployment that landed a third release. Then: put the old Fri back, stop the new containers and downgrade the DB with G's tools, restore pins and source, start the exact old images, probe Gene, reverse the settings (a no-op if already old), probe Fri, verify. It never restores a DB snapshot; that stays an operator decision. Once it has started, `gene`, `fri` and `resume` refuse: a rollback that was interrupted is finished by repeating it, then `resume-old`. |
| `resume-old` | development resumes | After a rollback once the old pair verifies, or after a `gene` phase that stopped before switching. |
| `status` | none | Read-only state plus the journal. |

## How it touches state
- **Controller DB.** Every read and write runs as the DB's owner in a throwaway container of the old runtime image, with only the data dir mounted. So no root SQLite client leaves journal files the controller can't open, and pausing, recording and checking work while the controller is down. `gene.fri_release` still opens the DB as root. SQLite normally hands new journal files to the DB owner anyway, but every DB access first gives back any it didn't, so a crash in the middle of a Fri release can't lock the helpers out. The diagnostics go to stderr, so captured output stays clean. The read-only `baseline` and `status` only report them. Every state read that decides something, such as whether to take the pause, has to succeed.
- **Pause.** The journal line `intent:pause` is written before pausing and is the ownership record. Resume unpauses only while the latest pause line is this release's. A pause someone set before the release stays.
- **Fri on rollback.** The rollback holds `gene.fri_release`'s lock before it looks at Fri, so a coordinator that outlived a killed batch shell makes it stop instead of racing it. If the old Fri's pins, record and image are not all in place, it works through the switch backup. It restores `release.env` and `release.json` byte for byte, sets `fri_active_sha` with an audit row, and starts the old runtime. This covers a clean new Fri and an interrupted Fri release alike, and needs no healthy controller. Fri's health is probed once Gene is back.

## Compatibility boundary
OLD_G already includes turn provenance and the profile migrations of `f667001`. The controller changes after it are:
- an additive `jobs.attributed` column and index
- reply-check tables and settings, which `gene.fri_reply_checks purge` removes
- member profiles: `chat_turns.audience`, `personal_notes.origin` and `personal_notes.provenance`, added on controller start with no backfill. `gene.profile_downgrade` deletes profile notes, copies their removals to every configured and known channel, and keeps conversation notes and exchanges. It never migrates, and it reads the channels from `FRI_CHANNEL_IDS` and `FRI_HISTORY_CHANNEL_IDS` in `controller.env`
- whatever G adds, which `rehearse` exercises on a copy of the live DB

Don't run `gene.provenance_downgrade` for this rollback: it would discard member continuity that the old release still supports.

## Offline tests
`FRI_REPO=<fri checkout> ops/maintainer/test/run.sh [t_name ...]` runs the script against mock hosts: stub docker, systemctl and curl, and a controller DB created by the old code as uid 10001. Each `docker run` executes the real command with the image's code: an export of OLD_G, this checkout for G, and FRI_REPO for F. It runs as the requested user and refuses anything it does not model. Every phase's exit code and the resulting state are asserted. Scenarios:
- the full release, with a failing eval suite, then repeats of resume and cleanup
- a failed health check with auto-rollback, a repeated rollback, then resume-old
- players on the audio node for the whole window: no switch, then resume-old
- a player appearing mid-switch
- a failed Fri release that recovers itself, then rollback
- an interrupted Fri release, reconciled by rollback
- a rollback after resume, after a member removed a profile note, whose removal the downgrade keeps in the configured channel
- a controller env without the configured channels, which the rehearsal refuses
- a probe for the profile downgrade that fails to run, which stops the rehearsal and the rollback rather than skipping the downgrade; a repeated rollback runs it
- a rollback after resume with the controller down
- a crash right after a mutation, then rollback
- a rollback killed after it stops the new Gene, where resume refuses and a repeated rollback finishes
- a live state outside the recorded pair, which rollback refuses without changes
- a deployment that lands a third release while the rollback fence waits, refused before Fri or Gene is touched
- a Fri coordinator still holding its lock, which rollback waits out
- backups moved to another filesystem, refused before the switch
- backups bind-mounted on the same filesystem (same device number, renames refused), refused before the switch
- the source tree bind-mounted onto itself, refused before the switch with Red still running
- a killed batch shell whose Gene compose up keeps running, which rollback waits out
- an owner's pause from before the release, with root-owned journal files at drain, which the release leaves in place
- root-owned DB journal files left by an interrupted Fri phase, recovered by rollback
- a crash right after the pause commits
- a resume repeated after an interrupted result write and after a complete one, and refused for a different result

## After the release
- The owner DMs Fri "fri health" and should see the watch running, the controller up at G, and a fresh Gene heartbeat.
- The owner DMs Fri "show reply checks" and should see that they're off.
- The three older review jobs stay in review with stale notices. Completing them needs a rebuild.
