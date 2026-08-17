# Recipe Maintenance UI Alignment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the v1 control plane’s Library/Catalog recipe workflow the only maintained frontend path while preserving the redesigned visual experience and removing obsolete profile/model administration.

**Architecture:** Library remains the operational read/action surface; Catalog and its visual editor remain the recipe authoring and lifecycle surface. The backend remains the authority for recipe identity, revisions, evidence, placement, and activation; frontend changes only route presentation and generated-client usage. Legacy profile/model/reconciliation presentation and API routes are removed where no v1 consumer needs them.

**Tech Stack:** FastAPI, SQLAlchemy, OpenAPI-generated TypeScript, React, Vitest, Testing Library, Playwright, pytest, Ruff.

## Global Constraints

- `Library` is the default place to understand model families, accepted recipe revisions, cluster placement, runtime state, and activation or recovery actions.
- `Catalog` is the maintenance workspace for creating, importing, revising, resolving, evidencing, and mapping recipes.
- Canonical recipe JSON remains an explicitly labelled advanced editor and is not a second authority.
- Recipe identity, revision, content digests, evidence, cluster mapping, and activation continue to come from v1 control APIs.
- The frontend retains the redesigned dark-green/mint responsive visual system, status pills, cards, freshness indicators, loading/error/empty states, and keyboard-visible focus.
- Legacy Profiles, Models, repository-editor, and profile reconciliation surfaces are not exposed in v1 navigation or API documentation.
- Do not remove shared proposal, audit, package, deployment, update, fleet, agent, or recipe-route APIs still consumed by v1.

---

### Task 1: Remove legacy backend authority and routes

**Files:**
- Delete: `control/src/vonk_control/desired_state.py`
- Delete: `control/src/vonk_control/acceptance.py`
- Delete: `control/src/vonk_control/legacy_runtime.py`
- Delete: `control/src/vonk_control/legacy_route_runtime.py`
- Modify: `control/src/vonk_control/reconcile.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/auth.py`
- Modify: `control/src/vonk_control/repository.py`
- Modify: `control/src/vonk_control/serializers.py`
- Test: `control/tests/test_operation_api.py`, `control/tests/test_reconcile.py`, `control/tests/test_auth.py`, `control/tests/test_repository.py`

**Interfaces:**
- Consumes: current recipe catalog, Library, recipe-route, package, deployment, update, and agent APIs.
- Produces: an API module that no longer imports or constructs profile compatibility/planning authorities; retained `ReconciliationPlan` helpers remain available to package rollouts and current v1 route/run code.

- [ ] **Step 1: Write/extend absence tests** asserting that profile plan, profile reconciliation, document editor, and cancellation routes are absent while current catalog, Library, package, deployment, update, fleet, agent, and recipe-route routes remain present.
- [ ] **Step 2: Run the focused API and auth tests** with `UV_CACHE_DIR=/tmp/vonk-ui-control-cache uv run --project control --frozen pytest control/tests/test_operation_api.py control/tests/test_auth.py control/tests/test_repository.py -q`; verify the new absence assertions fail only because legacy code still registers the routes.
- [ ] **Step 3: Remove the legacy imports, `AdminServices` fields, route handlers, role entries, and compatibility readers; reduce `reconcile.py` to `ChangeService`, `ReconciliationPlan`, and the helpers used by current package-rollout code.
- [ ] **Step 4: Run the focused suite again and run Ruff on changed Python files; verify retained recipe-route and package-rollout imports still resolve.
- [ ] **Step 5: Commit with `refactor: remove legacy profile control authority`.

### Task 2: Make the redesigned frontend the sole recipe workflow

**Files:**
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/components/app-shell.tsx`
- Modify: `control/web/src/pages/catalog.tsx`
- Modify: `control/web/src/pages/recipe-editor.tsx`
- Modify: `control/web/src/pages/library.tsx`
- Modify: `control/web/src/components/library-recipe-detail.tsx`
- Modify: `control/web/src/styles.css`, `control/web/src/pages/library.css`, `control/web/src/components/library-recipe-detail.css`
- Delete: `control/web/src/pages/profiles.tsx`, `control/web/src/pages/profiles.test.tsx`
- Delete: `control/web/src/pages/models.tsx`
- Delete: `control/web/src/components/repository-editor.tsx`, `control/web/src/components/repository-editor.test.tsx`
- Delete: `control/web/src/components/reconciliation-plan.tsx`, `control/web/src/components/reconciliation-plan.test.tsx`
- Test: `control/web/src/components/app-shell.test.tsx`, `control/web/src/pages/catalog.test.tsx`, `control/web/src/pages/library.test.tsx`, `control/web/src/pages/recipe-editor.test.tsx`

**Interfaces:**
- Consumes: the retained generated `CatalogApi`, `LibraryApi`, fleet, and recipe-route client methods.
- Produces: direct `/fleet`, `/library`, `/catalog`, `/catalog/:id`, `/catalog/:id/source`, `/catalog/:id/map`, and current activity/system routes with no Profiles/Models/editor dead ends.

- [ ] **Step 1: Add failing frontend assertions** for direct Library/Catalog navigation, no Profiles/Models links, the visual editor’s lifecycle actions, and advanced JSON disclosure.
- [ ] **Step 2: Run the focused Vitest tests** with `cd control/web && npm test -- --run src/components/app-shell.test.tsx src/pages/catalog.test.tsx src/pages/library.test.tsx src/pages/recipe-editor.test.tsx`; confirm failures identify remaining legacy navigation or missing lifecycle presentation.
- [ ] **Step 3: Remove old routes/components and refine the retained pages so the visual editor is the primary path, advanced JSON is clearly secondary, and Library shows accepted revision, runtime state, placement, evidence, and recovery actions in the redesigned card hierarchy.
- [ ] **Step 4: Exercise keyboard navigation, narrow viewport rendering, loading, empty, error, draft, resolved, and blocked states in tests; keep all labels and status text accessible.
- [ ] **Step 5: Run the focused frontend suite and commit with `refactor: make library and catalog the v1 recipe workflow`.

### Task 3: Regenerate and verify the API boundary

**Files:**
- Modify: `control/openapi.json`
- Modify: `control/web/src/api/generated.d.ts`
- Modify: `control/web/src/api/client.ts`, `control/web/src/api/types.ts`
- Modify: `src/cluster_profiles/generated_control/`
- Test: `control/tests/test_openapi.py`, `control/web/src/api/client.test.ts`, `control/web/src/api/generated.d.ts`

**Interfaces:**
- Consumes: the retained FastAPI route set from Task 1.
- Produces: generated OpenAPI and TypeScript artifacts that omit removed legacy endpoints and preserve current v1 catalog/Library methods.

- [ ] **Step 1: Add a generated-surface regression assertion** that removed profile/reconciliation/document paths are absent and retained catalog/Library paths are present.
- [ ] **Step 2: Run the generator used by this repository (`scripts/generate-control-clients`) and inspect the diff for unrelated endpoint churn.
- [ ] **Step 3: Update only handwritten client adapters/types required by the generated boundary, preserving request/response names used by Library and Catalog.
- [ ] **Step 4: Run backend OpenAPI tests and frontend client tests, then run TypeScript typecheck/build.
- [ ] **Step 5: Commit with `build: regenerate v1 control clients`.

### Task 4: Full UX and integration verification

**Files:**
- Modify: `control/web/e2e/admin.spec.ts`, `control/web/e2e/fleet-library.spec.ts` only if assertions need current v1 wording.
- Modify: `docs/superpowers/specs/2026-08-16-recipe-maintenance-ui-alignment-design.md` only if verified behavior requires clarification.

**Interfaces:**
- Consumes: completed backend, frontend, and generated-client changes.
- Produces: evidence that a fresh operator can reach Fleet, inspect Library, create/import a recipe in Catalog, and follow the immutable lifecycle without legacy routes.

- [ ] **Step 1: Run the control backend focused suite and the full control suite with the repository’s frozen uv commands.
- [ ] **Step 2: Run the frontend unit/typecheck/build suite and the existing Playwright admin/Fleet/Library suites.
- [ ] **Step 3: Search the repository for removed route names, imports, legacy command names, and old profile/model authority; allow only historical design/report references where they are explicitly labelled.
- [ ] **Step 4: Review the rendered screenshots or Playwright traces at desktop and mobile widths for clipping, inaccessible actions, and visual regressions.
- [ ] **Step 5: Commit documentation/test-only fixes and report exact verification results before integration.
