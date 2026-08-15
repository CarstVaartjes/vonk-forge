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

## Fix round 1 — exact replay preflight

Frontend review found that the API recomputed run admission before consulting
operation idempotency. A successful first start consumes reservations, so that
recomputation can no longer reproduce an allowed plan and an otherwise exact API
replay could incorrectly conflict.

Implementation commit:
`de1d9ab8d08f5964d0f8c01dd4992739e77b3adc` (`fix(control): preflight exact run replays`).

The fix adds a read-only `replay_start` preflight. It returns the persisted
operation only when the request key resolves to a `recipe.start` job and the
submitted installation, alias, and plan digest exactly match both that job's
authority payload and its persisted `RecipeRun`. The API uses that result before
repreviewing. Every missing or mismatched field returns to the existing fresh
preview and digest validation path, so alias/digest mismatches still fail before
request-key conflict handling, audit, reservations, jobs, agent operations,
queue notifications, or route side effects.

The development-slice authority fixture now records previewed installation/alias
authority and committed request-key installation/alias/digest authority. It
implements production ordering: exact committed replay first, then fresh
preview/digest validation, then generic request-key conflict, then creation.
Initial, retry, resumed, and interrupted-start caller tests assert preview/apply
alias parity.

### Round 1 RED evidence

The new tests were run before implementation and failed for the reviewed gaps:

- `test_start_rejects_alias_mismatched_digest_before_side_effects_and_replays_exactly`
  failed with `AttributeError` because `RecipeOperationService.replay_start` did
  not exist after the first admission had invalidated a fresh preview.
- `test_identical_start_api_replay_returns_original_without_repreview_or_audit`
  observed a second `preview_run`/`start` call instead of a read-only replay.
- `test_fake_run_authority_replays_only_exact_previewed_alias_and_digest`
  observed HTTP 202 for an alias-mismatched replay where HTTP 409 was required.

### Round 1 GREEN and verification evidence

- `uv run --project control --frozen --with-editable . pytest control/tests/test_recipe_operations.py -q`
  — 45 passed, 5 skipped.
- `uv run --project control --frozen --with-editable . pytest control/tests/test_recipe_api.py -q`
  — 7 passed.
- `uv run --frozen pytest scripts/tests/test_run_development_slices.py -q`
  — 55 passed.
- `uvx --from ruff==0.16.1 ruff check --force-exclude` over all five changed
  handwritten Python files — all checks passed.
- `uv run --project control --frozen python -m py_compile` over all five changed
  Python files — passed.
- `git diff --check` — passed with no whitespace errors.

The pytest runs retained the previously documented macOS temporary-directory
cleanup warnings; all test processes exited successfully. No generated
contracts changed in this review round, and no excluded scope was touched.
