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

## CI and pre-merge release acceptance

Every PR runs the complete sharded Controller and repository suites, repository
guards, web tests, Rust tests/lint, generated clients and Python lint/types.
Merge queues also accept their combined candidate before admission.
Integration selection uses the conservative dependency closure documented in
`scripts/select-ci-areas`; unfamiliar inputs select every integration. The
Controller suites include their real PostgreSQL tests on every PR.

Integration PRs carry the `release-acceptance` label. CI adds it to
same-repository integration PRs and requires acceptance of that exact PR merge
commit in `CI gate`. Create that repository label before enabling this workflow.
The standalone `labeled` and `synchronize` events rerun acceptance; a green result on an
older commit does not cover a new one. Fork PRs need reviewed source in a
same-repository branch before receiving protected signing credentials.

To inspect an explicit candidate, dispatch **Pre-merge release acceptance** with
its `ref` input set to the commit SHA (CLI: `gh workflow run release-acceptance.yml
-f ref=<sha>`). Dispatch the workflow from the branch containing that revision
of the workflow when reviewing changes to the harness itself.
Protected `installer-candidate-dev`, `agent-development`, `installer-canary-dev`
and `installer-acceptance-dev` environments must permit reviewed PR refs with
the existing scoped secrets; do not remove their review protections.

The PR workflow builds both image architectures, native setup programs and
signed ARM64 candidate/baseline packages. It publishes only immutable candidate
objects and digest-bound candidate image tags. It then calls the same clean NAS,
clean Spark and upgrade-carry acceptance jobs that main's Release calls. Main
alone promotes the accepted channel and publishes apt. These synthetic CI
Spark checks do not prove physical GPU or model-quality qualification.
