# Task 6 report — reconcile Fleet/Library with recipe v1

## Result

Completed the v1 one-topology reconciliation. Library and Fleet now use
`topology_name`; mapping preview/apply derive topology solely from the immutable
recipe revision. The Library visual contract presents exact model-version,
execution-harness, runtime-distribution, optional patch-bundle, interfaces, and
topology lifecycle order. The prototype `runtime.adapter` and deployment-profile
selection contract are absent from active Library/Fleet UI and API payloads.

Lock-first, same-transaction admission behavior and topology-defined lifecycle
phase ordering were preserved; no Task 5 compiler/runtime-spec findings or Task
7 migration work was implemented.

## Changed files

Backend and backend tests:

- `control/src/vonk_control/library_contract.py`
- `control/src/vonk_control/library_projection.py`
- `control/src/vonk_control/fleet_projection.py`
- `control/tests/test_library_projection.py`
- `control/tests/test_library_api.py`
- `control/tests/test_fleet_projection.py`
- `tests/control/test_openapi_clients.py`

Web client, views, fixtures, and tests:

- `control/web/e2e/fleet-library.spec.ts`
- `control/web/src/api/client.ts`
- `control/web/src/api/client.test.ts`
- `control/web/src/api/generated.d.ts`
- `control/web/src/api/types.ts`
- `control/web/src/components/library-action-preview.tsx`
- `control/web/src/components/library-actions.test.tsx`
- `control/web/src/components/library-browser.tsx`
- `control/web/src/components/library-placement.tsx`
- `control/web/src/components/library-recipe-advanced.test.tsx`
- `control/web/src/components/library-recipe-detail.tsx`
- `control/web/src/components/library-recipe-visual.tsx`
- `control/web/src/components/node-card.tsx`
- `control/web/src/components/node-card.test.tsx`
- `control/web/src/components/node-detail.tsx`
- `control/web/src/components/node-detail.test.tsx`
- `control/web/src/components/recipe-summary.tsx`
- `control/web/src/lib/fleet.test.ts`
- `control/web/src/lib/library-recipe-document.ts`
- `control/web/src/lib/library-recipe-document.test.ts`
- `control/web/src/pages/catalog.tsx`
- `control/web/src/pages/catalog.test.tsx`
- `control/web/src/pages/cluster-mapping.tsx`
- `control/web/src/pages/fleet.test.tsx`
- `control/web/src/pages/library.test.tsx`
- `control/web/src/pages/recipe-editor.tsx`
- `control/web/src/pages/recipe-editor.test.tsx`
- `control/web/src/test-fixtures/library.ts`

Generated artifacts:

- `control/openapi.json`
- `src/cluster_profiles/generated_control/models/__init__.py`
- Updated generated models: `library_recipe_detail.py`,
  `library_recipe_summary.py`, `mapping_plan_response.py`,
  `mapping_preview_input.py`, `mapping_preview_request.py`,
  `mapping_request.py`, `operational_mapping.py`,
  `placement_recommendation.py`, `recipe_presence.py`,
  `recipe_revision_response.py`, `recipe_summary_response.py`,
  `visual_recipe_document.py`, and `visual_runtime.py`.
- Removed generated legacy models: `profile_placement.py`, `recipe_profile.py`,
  `recipe_profile_measurement.py`, `recipe_profile_parameter_overrides.py`,
  `recipe_profile_summary.py`, `recipe_profile_summary_fabric_connectivity.py`,
  and `visual_workload.py`.
- Added generated v1 models: `recipe_topology.py`, `topology_placement.py`,
  `visual_catalog_identity.py`, `visual_catalog_identity_kind.py`,
  `visual_execution.py`, and `visual_interface.py`.
- Added generator-required catalog entity API/models:
  `api/default/{create_catalog_entity_draft,get_catalog_entity,list_catalog_entities,resolve_catalog_entity,revise_catalog_entity}.py`
  and `models/{catalog_entity_list_response,catalog_entity_revision_response,catalog_entity_revision_response_document,catalog_entity_revision_response_kind,catalog_entity_revision_response_lifecycle,create_catalog_entity_request,create_catalog_entity_request_document,resolve_catalog_entity_request,revise_catalog_entity_request,revise_catalog_entity_request_document}.py`.

## TDD evidence

Added the one-topology mapping-input test before changing the contract, then ran:

```text
uv run --project control --frozen python -m pytest control/tests/test_library_projection.py::test_mapping_preview_input_derives_one_topology_from_the_recipe_revision -q
```

Initial result: `1 failed`; `MappingPreviewInput` still required
`profile_name`. After the contract change: `1 passed in 0.33s`.

During self-review, added the job-interface regression before the fix:

```text
uv run --project control --frozen python -m pytest control/tests/test_library_projection.py::test_visual_projection_keeps_non_openai_interface_path_without_endpoint_fields -q
```

Initial result: `1 failed`, `KeyError: 'port'`. After the optional strict
interface projection fix: `1 passed in 0.45s`.

## Verification

```text
scripts/generate-control-clients
```

Output: generated `src/cluster_profiles/generated_control`, regenerated
`control/openapi.json`, and regenerated `control/web/src/api/generated.d.ts`.

```text
uvx ruff@0.16.1 check control/src/vonk_control/library_contract.py control/src/vonk_control/library_projection.py control/src/vonk_control/fleet_projection.py control/tests/test_library_projection.py control/tests/test_library_api.py control/tests/test_fleet_projection.py tests/control/test_openapi_clients.py
git diff --check
```

Output: `All checks passed!`; no whitespace errors.

```text
uv run --project control --frozen python -m pytest control/tests/test_library_projection.py control/tests/test_library_api.py control/tests/test_fleet_projection.py control/tests/test_recipe_api.py -q
```

Output: `73 passed in 5.66s`.

```text
uv run --project control --frozen python -m pytest tests/control/test_openapi_clients.py -q
```

Output: `11 passed in 6.12s`.

```text
uv run --project control --frozen python -m pytest control/tests/test_recipe_operations.py control/tests/test_install_admission.py control/tests/test_run_admission.py control/tests/test_recipe_routes.py control/tests/test_admin_api.py -q
```

Output: `95 passed in 24.78s`.

```text
npm test --prefix control/web -- --run
npm run build --prefix control/web
```

Output: `163 passed | 1 skipped (164)`; production TypeScript/Vite build
succeeded.

```text
npm run test:e2e --prefix control/web -- e2e/fleet-library.spec.ts --list
```

Output: Playwright discovered all 6 Fleet/Library browser tests successfully.

## Self-review

- Confirmed mapping request payloads contain recipe revision, node IDs, and
  parameters only; the topology comes from the revision.
- Confirmed Fleet, Library, placement, catalog, and mapping UI use
  `topology_name` and no longer offer a profile selector.
- Confirmed generated OpenAPI/TypeScript/Python contract assertions reject
  `profile_name`, legacy `profiles`, visual `workload`, and runtime `adapter`.
- Found and fixed the non-OpenAI interface edge case: strict job interfaces
  only require `adapter` and `path`, so visual port, aliases, and health path
  are optional and are displayed without loss.
- Confirmed topology start/stop order is projected and rendered; no admission
  implementation was changed.

## Concerns

- The Playwright browser execution cannot launch in this environment because
  the installed Chromium headless shell lacks `libnspr4.so`; all six tests fail
  before opening a page. Test discovery, web unit tests, and production build
  pass.
- A combined Python run once showed two transient infrastructure failures
  (a SQLAlchemy listener unexpectedly became a boolean and an
  `openapi-python-client` subprocess segfaulted). Each minimal reproducer and
  the final focused suites passed on rerun. No deterministic Task 6 failure was
  found.

---

## Review fix round 1/5

### Result

Resolved all four review findings. Library model groups and routes are now
keyed and displayed by the full immutable `publisher/slug@content_sha256`
model-version identity. The editor produces a valid multi-node entrypoint plus
worker topology. Run targets are only projected for recipes exposing an OpenAI
interface, and `RecipeRevisionSummary.schema_version` is literal v1 with
fail-closed projection for any stored non-v1 revision.

### Changed files

- `control/src/vonk_control/library_contract.py`
- `control/src/vonk_control/library_projection.py`
- `control/tests/test_library_projection.py`
- `tests/control/test_openapi_clients.py`
- `control/openapi.json`
- `src/cluster_profiles/generated_control/models/__init__.py`
- `src/cluster_profiles/generated_control/models/library_model.py`
- `src/cluster_profiles/generated_control/models/model_version_identity.py`
- `src/cluster_profiles/generated_control/models/recipe_revision_summary.py`
- `control/web/src/api/generated.d.ts`
- `control/web/src/components/library-browser.tsx`
- `control/web/src/components/library-recipe-detail.tsx`
- `control/web/src/lib/library-route.ts`
- `control/web/src/pages/library.tsx`
- `control/web/src/pages/library.test.tsx`
- `control/web/src/pages/recipe-editor.tsx`
- `control/web/src/pages/recipe-editor.test.tsx`
- `control/web/src/test-fixtures/library.ts`
- `control/web/e2e/fleet-library.spec.ts`

### TDD evidence

Added the collision, non-v1 revision, job-only action, and multi-node editor
tests before implementation.

```text
uv run --project control --frozen python -m pytest control/tests/test_library_projection.py::test_root_separates_same_slug_model_versions_by_portable_identity control/tests/test_library_projection.py::test_non_v1_revision_schema_fails_closed_in_library_projection control/tests/test_library_projection.py::test_job_only_recipe_does_not_offer_an_openai_load_action -q
```

Initial output: `FFF`; failures respectively showed no `LibraryModel.model`,
the invalid schema still appeared in a Library model group, and preview targets
were `mapping`, `install`, `run` rather than `mapping`, `install`.

```text
npm test --prefix control/web -- --run src/pages/recipe-editor.test.tsx
```

Initial output: `1 failed | 2 passed`; the emitted entrypoint role had
`count: 2` instead of `count: 1` with a worker role.

After implementation:

```text
uv run --project control --frozen python -m pytest control/tests/test_library_projection.py::test_root_separates_same_slug_model_versions_by_portable_identity control/tests/test_library_projection.py::test_non_v1_revision_schema_fails_closed_in_library_projection control/tests/test_library_projection.py::test_job_only_recipe_does_not_offer_an_openai_load_action -q
```

Output: `3 passed in 0.61s`.

```text
npm test --prefix control/web -- --run src/pages/recipe-editor.test.tsx
```

Output: `3 passed`.

### Generated artifacts

```text
./scripts/generate-control-clients
```

Output:

```text
Generating /home/carst/vonk-forge/.worktrees/execution-harness-foundation/src/cluster_profiles/generated_control
✨ openapi-typescript 7.13.0
🚀 /home/carst/vonk-forge/.worktrees/execution-harness-foundation/control/openapi.json → /home/carst/vonk-forge/.worktrees/execution-harness-foundation/control/web/src/api/generated.d.ts [165ms]
```

Regenerated `control/openapi.json`, Python generated client models (including
new `model_version_identity.py`), and `control/web/src/api/generated.d.ts`.

### Verification

```text
uv run --project control --frozen python -m pytest control/tests/test_library_projection.py control/tests/test_library_api.py tests/control/test_openapi_clients.py -q
```

Output: `67 passed in 11.19s`.

```text
npm test --prefix control/web -- --run src/pages/library.test.tsx src/pages/recipe-editor.test.tsx src/components/library-actions.test.tsx src/components/library-recipe-advanced.test.tsx
```

Output: `Test Files 4 passed (4)` and `Tests 24 passed (24)`.

```text
npm run build --prefix control/web
```

Output: `tsc --noEmit` passed and Vite built successfully (`69 modules transformed`).

```text
npm run test:e2e --prefix control/web -- --list
```

Output: `Total: 9 tests in 2 files`.

```text
uvx ruff@0.16.1 check control/src/vonk_control/library_contract.py control/src/vonk_control/library_projection.py control/tests/test_library_projection.py tests/control/test_openapi_clients.py
git diff --check
```

Output: `All checks passed!`; no whitespace errors.

Required controller browser command (with the prepared `LD_LIBRARY_PATH`
prefix when needed):

```text
npm run test:e2e --prefix control/web -- e2e/fleet-library.spec.ts
```

### Self-review

- Confirmed exact model-version groups differ when publisher or digest differs,
  and every browser key, route, merge, selection, label, and E2E assertion uses
  that complete portable identity rather than `family`.
- Confirmed the multi-node editor emits one endpoint-owning entrypoint rank,
  worker ranks for the remainder, complete artifact coverage, valid topology
  names, and reversed stop order.
- Confirmed job-only interfaces cannot yield a `run` preview target; the UI has
  no Load action without that server-provided target and now chooses aliases
  only from an OpenAI interface.
- Confirmed OpenAPI, TypeScript, and Python generated contracts expose
  literal `schema_version: 1`; projection unlinks non-v1 revisions and omits
  their selected revision/visual detail.
- Preserved lock-first admission and topology-ordered lifecycle code paths;
  no Task 5 or Task 7 scope was changed.

### Concerns

- Focused unit, discovery, and build verification pass. Full Playwright browser
  execution remains for the controller with its prepared non-root Chromium
  library path; this environment's known `libnspr4`/`libnss` issue was not
  changed in code. No deterministic transient-infrastructure cause was found,
  so no infrastructure code change was made.

### Controller browser verification

The browser verification finding is addressed.

```text
LD_LIBRARY_PATH=/tmp/vonk-playwright-libs.3oQg9j/root/usr/lib/x86_64-linux-gnu npm run test:e2e --prefix control/web -- e2e/fleet-library.spec.ts
```

Output: `Running 6 tests using 1 worker`; all six named Fleet/Library tests
passed; final `6 passed (4.8s)`.

Warnings only: Node reported that `NO_COLOR` was ignored because `FORCE_COLOR`
is set. This is harness color-environment noise, not an application failure.
