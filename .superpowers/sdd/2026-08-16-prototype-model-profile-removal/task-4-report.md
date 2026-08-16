# Task 4 report — remove profile reconciliation authority

## Result

Implemented the v1 production/web cutover and committed it as:

`refactor: remove profile reconciliation authority`

The control API no longer exposes profile planning, reconciliation plan/apply/cancel, or the legacy document editor/listing route. The authorization matrix, operation metadata, generated OpenAPI, generated Python client, and generated TypeScript client were regenerated accordingly.

The profile desired-state resolver, acceptance layer, legacy runtime handlers, and legacy route runtime were removed. The retained reconciliation plan value object is used only by current package/deployment internals. Platform update workload safety now projects running v1 `RecipeRun`/`RunNode` state instead of the removed desired-state projection. The dashboard no longer reports active profile assignments.

The web application now keeps model work in Library/Catalog and removes Profiles, Models, repository editor, and reconciliation-plan UI surfaces. The obsolete browser equivalence fixture was reduced to an explicit skipped retirement marker because its tested flow no longer exists.

## Verification

- `scripts/generate-control-clients` — passed.
- `npm test -- --run` in `control/web` — 149 passed, 1 skipped.
- `npm run build` in `control/web` — passed.
- Focused control tests — 97 passed, 1 skipped:
  `test_reconcile.py`, `test_dashboard.py`, `test_update_admin_service.py`,
  `test_operation_api.py`, `test_admin_api.py`, `test_orchestration.py`, and
  `control/tests/security`.
- `python -m compileall -q control/src/vonk_control control/tests` — passed.
- `git diff --check` — passed.

## Broader-suite concerns

The initial `uv run --project control --frozen python -m pytest control/tests -q` run completed with 2343 passed, 1 skipped, and 14 failures. The profile-related dashboard assertion was updated and passes in the focused rerun; the remaining failures are outside the bounded Task 4 cutover:

- two agent-job tests construct the older recipe-build payload and omit the now-required `base_images` and `base_image_storage_bytes` fields;
- two migration tests expect the old reversible migration path to accept populated mutable state, while the v1 pre-production migration intentionally requires a fresh state;
- one dashboard assertion expected the removed `profile` field (fixed in Task 4 tests; the recorded full-suite result predates that focused rerun);
- package action/service tests depend on legacy root package/deployment fixture documents that are absent from the current v1 repository tree;
- one PostgreSQL migration test likewise attempts to advance a populated database through the fresh-state v1 catalog migration.

The repository’s required Ruff version is `0.16.1`, while the available frozen environment provided `0.13.3`; Ruff therefore refused to run. Compilation, focused tests, web type-check/build, and diff checks passed.

No root tests, configuration, adapters, or documentation were changed. Existing unrelated shared-worktree changes were preserved and excluded from the focused commit.
