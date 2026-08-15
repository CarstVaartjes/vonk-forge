# Remote development project publication implementation plan

**Goal:** Make fresh development NAS publication reproducible from Linux or
WSL without depending on SMB client filesystem semantics.

**Architecture:** A small standard-library workstation wrapper validates and
snapshots the accepted Compose and complete local secret generation, streams a
bounded tar over batch-mode SSH to NAS tmpfs, and runs the existing publisher
inside the accepted API image against the NAS's real filesystem. The existing
publisher remains the only code that renders and mutates the project.

**Tech stack:** Python 3.12 standard library, OpenSSH, Docker, existing control
environment, pytest, Markdown contract tests.

## Task 1: Lock the remote boundary in tests

- Add `scripts/tests/test_dev_runtime_project_remote.py`.
- Cover source snapshots, archive membership/modes, SSH argv, image extraction,
  path validation, Docker modes, cleanup, output redaction, and failures.
- Run the new test module and confirm it fails for the missing entrypoint.

## Task 2: Implement the remote publisher

- Add `scripts/dev-runtime-project-remote`.
- Reuse the local publisher's source validation and generation lock.
- Build the bounded tar in memory and invoke OpenSSH without a shell locally.
- Run the existing publisher in the restricted accepted API container on NAS
  tmpfs and propagate errors without leaking input values.
- Run focused local and existing publisher tests.

## Task 3: Correct the operator documentation

- Make the remote path recommended in
  `docs/runbooks/fresh-development-install.md` and
  `docs/runbooks/development-nas-installation.md`.
- Explain why Windows write access does not imply POSIX publisher support.
- Retain the direct mounted path only for mounts that satisfy the checks.
- Document direct Docker versus `sudo -n`, strict SSH host trust, tmpfs cleanup,
  anonymous image pull, and the exact two-item share result.
- Update runbook contract tests first, then the prose.

## Task 4: Align architecture explanations

- Update `docs/architecture-overview.md` and
  `docs/vonk-forge-architecture.html` where the physical installation exposed
  stale names or fixed-two-node wording.
- Make one-node, two-node tensor-parallel, and many-node fleet behavior clear.
- Keep the NAS service map and control/runtime contract suitable for reuse by
  `vonk-forge-web`.

## Task 5: Close the lifecycle finding exposed by acceptance cleanup

- Reproduce the failed uninstall of a partial historical installation.
- Keep digest checks fail-closed for present installations, but make uninstall
  idempotently succeed when the exact installation is already absent.
- Add Rust tests for absent, present, and unsafe installation metadata.
- Publish the correction only through the normal main-branch agent package
  workflow before retrying the controller operation.

## Task 6: Verify and publish

- Run focused script, runbook, and documentation contracts.
- Run formatting/linting appropriate to changed files.
- Request code review, address findings, push a PR, and merge only after CI.
- Exercise the new wrapper against the current NAS with the accepted artifact
  and compare the resulting two-item project without redeploying a new cohort.
