# Task 3 report: retired root profile controller removal

Status: complete.

Removed the legacy root controller modules, legacy fleet/package readers,
retired schemas, and profile-only tests. `vonkctl` now exposes current nodes,
endpoint, Fleet/jobs/audit, package, deployment, proposal, and platform-update
administration paths; profile/model commands and local profile switching are
not registered. The cross-boundary generic-package E2E tests now run in the
control suite, where both control and agent sources are available.

Verification:

- `404 passed` for `tests/cluster_profiles`.
- `2 passed` for the moved generic-package E2E tests.
- `49 passed` for the v1 absence, current CLI, and package-contract tests.
- `1,083 tests collected` for root Python 3.12 collection.
- Ruff 0.16.1 clean for changed Python files.
