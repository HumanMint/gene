# Batched Gene + Fri production release runbook (v2)

The release thread "Finish Gene request awareness in Fri" owns this release. The owner approved it on 2026-10-25 in two messages: "merge and release all as one big release when all private reviews are done" and "approved: production access for the big release".

The host steps live in `batch-release.sh`, next to this file. It covers v1's Codex review findings: draining before stopping, journaled recovery for every mutation, DB migration and downgrade rehearsal on copies, the playback check, baseline-matched health probes, a guarded settings transaction, Fri reconciliation, fresh destinations, and disk sizing.

The release runs from the Mac mini's Remote Control session in `/Users/danmacmini/Projects/vibing`, over `ssh hostinger` with `sudo -n`. It never prints secrets. It never starts the legacy `fri` service in Gene's compose or `gene-fri-1`, and it never injects synthetic chat into live history.

## On the Mac mini
1. Fetch both repos.
   - Confirm `origin/main` is G for gene and F for fri, and that CI is green on both.
   - Get V with `git -C fri show "$F:docs/integration-pair.json"` (the `verified_controller_commit` field).
   - Then `git -C gene merge-base --is-ancestor "$V" "$G"`. STOP if that fails.
2. Make the tarballs. `git -C gene archive --format=tar "$G" > gene.tar`, the same for fri, then `sha256sum gene.tar > gene.tar.sha256`, and the same for fri.
3. Copy the script and the tars to `/srv/gene/staging/batch-$G/` (root, 0700).
   - Run `ssh hostinger "sudo -n install -d -m 0700 /srv/gene/staging /srv/gene/staging/batch-$G"`.
   - Pipe each file through `ssh hostinger "sudo -n tee <path> >/dev/null"`.
   - Install the script as root 0700 at `/srv/gene/staging/gene-batch-release.sh`.
4. Run the phases one at a time with `ssh hostinger "sudo -n /srv/gene/staging/gene-batch-release.sh <phase> $G $F"`. Report each phase's output before starting the next.

## Phases
| Phase | Touches services? | What it does |
|---|---|---|
| `baseline` | no | Checks the live state matches Gene f667001 / Fri 6a1d410. Records the settings, the guild id and presence, and the paused flag. |
| `stage` | no | Checks the tar hashes and extracts into fresh dirs. Refuses dependency or ops diffs (`ALLOW_OPS_DIFF=1` after review). Builds the labelled images, runs isolated import checks, runs the controller contract on synthetic state, checks Fri accepts that contract, and sizes the disk. |
| `rehearse` | no | Copies the live DB twice into a private dir. Migrates one copy with G and then starts the old controller on it, including an old-code request write. Migrates the other, runs the reply-check purge and the profile downgrade, then starts the old code on it. Deletes the copies. |
| `drain` | pauses development | Pauses through the store (audited) and waits up to 30 min for building/deploying jobs and `deploy-result.json` to clear. Then stops gene-worker and gene-deployer and rechecks. |
| `gene` | Red ~1 min, controller seconds | Waits up to 30 min until no audio is streaming. Then backs up (unique dir), stops Red, installs the cogs, swaps the source, and pins `release.env` and the worker image. Starts controller and red, checks health (SHA, guild, presence, controls, freshness, old Fri alive), then records `active_sha`/`previous_sha` plus audit in one guarded transaction. Any failure rolls back automatically. |
| `fri` | Fri ~1 min | Runs `gene.fri_release F` (G's coordinator) and checks health. On failure it prints reconciliation state, and the workers stay stopped. |
| `evals` | no | Runs the live `requests`, `sass` and `controls` suites inside fri-runtime-1 and prints a digest. Never rolls back by itself: the release thread decides. |
| `verify-off` | no | Confirms reply checks are off and have no runs. |
| `resume` | resumes development | Checks that running, pinned and recorded state agree. Starts the units, unpauses only if the release paused, records an audit row, and writes `maintainer-G-result.json`. |
| `cleanup` | no | Removes the staged sources and tars. Keeps the journal, baseline, evals, backup and all images. |
| `status` | no | Read-only state plus the journal. |
| `rollback-gene` | yes | Journal-driven full rollback. Restores Fri 6a1d410 first, downgrades the DB with G's tools while the controller is stopped, then restores the env files and source. Starts the old images, checks health, and restores settings with a `maintainer_rollback` audit. It never restores a DB snapshot; that stays an operator decision. |
| `resume-old` | resumes development | After a rollback, once the old state is consistent. |

## Post-release checks (owner or other threads)
- The owner DMs Fri "fri health". Expect "watch running", "controller: up, release <sha>" and a fresh Gene heartbeat (#14).
- The owner DMs Fri "show reply checks". Expect "reply checks are off" (#11).
- #15: "you'd be a terrible model lol" in a DM gets a normal reply with no settings preview. "be a bit less sarcastic" still shows an Apply preview.
- Mention follow-up: a ping during cooldown gets a 🪫 reaction, and a second message sent mid-reply gets answered after the first.
- Memory v2: Fri's log shows "Member profiles: N of M configured channels are readable by everyone". If N is 0, profiles never fill.
- Ping the thread "Sharpen Fri's sassy-helpful voice" to check that cached input rises in the usage breakdown (#8 prompt_cache_key).
- The 3 older review jobs stay in review and show stale notices. Completing them needs a rebuild.
