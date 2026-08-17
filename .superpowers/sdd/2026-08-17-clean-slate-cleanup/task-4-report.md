# Task 4 report: legacy package/deployment cleanup

Status: DONE_WITH_CONCERNS.

Commit hash: final committed hash is reported in the task handoff response.

## Scope completed

- Deleted the old Alembic migration chain and replaced it with `control/migrations/versions/0001_fleet_library_baseline.py`.
- Removed package/deployment control API wiring, service construction, worker loops, operation IDs, auth special cases, metrics/dashboard summaries, publication/signing helpers, rollout orchestration, validation runners, and package/deployment SQLAlchemy models.
- Removed package/deployment API modules, tests, generated OpenAPI clients, web generated types, stale web e2e package-page test, cluster-profile package/deployment CLI/client surfaces, package-family/workload-deployment schemas, obsolete runbook/docs/plans, stale fixture artifacts, and CI references to deleted package/deployment tests.
- Preserved Spark-agent package/runtime protocol consumers where concrete retained consumers remain (`agent`, `agent_protocol`, agent package store/runtime tests, and control agent helper authority). The control helper authority was renamed from package-helper implementation naming to workload-helper naming while retaining Spark-agent wire protocol compatibility.

## Changed/deleted file categories

- Migrations: deleted `control/migrations/versions/0001_operational_state.py` through `0027_execution_harness_catalog.py`; added `0001_fleet_library_baseline.py`.
- Control source: modified `api.py`, `agent_api.py`, `auth.py`, `dashboard.py`, `metrics.py`, `models.py`, `operation_api.py`, `repository.py`, `settings.py`, `worker.py`, dev revision references, and helper-authority imports.
- Deleted control source modules: `package_api.py`, `package_compatibility.py`, `package_discovery.py`, `package_providers.py`, `package_publication.py`, `package_resolution.py`, `package_rollout_worker.py`, `package_rollouts.py`, `package_services.py`, `package_validation.py`, `package_validation_runner.py`, `workload_signer.py`, `workload_trust.py`, and the old `package_helper_authority.py`.
- Tests: deleted old package/deployment/migration tests and rewrote retained tests for negative route contract, fresh baseline, worker wiring, repository paths, OpenAPI absence, docs, and supply-chain baseline references.
- Artifacts/docs/CI: regenerated `control/openapi.json`, `control/web/src/api/generated.d.ts`, and `src/cluster_profiles/generated_control`; removed package/deployment generated client files, package schemas, workload-package runbook, stale plans/specs, stale fixtures, and obsolete CI gates.

## Red/green evidence

Red:

- `uv run --project control pytest -q control/tests/test_api.py -k legacy`
  - Expected failure before route deletion: legacy package/deployment route paths were still registered.
- Dependency search before deletion:
  - `rg -l "package_api|package_services|package_rollout|PackageCandidate|PackageRollout|/api/v1/packages|/api/v1/deployments" control/src control/tests control/web/src`
  - Identified package/deployment API, worker, metrics, dashboard, auth, model, route, test, and generated-client consumers before removal.

Green / verification:

- `uv run --project control python -m compileall -q control/src/vonk_control` — passed.
- `uv run --project control pytest -q control/tests/test_api.py -k legacy` — `1 passed, 12 deselected`.
- `uv run --project control pytest -q control/tests/test_migrations.py` — `2 passed`.
- `uv run --project control pytest -q control/tests/test_worker.py control/tests/test_production_worker.py control/tests/test_repository.py control/tests/test_proposals.py control/tests/security/test_boundaries.py control/tests/test_workload_helper_authority.py control/tests/test_harness_registry.py` — `145 passed`.
- `uv run pytest -q tests/control/test_openapi_clients.py tests/cluster_profiles/fleet/test_schemas.py tests/test_docs_contract.py tests/test_no_prototype_model_authority.py` — `65 passed`.
- `uv run pytest -q tests/scripts/test_verify_platform_release.py tests/scripts/test_verify_supply_chain.py tests/agent/test_failure_matrix.py tests/control/test_failure_injection.py` — initially failed on stale `0015_recipe_catalog.py` fixture references; after updating to `0001_fleet_library_baseline.py`, `80 passed`.
- `git diff --check` — passed after removing one trailing blank line.

## Migration baseline verification

- `control/tests/test_migrations.py` upgrades a blank database to Alembic head from the fresh `0001_fleet_library_baseline.py`, verifies the created table set matches `Base.metadata`, verifies `agent_node_profiles` exists, verifies deleted legacy package/deployment tables are absent, and verifies Alembic autogenerate diff is empty.
- The same test downgrades from head to base and verifies the schema is reversible.

## Dependency sweep output

Final required sweep:

- `rg -n "PackageCandidate|PackageRollout|/api/v1/packages|/api/v1/deployments|config/package-families|config/workload-deployments" control/src control/tests control/web/src`
  - Exit 1, no output.

Final broader package route/module sweep:

- `rg -n "package_api|package_services|package_rollout|PackageCandidate|PackageRollout|/api/v1/packages|/api/v1/deployments" control/src control/tests control/web/src`
  - Exit 1, no output.

Expanded supported-source sweep after doc/artifact cleanup only hits the current clean-slate plan that describes this task; retained source/tests/docs no longer advertise the deleted control package/deployment pipeline.

## Concerns

- Broad `uv run --project control pytest -q control/tests` was started and then interrupted at the user's request. It had progressed to about 35%, showed a block of errors near 16% and two failures before interruption, but no final failure summary was produced because it was stopped with KeyboardInterrupt.
- The retained Spark-agent package/runtime protocol still uses package-oriented wire names under `vonk_agent_protocol.workload_packages` and `/agent/v1/package-helper/*`; these were preserved only because concrete Spark-agent consumers remain.

## Review fix round: active package/deployment operation cleanup

Status: DONE_WITH_CONCERNS.

Fix-round commit hash: reported in the final handoff response for the committed tree.

### Scope completed

- Removed active package/deployment operation handling that review identified in `control/src/vonk_control/orchestration.py`, `control/src/vonk_control/agent_reconciliation.py`, and `control/src/vonk_control/agent_jobs.py`.
- Replaced positive package-operation control-plane tests with negative route/operation contract tests proving `package.prepare` persisted graph nodes, job queue operations, capabilities, and evidence are rejected instead of compatibility-wrapped.
- Replaced the live-metadata Alembic baseline implementation with fixed explicit Alembic table/index/constraint operations in `control/migrations/versions/0001_fleet_library_baseline.py`.
- Updated migration tests to verify an explicit expected retained table set, `agent_node_profiles`, no deleted package/deployment tables, empty metadata diff, and no baseline import of `vonk_control.models`, `Base.metadata`, or `.create_all(`.
- Deleted stale tracked package/deployment interface artifacts:
  - `tests/e2e/UNKNOWN_WORKLOAD_PACKAGE_INTERFACES.md`
  - `docs/superpowers/plans/2026-08-05-node-workload-package-engine.md`
  - `docs/superpowers/plans/2026-08-07-vonk-local-catalog-authority.md`
- Rewrote older supported-source roadmap/spec references so they no longer present deleted package/deployment APIs or package operation IDs as active supported direction.

### Red evidence

- `uv run --project control pytest -q control/tests/test_orchestration.py -k package_operations`
  - Failed before production changes because a `package.prepare` graph node was accepted instead of raising.
- `uv run --project control pytest -q control/tests/test_orchestration.py -k deleted_package`
  - Failed before production changes with the old package graph lifecycle validation path instead of rejecting the deleted package operation as an unsupported graph operation.
- `uv run --project control pytest -q control/tests/test_agent_reconciliation.py -k package_evidence`
  - Failed before production changes because package evidence was still accepted.
- `uv run --project control pytest -q control/tests/test_agent_jobs.py -k package`
  - Failed before production changes because `package.prepare` could still be enqueued/claimed through the control-plane job service.
- `uv run --project control pytest -q control/tests/test_migrations.py`
  - Failed before the migration rewrite because `0001_fleet_library_baseline.py` still imported `vonk_control.models` and used live `Base.metadata`.

### Green and blocker evidence

- `uv run --project control python -m py_compile control/migrations/versions/0001_fleet_library_baseline.py` — passed.
- `uv run --project control pytest -q control/tests/test_orchestration.py -k 'package_operations or deleted_package' control/tests/test_agent_reconciliation.py -k package_evidence control/tests/test_agent_jobs.py -k package` — `5 passed, 144 deselected in 0.74s`.
- `uv run --project control pytest -q control/tests/test_migrations.py` — `3 passed in 1.38s`.
- `uv run --project control pytest -q control/tests/test_orchestration.py control/tests/test_agent_reconciliation.py control/tests/test_agent_jobs.py control/tests/test_migrations.py control/tests/test_api.py control/tests/test_operation_api.py control/tests/test_fleet_projection.py control/tests/test_fleet_stream.py control/tests/test_library_api.py control/tests/test_library_projection.py control/tests/test_catalog_api.py control/tests/test_recipe_api.py control/tests/test_workload_run_api.py` — `305 passed, 1 skipped in 30.01s`.
- `uv run --project control pytest -q control/tests` — stopped at the user's request after the Docker environmental failures were captured; summary at interruption: `2 failed, 796 passed, 36 skipped, 42 errors in 73.58s (0:01:13)`.

Exact broad-test Docker blocker:

- `control/tests/test_agent_reconciliation_postgres.py` errors at fixture setup while running `docker run --rm -d -e POSTGRES_PASSWORD=postgres -p 127.0.0.1::5432 postgres:16`.
- Docker stderr: `Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?`
- `control/tests/test_dev_runtime_assets.py::test_caddy_entrypoint_stages_runtime_files_as_uid_10000` failed with the same Docker daemon error.
- `control/tests/test_dev_runtime_assets.py::test_caddy_entrypoint_requires_fresh_generation_and_reacts_to_real_file_events` failed because the Docker-based Caddy handoff process exited with the same Docker daemon error.
- The broad run also ended with `KeyboardInterrupt` because it was stopped immediately for status reporting, not because of a new code-path assertion failure.

### Fix-round dependency sweep notes

- Active control-plane sweeps after production cleanup found no package operation registration, validation, package evidence, or package queue handling in `control/src/vonk_control/orchestration.py`, `control/src/vonk_control/agent_reconciliation.py`, or `control/src/vonk_control/agent_jobs.py`.
- Remaining `package.prepare`, `deployment_id`, and `deployment_digest` hits in focused control tests are the new negative contract fixtures.
- Remaining `PACKAGE_HELPER_*` and package-helper wire names in control source are retained Spark-agent helper protocol constants/settings with concrete consumers, not deleted package/deployment control-plane APIs.
- Remaining package/deployment strings in `docs/superpowers/plans/2026-08-17-clean-slate-cleanup.md` are the current cleanup instructions themselves and were left as task source-of-truth.

### Fix-round concerns

- Docker is unavailable in this environment, so PostgreSQL race tests and Docker-based Caddy runtime-asset tests cannot complete here.
- The broad backend suite was intentionally not retried after the Docker blocker because the user instructed not to wait for Docker.
- Spark-agent runtime/helper package wire names remain by design where concrete retained agent consumers still exist.
