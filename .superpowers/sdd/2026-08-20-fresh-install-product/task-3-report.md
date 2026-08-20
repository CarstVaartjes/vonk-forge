# Task 3 report: API-owned fresh initialization

## Delivered

- Removed the persistent `control-bootstrap` helper, its healthcheck, its
  dependencies, its Python module, and its obsolete tests. The direct SQL/reset
  schema bootstrap was also removed; the maintained fresh Alembic baseline is
  now the only schema mechanism.
- Added a root-only `vonk_control.api_preexec` entrypoint to the real API image.
  It normalizes file-backed secrets, restores the existing per-consumer named
  volume ownership/mode contract, installs the API-only admin grant key,
  initializes PostgreSQL, and then execs `python -m vonk_control.api`.
- The pre-exec irreversibly clears supplementary groups and sets real,
  effective, and saved GID/UID to `10001`. It verifies that the dropped process
  cannot traverse `/run/secrets` before exec, so Uvicorn remains PID 1 and no
  privileged helper sleeps or exits beside it.
- The API image pre-creates `/run/secrets` as `root:root 0700`. The final worker
  image remains `USER 10001:10001`; only the final API image starts as root and
  owns the pre-exec entrypoint. A named-stage Dockerfile contract prevents these
  directives from drifting onto the worker again.
- Compose retains `cap_drop: [ALL]` and `no-new-privileges:true` for the API,
  adding exactly `CHOWN`, `FOWNER`, `DAC_OVERRIDE`, `SETUID`, and `SETGID` for
  the bounded startup phase. `SYS_ADMIN` is absent.
- The API now mounts the former bootstrap secret and named-volume boundaries.
  Route, LiteLLM supervisor, signer socket/verifier, agent publication, workload
  publication, normalized-secret, and API runtime ownership/modes match the
  removed bootstrap behavior. Normalized files keep their consumer-specific
  UID/GID and mode.
- The API depends only on healthy PostgreSQL. Worker, signer, LiteLLM, and Step
  CA use `control-api: service_healthy` ordering; Step CA is not an API
  prerequisite, so the graph has no dependency cycle.
- PostgreSQL startup now takes a session-level advisory lock before applying
  the single Alembic head. The same lock remains held while the initial
  PostgreSQL authority revision and singleton head are committed. Repeated and
  concurrent initializers are idempotent and cannot expose a partial authority
  head.
- The fresh PostgreSQL initializer contract proves distinct `control` and
  `litellm` roles and databases with matching distinct owners.
- Renderer token-count guards, operator documentation, PKI/runbook contracts,
  networking contracts, and the strict Task 3 expected failure were updated to
  the helper-free model. The strict xfail is now an ordinary passing behavioral
  contract.

## TDD evidence

- RED: the initial unit run failed collection because `SharedRuntimePaths` and
  the API pre-exec interface did not exist. The model-only run then reported 12
  expected failures for the still-present helper, API UID/capability boundary,
  missing source secrets/volume mounts, missing health dependencies, and absent
  Dockerfile pre-exec directives.
- RED (Dockerfile regression): after the first Dockerfile edit, the named-stage
  contract reported that the worker stage contained `USER 0:0` and
  `api_preexec`. This reproduced the misplaced final-stage directives before
  the correction.
- GREEN (Dockerfile regression): the same stage-aware test reported `1 passed`
  after restoring the worker stage and moving root/pre-exec/CMD to the API
  stage.
- GREEN (focused): the final focused suite reported `138 passed, 14 skipped in
  11.93s` across API pre-exec/runtime initialization, database authority,
  canonical Compose/security/networking, PostgreSQL initialization, renderers,
  PKI runbook, and deployment-bundle coverage.

## PostgreSQL integration policy and local result

`test_concurrent_fresh_startup_migrates_once_and_creates_one_authority_head`
starts a real pinned PostgreSQL 18.3 container, drops to a fresh public schema,
runs two initializers concurrently, and checks the exact Alembic head, one
authority revision, one singleton head, and a valid head-to-revision join.
Docker absence fails the test on CI. Locally it skips only after checking for
the Docker CLI and a responsive Docker daemon; container startup/readiness
failures are assertions rather than skips.

This host has no Docker socket (`/var/run/docker.sock` is absent), so the four
container-backed cases skipped locally. Two non-container PostgreSQL
initializer behavior tests passed. The real concurrent test is therefore
present and CI-enforced but could not execute on this host.

## Final verification

- Focused pytest: `138 passed, 14 skipped in 11.93s`.
- Ruff 0.16.1 over every changed Python file: passed.
- `sh -n deploy/compose/postgres/init-databases.sh`: passed.
- Canonical default `docker compose ... config --quiet`: passed.
- Canonical Hermes-profile `docker compose ... --profile hermes config
  --quiet`: passed.
- `git diff --check`: passed.
- Rendered graph inspection confirmed the helper-free exact service set, API
  capability list/security option, PostgreSQL-only API dependency, all source
  secrets on the API pre-exec boundary, and real-service health dependencies.

## Scope and preserved work

The unrelated unstaged Task 6 changes in `control/src/vonk_control/offline.py`,
`control/tests/test_host_backup.py`,
`control/tests/test_host_upgrade_boundary.py`, and
`control/tests/test_offline.py` were not edited or staged. The untracked
fresh-install design and plan files were read for context and left untouched;
the binding ruling was already represented by the implementation brief and
this report.
