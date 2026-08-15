# Task 7B Report — Digest-bound recipe run alias

## Outcome

Implemented the bounded run-alias authority correction on
`work/control-plane-frontend-ux`, based on clean HEAD
`61b9cf8e8ebce4a99d0f5f2c34ef09549e14b68f`.

The operator-selected route alias is now required at run preview, carried by the
immutable `RunPlan`, included in canonical plan identity before hashing, returned
by the preview API, and persisted in `RecipeRun.plan` alongside the existing
`RecipeRun.alias` column. Apply recomputes the preview with the submitted alias,
and downstream admission and agent-job construction consume only `RunPlan.alias`.

## Authority and idempotency changes

- Added the validated alias to `RunPreviewRequest`, `RunPlan`, and
  `RunPlanResponse`.
- Included `alias` in the canonical run-plan identity before SHA-256 hashing, so
  two valid aliases for the same installation produce different plan digests.
- Recomputed apply authority with `body.installation_id` and `body.alias` before
  calling start.
- Removed redundant internal alias parameters from
  `RecipeOperationService.start`, `RunAdmissionService.accept_run`, and
  `RunAdmissionService.accept_run_in_session`.
- Moved run digest equality validation ahead of idempotent lookup. Exact replay
  still returns the original operation; an alias-B plan submitted with alias-A's
  digest is rejected before run, reservation, parent job, agent operation, or
  route side effects.
- Persisted the same `RunPlan.alias` in the run alias column, immutable plan
  document, and existing agent operation authority payload. The parent job and
  operation response continue to expose the alias-bound plan digest without
  changing their payload shape.
- Preserved installation locking, mapping and inventory fences, memory/fabric
  admission, resource reservation semantics, operation/audit payloads, and
  request-key ownership.

## Caller and contract changes

- Updated `scripts/run-development-slices` so every initial, retry, stale-plan,
  and interrupted-request recovery path previews and applies the same slug alias.
- Updated development-slice fixtures to require and echo the preview alias.
- Updated the handwritten web action flow to preview with the selected alias and
  apply the alias returned by the accepted preview plan. Alias changes trigger a
  fresh preview.
- Updated recipe operation/API, admission, projection, caller, web wrapper, and
  generated-contract tests.
- Regenerated `control/openapi.json`, the Python client, and TypeScript schema
  types exclusively with `scripts/generate-control-clients`.

## Strict TDD evidence

Before production changes, focused tests were added and observed failing for the
missing authority behavior:

- run admission rejected the new `alias` planning argument;
- recipe operation preview rejected the alias argument;
- API preview with alias returned HTTP 422;
- the development-slice test server rejected the alias-less preview with HTTP
  422;
- generated OpenAPI lacked alias in `RunPreviewRequest.required`;
- the web Load action preview omitted alias.

After the minimal production and caller changes, the same focused tests and their
complete relevant files passed.

## Verification

- `uv run --project control --frozen --with-editable . pytest tests/control/test_openapi_clients.py -q`
  — 10 passed; generator idempotency and Python/TypeScript contracts verified.
- `uv run --project control --frozen --with-editable . pytest control/tests/test_run_admission.py control/tests/test_recipe_operations.py control/tests/test_recipe_api.py control/tests/test_library_projection.py -q`
  — 100 passed, 6 skipped.
- `uv run --frozen pytest scripts/tests/test_run_development_slices.py -q`
  — 54 passed.
- `npm test -- --run` in `control/web`
  — 141 passed, 1 skipped across 28 test files.
- `npm run build` in `control/web`
  — TypeScript typecheck and Vite production build passed.
- `uvx --from ruff==0.16.1 ruff check --force-exclude ...`
  — all changed handwritten Python files passed pinned Ruff 0.16.1.
- Python built-in compilation over control source/tests, the development-slice
  caller/tests, generated Python client, and client contract tests
  — 636 files compiled successfully.
- `git diff --check` — passed with no whitespace errors.

No Rust, migrations/database schema, dependencies, live systems, MIA recipe
documents, runtime behavior, or readiness behavior were changed.

## Warnings and concerns

The repository generator continues to emit 12 known literal-guard
`SyntaxWarning: invalid decimal literal` warnings while compiling generated
models. They occur in the pre-existing generated files `fleet_snapshot.py`,
`library_recipe_detail.py`, `library_snapshot.py`, `placement_limits.py`, and
`telemetry_history_response.py`. Generated output was not hand-edited.

Relevant pytest runs also emitted macOS temporary-directory cleanup warnings for
stale `test_stage_runtime_secrets_rej*` garbage directories. These warnings were
outside the changed code and did not cause test failures.
