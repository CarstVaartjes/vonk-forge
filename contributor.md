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

Integration PRs run full acceptance of their exact merge commit through CI's
`lane-proof` job, selected by dependency or the `release-acceptance` label.
A green result on an older commit does not cover new source. Fork source needs
a reviewed same-repository branch for the token's candidate image write scope.

To inspect an explicit candidate, dispatch **Pre-merge release acceptance** with
its `ref` input set to the commit SHA (`gh workflow run release-acceptance.yml
-f ref=<sha>`). The PR path generates ephemeral Ed25519 package and RSA installer
signing keys inside their jobs and writes images only to per-run GHCR tags using
`GITHUB_TOKEN`. Signed candidate objects stay in run artifacts and are served
over loopback HTTPS in each lane. Private signing keys are never uploaded.

The Spark setup binary must be built with `acceptance-test-trust` and receive
both `VONK_ACCEPTANCE_TEST_MODE=1` and an explicit
`VONK_ACCEPTANCE_RELEASE_PUBLIC_KEY` path. Default production builds refuse
these inputs. The root handoff authenticates the exact setup and carries the
same test authority into privileged verification. Production trust roots remain
unchanged. PR NAS lanes explicitly report disabled Tailscale; main still proves
the real disposable tailnet. PR upgrade-carry uses the signed lower package in
the candidate's acceptance baseline, so it always exercises a real upgrade.
Protected release environments remain main-only and are never requested by PRs.

The PR workflow builds both image architectures, native setup programs and
signed ARM64 candidate/baseline packages. It retains immutable candidate
objects and digest-bound candidate image tags. It then calls the same clean NAS,
clean Spark and upgrade-carry acceptance jobs that main's Release calls. Main
alone promotes the accepted channel and publishes apt. These synthetic CI
Spark checks do not prove physical GPU or model-quality qualification.
