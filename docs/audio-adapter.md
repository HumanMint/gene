# Independent audio adapter

[red-audio-adapter](https://github.com/HumanMint/red-audio-adapter) owns Gene's
external Lavalink source, resolver/cache, optional private download relay, images and
audio transport tests. Gene owns its Red cog/commands, diagnostics and deployment
authority. The exact Python dependency is recorded in [audio-adapter.json](audio-adapter.json)
and `uv.lock`; do not replace it with an unreviewed branch tip.

## New setup

Use the adapter's [setup guide](https://github.com/HumanMint/red-audio-adapter/blob/main/docs/setup.md).
Clone/check out its pinned source and build its audio images there. Configure Gene's
private external-node host/port/password and `GENE_AUDIO_PREPARE_URL` /
`GENE_AUDIO_PREPARE_TOKEN`. Gene's startup already installs the pinned preparation hook.
No controller credentials or database belong in the audio adapter.

The `GENE_*` configuration and `X-Gene-*` protocol names remain compatible. Existing
`gene.audio_cache`, `gene.audio_resolver`, `gene.audio_relay_server` and
`gene.runtime_audio` imports delegate to the new package, including monkeypatch targets.
`python -m gene.audio_resolver` remains a compatibility entry point; new installs use
`red-audio-resolver` from the adapter checkout. Build/install tools now live in that repo.

## Existing installation

The daily runtime batch installs the pinned Python dependency but does not rebuild or
switch the independently deployed audio node, resolver or relay. Keep the existing
audio image pins, configuration, media/cache metadata, leases, private tokens, installed
service labels and rollback releases until an explicit audio migration is verified.

`ops/audio-compose.yml` remains Gene's deployment glue. Newly built images come from
the adapter repo, retain the existing `gene-audio:<sha>` / `gene-audio-resolver:<sha>`
tag names, and identify adapter source revisions. `GENE_AUDIO_SHA` pins the node;
`GENE_AUDIO_RESOLVER_SHA` can independently pin the resolver. Old pins remain valid.
Gene image retention continues protecting audio images and does not remove them here.

When migrating the relay, use the new installer's `--label <existing-label>` and private
settings file. Do not accidentally start a second service under its new default label.
Keep the former relay release folder and service definition for rollback. No Red,
controller, authentication or cache state needs to be reset for the repo split.

Review/merge the adapter revision before Gene's integration, then run Gene's suite,
the adapter suite and the release-script mock suite on the exact candidate. Production
switches follow the owner's maintenance window and release rules. A successful health
check or search does not prove audible playback; verify that separately during rollout.

For detailed protocol, independent audio rollout and rollback, use the adapter's
[Gene guide](https://github.com/HumanMint/red-audio-adapter/blob/main/docs/gene.md).
