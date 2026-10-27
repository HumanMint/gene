# Fri/Gene service separation

## Objective and boundary

Gene should remain an upstream Red installation with custom cogs and a narrow
command/management interface. Fri owns the companion's conversation, memory and
background application services. Trusted coding, approval and release authority
remains separate from the companion. Fri never gains GitHub, Docker, worker or
deployer credentials through this separation.

This is staged work, prepared independently of the full-send coordinator's feature
branches. The coordinator integrates reviewed PRs against its latest commits and
owns release ordering and the maintenance window. No migration PR is proof that
the entire separation is finished.

## Slice 1: remove the retired client from the production package

Base: Gene `4c51f3caa8ce5f8106edaf797afb3be259c6aaa2`.

- Thirty-three retired client modules move from `src/gene/<name>.py` to
  `tests/legacy_fri/<name>.py`. Existing tests and patch targets name that fixture
  explicitly; its imports of live controller contracts continue to use `gene`.
- The old opt-in `fri` service is removed from `ops/compose.yml`. The independent
  `ops/fri-compose.yml` service, `gene.fri_release`, and rollback to an existing
  immutable legacy container remain unchanged.
- No controller route, schema, contract capability, credential, privacy fence,
  memory record, pending action, receipt or integration pin changes.
- An isolated-interpreter regression checks that the retired client is absent
  while the controller, Red adapter, worker and deployers still import and the real
  controller serves its existing health/contract routes using a synthetic database.

The installed package still has active `fri_*` backend modules. This slice removes
the duplicate companion; it does not yet move the backend or its database.

## Next slices

1. Extract the shared wire types and command grammar from companion-specific
   modules. Give the Gene command bridge an explicit, bounded contract. Preserve
   the current `gene-fri/1` routes while old and new clients coexist.
2. Move the active Fri backend implementation into a separately packaged service
   owned by the Fri repository. Keep its integration with the trusted management
   service explicit, versioned and covered by the real-controller fixtures.
3. Separate persistent ownership only with a reviewed cutover and rollback design.
   Today erasing member context and withdrawing dependent development jobs happen
   in the same Gene transaction. A new service must preserve that guarantee across
   failures, delayed delivery and old/new combinations before the data is moved.
   Do not mount Gene's database in the companion or replace these fences with an
   eventually delivered notification.
4. Retire compatibility aliases and fixtures only once the supported old/new
   deployment matrix no longer needs them. Keep upstream Red pinned and verify
   its runtime, command and audio adapters when the Red dependency changes.

## Integration handoff

This branch does not edit the coordinator's workspace, feature branches,
`integration-pair.json`, production configuration or the shared workspace handoff.
The PR carries its reviewed exact head and proof; a queued message supplies the
coordinator with the PR URL and remaining work.

When integrating a fix made after this base, preserve its ownership: active Fri
client fixes belong in `HumanMint/fri`; Gene-side old-client regression changes
belong in `tests/legacy_fri`; controller fixes stay in the live `src/gene` modules.
Resolve a delete/modify conflict by checking that mapping rather than restoring a
second production companion. Re-run the combined checks, and obtain fresh review
for any behavior-changing conflict resolution. Merge and deployment are separate.

## Proof and remaining work

The unchanged base passed 1,443 tests, with 45 platform-dependent skips, under a
network-denying sandbox with a clean environment and synthetic test state. The
new runtime-boundary regression failed on that base because `gene.fri` was still
installed. Candidate and wheel checks, fresh Sol review and final PR details are
recorded in the PR before handoff. No live or model eval is needed for this
test-fixture relocation; user-facing companion behavior does not change.
