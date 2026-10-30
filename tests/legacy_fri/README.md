# Retired Fri client: compatibility fixture

This is the Gene-hosted companion at Gene commit
`4c51f3caa8ce5f8106edaf797afb3be259c6aaa2`, relocated from `src/gene/`.
Only import locations changed. The fixture shares the current `gene` controller
contracts and stores, so existing offline tests continue to cover an older client
against a newer controller without shipping that client in the production package.

Tests import `tests.legacy_fri.*` explicitly. This directory is not a Python
distribution and is not a supported bot or live-eval entry point. New companion
behavior and live evals belong to `HumanMint/fri`. Do not copy a production token,
subscription identity, conversation or database here.

The production image excludes tests. The worker image includes them for offline
verification; they do not add a runnable service. Retained immutable images and
containers, not these fixtures, provide initial-cutover rollback.
