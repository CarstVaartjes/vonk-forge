# Contributors

Thank you to everyone who contributes improvements, fixes, documentation, and
feedback to Vonk Forge.

## External contributors

- Matthew Kelch (`@kelchm`)

Additional contributors should be added here when their contributions are
merged into the repository.

## Local checks

Run `scripts/test-local` from your worktree for the repository and Controller
pytest suites. On macOS it sends Linux-marked tests to the `vonk-ci` OrbStack
VM when available; the script builds the local agent protocol wheel and checks
it against `control/uv.lock` before running Controller commands. For direct
Controller commands, run `scripts/build-control-wheel` once first. See
[Testing and CI](docs/testing-and-ci.md) for focused commands and the other
acceptance lanes.

## Added-line guards

`scripts/check-added-lines` uses the merge base with `origin/main`; CI supplies
the PR base. Only new syntax on added lines fails, with no debt or category
ledger and no before/after count report. Keep behavior tests and generated
contract checks. See [principle guards](docs/principle-guards.md).
