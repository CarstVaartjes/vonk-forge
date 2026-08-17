# Clean-slate control-plane cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the superseded control-plane pages, Spark Git roster, and old package/deployment pipeline so Fleet and Library are the only supported product surfaces.

**Architecture:** PostgreSQL `AgentNode` registration becomes the source set for Fleet. The old Git roster and package/deployment projection pipeline are deleted rather than migrated. The cleanup leaves only the current Fleet/Library entry points and the secure primitives they still consume; the richer unified Fleet/Library workflows are implemented after this cleanup pass.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, PostgreSQL, React/TypeScript, Vitest, pytest, Git, TOML repository documents, Rust/Python Spark agent packaging.

## Global Constraints

- PostgreSQL is the management authority for the live Spark roster.
- A fresh/reset database with no non-revoked enrolled `AgentNode` records renders zero Sparks.
- Each successfully enrolled, non-revoked Spark appears exactly once, regardless of whether a Git repository record exists.
- Add Spark generates host bootstrap inputs and never requires manual editing of the agent runtime TOML.
- This is a direct replacement, not a migration; do not add upgrade/adaptation code, compatibility shims, redirect routes, legacy schema readers, data translators, dual-write paths, or old API aliases. The replacement schema is created by one fresh baseline only.
- Delete the old package/deployment pipeline directly, including its artifacts, services, routes, tests, and documentation.
- Keep only toolchain/project manifests and generated local runtime TOML files that the new system genuinely requires.

---

### Task 1: Remove superseded navigation and page routes

**Files:**
- Modify: `control/web/src/components/app-shell.tsx`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/components/app-shell.test.tsx`
- Modify: `control/web/src/pages/library.tsx`
- Delete: `control/web/src/pages/agents.tsx`
- Delete: `control/web/src/pages/agents.test.tsx`
- Delete: `control/web/src/pages/catalog.tsx`
- Delete: `control/web/src/pages/catalog.test.tsx`
- Delete: `control/web/src/pages/recipe-editor.tsx`
- Delete: `control/web/src/pages/recipe-editor.test.tsx`
- Delete: `control/web/src/pages/recipe-source.tsx`
- Delete: `control/web/src/pages/workload-run-import.tsx`
- Delete: `control/web/src/pages/workload-run-import.test.tsx`
- Delete: `control/web/src/pages/cluster-mapping.tsx`
- Delete: `control/web/src/pages/packages.tsx`
- Delete: `control/web/src/pages/packages.test.tsx`
- Delete: `control/web/src/pages/package-candidate.tsx`
- Delete: `control/web/src/pages/package-types.ts`
- Delete: `control/web/src/pages/deployments.tsx`
- Delete: `control/web/src/pages/deployments.test.tsx`
- Delete: `control/web/src/pages/updates.tsx`
- Delete: `control/web/src/pages/updates.test.tsx`
- Delete: `control/web/src/pages/jobs.tsx`
- Delete: `control/web/src/pages/jobs.test.tsx`
- Delete: `control/web/src/pages/audit.tsx`

**Interfaces:**
- Produces an application shell whose primary routes are only `/fleet` and `/library`.
- Existing Fleet and Library pages remain the only rendered page components in `App`.

- [ ] **Step 1: Add the failing navigation contract test**

Extend `control/web/src/components/app-shell.test.tsx` with a real rendered shell assertion:

```tsx
test("exposes only Fleet and Library as primary navigation", () => {
  render(<AppShell activeRoute="fleet" onNavigate={() => undefined}>{null}</AppShell>);
  expect(screen.getByRole("link", {name: "Fleet"})).toBeVisible();
  expect(screen.getByRole("link", {name: "Library"})).toBeVisible();
  expect(screen.queryByText("Agents")).not.toBeInTheDocument();
  expect(screen.queryByText("Catalog")).not.toBeInTheDocument();
  expect(screen.queryByText("Packages")).not.toBeInTheDocument();
  expect(screen.queryByText("Deployments")).not.toBeInTheDocument();
  expect(screen.queryByText("Updates")).not.toBeInTheDocument();
  expect(screen.queryByText("Jobs")).not.toBeInTheDocument();
});
```

- [ ] **Step 2: Run the focused test and verify it fails**

Run: `cd control/web && npm test -- --run src/components/app-shell.test.tsx`

Expected: FAIL because the current shell still renders Activity/System groups and their legacy links.

- [ ] **Step 3: Remove the legacy route declarations and imports**

Delete the `activityRoutes` and `systemRoutes` groups, reduce `AppRoute` to `"fleet" | "library"`, remove legacy page imports and render branches, and remove `/catalog`, `/packages`, and other legacy links from Library empty/detail states.

- [ ] **Step 4: Delete the superseded page modules and tests**

Remove the files listed above. Keep reusable Library components only when `rg -n` proves they are imported by `control/web/src/pages/library.tsx` or its retained child components.

- [ ] **Step 5: Run the web test suite**

Run: `cd control/web && npm test -- --run`

Expected: PASS with no imports or route references to the deleted pages.

- [ ] **Step 6: Commit the navigation cleanup**

```bash
git add control/web/src
git commit -m "refactor: reduce control plane to Fleet and Library"
```

### Task 2: Move audit access into the authenticated user menu

**Files:**
- Create: `control/web/src/components/admin-menu.tsx`
- Create: `control/web/src/components/admin-menu.test.tsx`
- Modify: `control/web/src/components/app-shell.tsx`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/api/types.ts`
- Modify: `control/web/src/api/client.ts`

**Interfaces:**
- Consumes: `ControlApi.audit(): Promise<{events: AuditSummary[]}>`.
- Produces: A compact authenticated user menu with `Audit log` and `Logout`; audit is rendered as a small popover/drawer rather than a primary route.

- [ ] **Step 1: Write the failing component test**

Add a test that renders the menu with a complete audit response and verifies the user can open `Audit log`, see an action and actor, and close the panel without navigating away from Fleet.

- [ ] **Step 2: Run the test and verify it fails**

Run: `cd control/web && npm test -- --run src/components/admin-menu.test.tsx`

Expected: FAIL because `AdminMenu` does not exist.

- [ ] **Step 3: Implement the minimal menu and audit panel**

Keep the existing authenticated operator identity and logout behavior. Add an accessible button for the operator identity, a compact panel, bounded audit rendering, loading/error states, and a close control. Do not create an empty Settings route; expose Settings only after a real setting exists.

- [ ] **Step 4: Run focused and shell tests**

Run: `cd control/web && npm test -- --run src/components/admin-menu.test.tsx src/components/app-shell.test.tsx`

Expected: PASS.

- [ ] **Step 5: Commit the admin menu**

```bash
git add control/web/src
git commit -m "feat: put audit access in the operator menu"
```

### Task 3: Make Fleet PostgreSQL-backed and remove the Git roster dependency

**Files:**
- Modify: `control/src/vonk_control/fleet_projection.py`
- Modify: `control/src/vonk_control/dashboard.py`
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/tests/test_fleet_projection.py`
- Modify: `control/tests/test_operation_api.py`
- Delete: `inventory/fleet.toml`
- Delete: `control/tests/fixtures/fleet/generic.toml` if no retained loader consumes it
- Modify/Delete: tests and onboarding documents that assert the Git Spark roster

**Interfaces:**
- Consumes: PostgreSQL `AgentNode`, `AgentCertificate`, `AgentPresence`, `NodeInventorySnapshot`, observations, and retained workload state.
- Produces: Fleet snapshots whose node IDs come from non-revoked PostgreSQL registrations, with PostgreSQL profile defaults for display metadata.

- [ ] **Step 1: Add failing projection tests**

Add real database tests for these literals:

```python
def test_fleet_is_empty_when_repository_has_old_nodes_but_database_has_no_agents():
    assert projection.read().nodes == []

def test_fleet_contains_registered_node_absent_from_repository():
    snapshot = projection.read()
    assert [node.id for node in snapshot.nodes] == ["spk_" + "1" * 32]
    assert snapshot.nodes[0].display_name == "spk_" + "1" * 32

def test_fleet_excludes_revoked_agent_nodes():
    assert [node.id for node in projection.read().nodes] == []
```

- [ ] **Step 2: Run the focused tests and verify the old behavior fails**

Run: `pytest -q control/tests/test_fleet_projection.py`

Expected: FAIL because the current projection starts from `inventory/fleet.toml` and does not include unlisted registrations.

- [ ] **Step 3: Add the PostgreSQL profile and query registered nodes first**

Add the bounded mutable node profile, select non-revoked `AgentNode` rows as the Fleet root, and use node ID/empty hostname/`managed`/empty-label defaults when no profile exists. Remove all reads of `inventory/fleet.toml` from Fleet and dashboard evidence.

- [ ] **Step 4: Run the projection and API evidence tests**

Run: `pytest -q control/tests/test_fleet_projection.py control/tests/test_operation_api.py`

Expected: PASS, including zero-node reset behavior and a registered node absent from any repository document.

- [ ] **Step 5: Remove the Git roster fixtures and onboarding contract**

Delete the old roster file and update every test/runbook/proposal contract found by:

```bash
rg -n "inventory/fleet\.toml|repository-defined Fleet|fleet document" control docs scripts tests
```

Each remaining match must either be deleted or describe a historical decision outside the supported source tree; no runtime or operator workflow may depend on it.

- [ ] **Step 6: Commit the PostgreSQL Fleet cleanup**

```bash
git add control inventory docs scripts tests
git commit -m "refactor: make PostgreSQL authoritative for Fleet"
```

### Task 4: Delete the old package/deployment pipeline

**Files:**
- Delete: `control/src/vonk_control/package_api.py`
- Delete: `control/src/vonk_control/package_compatibility.py`
- Delete: `control/src/vonk_control/package_discovery.py`
- Delete: `control/src/vonk_control/package_providers.py`
- Delete: `control/src/vonk_control/package_publication.py`
- Delete: `control/src/vonk_control/package_resolution.py`
- Delete: `control/src/vonk_control/package_rollout_worker.py`
- Delete: `control/src/vonk_control/package_rollouts.py`
- Delete: `control/src/vonk_control/package_services.py`
- Delete: `control/src/vonk_control/package_validation.py`
- Delete: `control/src/vonk_control/package_validation_runner.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/operation_api.py`
- Modify: `control/src/vonk_control/worker.py`
- Modify: `control/src/vonk_control/dashboard.py`
- Modify: `control/src/vonk_control/metrics.py`
- Modify: `control/src/vonk_control/models.py`
- Delete: old package/deployment model classes and their fresh-schema definitions
- Delete: `control/migrations/versions/0001_operational_state.py` through `control/migrations/versions/0027_execution_harness_catalog.py`
- Create: `control/migrations/versions/0001_fleet_library_baseline.py`
- Delete: old package/deployment route and worker tests
- Delete: any tracked files under `config/package-families/` and `config/workload-deployments/`; the cleanup must leave both artifact directories absent or empty

**Interfaces:**
- Produces: No `/api/v1/packages/*`, `/api/v1/deployments/*`, or package/deployment operation IDs.
- Preserves: agent-side signed artifact transfer primitives only when a retained Fleet/Library consumer proves they are needed; names and contracts are rewritten rather than compatibility-wrapped.

- [ ] **Step 1: Add a failing negative route contract**

Add an API test to `control/tests/test_api.py` that builds the application and asserts these paths are not registered:

```python
client, _, _, _ = _client("administrator")
legacy_paths = {
    route.path
    for route in client.app.routes
    if route.path.startswith("/api/v1/packages/") or route.path.startswith("/api/v1/deployments")
}
assert legacy_paths == set()
```

- [ ] **Step 2: Run the negative contract and dependency search**

Run: `pytest -q control/tests/test_api.py -k legacy` and `rg -n "package_api|package_services|package_rollout|PackageCandidate|PackageRollout|/api/v1/packages|/api/v1/deployments" control/src control/tests control/web/src`

Expected: the new test fails and the search identifies every remaining consumer before deletion.

- [ ] **Step 3: Remove route installation and worker wiring**

Delete package/deployment imports, service construction, worker loops, dashboard package summaries, metrics snapshots, operation IDs, and authentication special cases. Remove only helpers that have no retained Fleet/Library or Spark-agent consumer.

- [ ] **Step 4: Remove old models and create the fresh baseline**

Delete package candidates, resolutions, validations, rollouts, rollout nodes, observations, action plans, and package-family/deployment authority tables from the retained SQLAlchemy model set. Replace the existing migration chain with `0001_fleet_library_baseline.py` generated from the final retained models. The baseline creates only the new Fleet/Library schema; it does not upgrade, translate, or preserve old database rows.

- [ ] **Step 5: Run backend tests and verify no legacy symbols remain**

Run: `pytest -q control/tests` and:

```bash
if rg -n "PackageCandidate|PackageRollout|/api/v1/packages|/api/v1/deployments|config/package-families|config/workload-deployments" control/src control/tests control/web/src; then exit 1; fi
```

Expected: PASS with no supported-source references to the deleted pipeline.

- [ ] **Step 6: Commit the direct pipeline deletion**

```bash
git add control config docs tests
git commit -m "refactor: delete legacy package and deployment pipeline"
```

### Task 5: Scrub legacy documentation, repository contracts, and artifacts

**Files:**
- Modify/Delete: `docs/runbooks/node-onboarding.md`
- Modify/Delete: `docs/runbooks/inventory.md`
- Modify/Delete: Catalog/package/deployment design documents and plans that describe removed workflows
- Modify/Delete: `tests/runbooks/test_node_onboarding.py`
- Modify/Delete: proposal and repository tests that only exercise deleted TOML documents
- Modify: `docs/operations/install-vonk-agent.md`
- Modify: packaging/bootstrap scripts that currently require manual `agent.toml` editing

- [ ] **Step 1: Run the complete legacy-reference inventory**

Run:

```bash
rg -n "inventory/fleet\.toml|config/package-families|config/workload-deployments|/packages|/deployments|Catalog page|Packages page|Deployments page|Jobs page|edit .*agent\.toml" README.md docs control agent scripts tests packaging
```

- [ ] **Step 2: Rewrite supported operator instructions**

Describe only Fleet, Library, PostgreSQL-backed Spark registration, and the generated bootstrap command. Delete obsolete Git-roster, package-candidate, deployment-rollout, and manual-agent-TOML instructions.

- [ ] **Step 3: Run documentation/runbook tests**

Run: `pytest -q tests/runbooks control/tests/test_repository.py control/tests/test_proposals.py`

Expected: PASS with no supported documentation or test asserting removed workflows.

- [ ] **Step 4: Commit the documentation scrub**

```bash
git add README.md docs control/tests tests packaging scripts
git commit -m "docs: scrub superseded control-plane workflows"
```

### Task 6: Final cleanup verification

**Files:**
- Verify: all tracked source, test, documentation, and packaging files

- [ ] **Step 1: Verify the web build and tests**

Run: `cd control/web && npm test -- --run && npm run build`

- [ ] **Step 2: Verify backend tests and static legacy absence**

Run: `pytest -q control/tests tests` and:

```bash
if rg -n "AgentsPage|CatalogPage|PackagesPage|DeploymentsPage|UpdatesPage|JobsPage|AuditPage|inventory/fleet\.toml|/api/v1/packages|/api/v1/deployments|PackageCandidate|PackageRollout" control/src control/web/src control/tests tests docs; then exit 1; fi
```

- [ ] **Step 3: Verify the clean working tree and fresh-schema contract**

Run: `git diff --check`, `git status --short`, and the disposable fresh-database startup test.

Expected: no diff-check errors, no unintended files, a clean fresh schema, zero Sparks before enrollment, and no legacy tables/routes/artifacts.

- [ ] **Step 4: Commit the verified cleanup checkpoint**

```bash
git add .
git commit -m "refactor: complete clean-slate control-plane cleanup"
```
