# Contributing

Thank you to everyone who contributes improvements, fixes, documentation, and
feedback to Vonk Forge.

Open an issue for a bug or an idea, or a pull request for a fix. Recipe
changes belong in
[vonk-forge-recipes](https://github.com/CarstVaartjes/vonk-forge-recipes).
Report security problems privately as [SECURITY.md](SECURITY.md) describes.
Everyone follows the [code of conduct](CODE_OF_CONDUCT.md). The rules for
changes are in [AGENTS.md](AGENTS.md) and the
[engineering principles](docs/engineering-principles.md).

## External contributors

- Matthew Kelch (`@kelchm`)

Additional contributors should be added here when their contributions are
merged into the repository.

## Release notes

There is no hand-kept changelog. Each stable release's notes are its GitHub
Release body, generated from the merged pull request titles; deprecations are
announced there ([Stable release](docs/runbooks/stable-release.md#what-becomes-public-when)).

## Local checks

Run `scripts/dev/test-local` from your worktree for the repository and Controller
pytest suites. On macOS it sends Linux-marked tests to the `vonk-ci` OrbStack
VM when available. The Python packages are one uv workspace with one root
`uv.lock` and one root `.venv`; the protocol is an editable member, so a fresh
clone runs Controller commands and tests with `uv run --project control --frozen`
and no wheel step (`scripts/build-control-wheel` is only for image builds and
releases). See
[Testing and CI](docs/testing-and-ci.md) for focused commands and the other
acceptance lanes.

## Added-line guards

`scripts/dev/check-added-lines` uses the merge base with `origin/main`; CI supplies
the PR base. Only new syntax on added lines fails, with no debt or category
ledger and no before/after count report. Keep behavior tests and generated
contract checks. See [principle guards](docs/principle-guards.md).

## CI and pre-merge release acceptance

Every change lands through an integration PR (`integration/*` or the
`release-acceptance` label), which runs the complete suites, candidate
acceptance and the path-filtered proof workflows. A member PR runs no CI jobs
and reports `CI gate (member PR)` instead of the required `CI gate`, so it
cannot merge itself. [Testing and CI](docs/testing-and-ci.md) lists what runs
where (`full` in `scripts/ci/select-ci-areas`).
Merge queues also accept their combined candidate before admission.
Integration selection uses the conservative dependency closure documented in
`scripts/ci/select-ci-areas`; unfamiliar inputs select every integration. The
Controller suites include their real PostgreSQL tests on every full run.

Integration PRs carry the `release-acceptance` label. CI adds it to
same-repository `integration/*` PRs (the branch alone selects acceptance) and requires acceptance of that exact PR merge
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

The PR workflow builds both image architectures, one Python NAS setup wheel,
the native ARM64 Spark installer and signed ARM64 candidate/baseline packages. It publishes only immutable candidate
objects and digest-bound candidate image tags. It then calls the same clean NAS,
clean Spark and upgrade-carry acceptance jobs that main's Release calls. Main
alone promotes the accepted channel and publishes apt. These synthetic CI
Spark checks do not prove physical GPU or model-quality qualification.
