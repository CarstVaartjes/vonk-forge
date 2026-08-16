# Prototype Model/Profile Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the pre-production v1 cutover by removing every retired model-profile/runtime authority while preserving the shared fleet, generic package, platform-update, transport, and native execution-harness paths.

**Architecture:** PostgreSQL v1 catalog entities, immutable recipe revisions, cluster mappings, recipe operations, and recipe route publication become the only model execution authority. The Git repository remains authoritative for fleet/topology, generic package definitions, deployment bundles, and platform update trust, but no longer exposes `config/workloads`, `config/cluster-profiles`, prototype maturity indexes, profile reconciliation, or model-specific package projections. Hermes selects an accepted v1 recipe run using exact run alias `hermes-agent`; no legacy fallback policy or compatibility reader remains.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/Alembic, React/TypeScript, pytest 9.1.1, Vitest, GitHub Actions, canonical JSON/TOML repository contracts.

## Global Constraints

- This is a pre-production v1 replacement: no prototype data migration, compatibility reader, alias bridge, restored deleted runtime manifest, or dual-contract period.
- Preserve `src/cluster_profiles` fleet/install, placement, generic package contracts, SSH transport, deployment bundle, platform release/publication/authority, update trust, and generated current API client modules.
- Preserve the native-v1 DS4 and Mia adapter build contexts and every exact catalog entity/recipe under `config/model-groups`, `config/models`, `config/model-versions`, `config/execution-harnesses`, `config/runtime-distributions`, `config/patch-bundles`, and `config/recipes`.
- Unsupported creative targets remain documented research/follow-on targets, not executable prototype adapters or active repository definitions.
- Keep secrets, tokens, model weights, and generated acceptance evidence out of Git and public images.
- Use TDD, one independently reviewed commit per task, and regenerate OpenAPI/clients/supply-chain evidence whenever an authoritative surface changes.

---

### Task 1: Put native-v1 recipe tests in the owning control suite

**Files:**
- Move: `tests/recipes/test_deepseek_v4_flash_ds4.py` → `control/tests/test_deepseek_v4_flash_ds4_recipe.py`
- Move: `tests/recipes/test_mia_deepseek_v4_flash.py` → `control/tests/test_mia_deepseek_v4_flash_recipe.py`
- Delete: `tests/adapters/test_ds4_artifact_manifest.py`
- Delete: `tests/adapters/test_ds4_runtime.py`
- Delete: `tests/adapters/test_mia_deepseek_dual.py`
- Delete: `tests/cluster_profiles/test_admission.py`
- Delete: `tests/cluster_profiles/test_backend.py`
- Delete: `tests/cluster_profiles/test_catalog.py`
- Delete: `tests/cluster_profiles/test_cli.py`
- Delete: `tests/cluster_profiles/test_contracts.py`
- Delete: `tests/cluster_profiles/test_health.py`
- Delete: `tests/cluster_profiles/test_phase4_model_catalog.py`
- Delete: `tests/cluster_profiles/test_profile_compat.py`
- Delete: `tests/cluster_profiles/test_state.py`
- Delete: `tests/cluster_profiles/test_switcher.py`
- Delete: `tests/cluster_profiles/test_workload_package_migration.py`
- Create: `tests/test_no_prototype_model_authority.py`

**Interfaces:**
- Consumes: native-v1 recipe documents and `vonk_control.recipe_runtime_specs` from the control dependency graph.
- Produces: root-suite collection without control-only imports and a negative repository guard against retired model/profile paths.

- [ ] **Step 1: Write the failing ownership and absence guard**

```python
def test_root_tests_do_not_import_control_implementation() -> None:
    offenders = [path for path in (ROOT / "tests").rglob("test_*.py") if "control/src" in path.read_text()]
    assert offenders == []


def test_prototype_model_authority_is_absent() -> None:
    forbidden = (
        ROOT / "config/workloads",
        ROOT / "config/cluster-profiles",
        ROOT / "locks/model-definitions.toml",
        ROOT / "inventory/reports/model-definitions.json",
        ROOT / "inventory/reports/accepted-cluster-profiles.json",
    )
    assert [str(path.relative_to(ROOT)) for path in forbidden if path.exists()] == []
```

- [ ] **Step 2: Capture the current CI RED state**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest --collect-only -q`

Expected: FAIL because root tests import deleted DS4/Mia prototype files and control-only SQLAlchemy code.

- [ ] **Step 3: Move the two native-v1 tests and delete only obsolete tests**

Use `git mv` for the two recipe tests, remove their manual `sys.path` mutation, and use ordinary `vonk_control` imports. Use `git rm` for the fourteen retired adapter/profile test files. Do not weaken any moved recipe assertion.

- [ ] **Step 4: Prove ownership before the data deletion task**

Run: `uv run --project control --frozen python -m pytest control/tests/test_deepseek_v4_flash_ds4_recipe.py control/tests/test_mia_deepseek_v4_flash_recipe.py -q`

Expected: `21 passed`.

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest --collect-only -q`

Expected: collection progresses beyond the four original errors; the new absence guard remains RED until Task 2.

- [ ] **Step 5: Commit**

```bash
git add control/tests tests
git commit -m "test: move v1 recipes to control ownership"
```

### Task 2: Delete prototype repository authority and unsupported adapters

**Files:**
- Delete directories: `config/workloads/`, `config/cluster-profiles/`, `inventory/reports/model-definitions/`
- Delete: `config/profile-selectors.toml`
- Delete: `locks/model-definitions.toml`
- Delete: `inventory/reports/model-definitions.json`
- Delete: `inventory/reports/accepted-cluster-profiles.json`
- Delete: `inventory/reports/deepseek-ds4-operational.json`
- Delete: `inventory/reports/deepseek-mia-operational.json`
- Delete: `config/package-families/ds4-deepseek.toml`
- Delete: `config/package-families/mia-deepseek.toml`
- Delete: `config/workload-deployments/ds4-deepseek-single.toml`
- Delete: `config/workload-deployments/mia-deepseek-dual.toml`
- Delete directories: `manifests/workload-releases/ds4-deepseek/`, `manifests/workload-releases/mia-deepseek/`
- Delete: `release/workloads/ds4-v0.5.3-spark-runtime.json`
- Delete directories: `adapters/creative/`, `adapters/llm/laguna-s21-single/`
- Delete obsolete tests under `tests/adapters/` that exclusively target those unsupported adapter trees
- Modify: `tests/scripts/test_workload_artifact_metadata.py`
- Modify: `tests/test_no_prototype_model_authority.py`

**Interfaces:**
- Consumes: Task 1 negative guard and native-v1 adapter allowlist.
- Produces: repository data with no active prototype workload/profile identity and no unsupported executable adapter masquerading as a supported recipe.

- [ ] **Step 1: Expand the failing guard to exact retained/deleted boundaries**

```python
def test_only_native_v1_model_adapter_roots_remain() -> None:
    files = {path.relative_to(ROOT).as_posix() for path in (ROOT / "adapters").rglob("*") if path.is_file()}
    assert files
    assert all(path.startswith(("adapters/deepseek/ds4/", "adapters/deepseek/mia-vllm/")) for path in files)
```

- [ ] **Step 2: Verify RED**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest tests/test_no_prototype_model_authority.py -q`

Expected: FAIL and enumerate existing prototype directories.

- [ ] **Step 3: Delete exact prototype data and unsupported adapters**

Use `git rm -r` only on the paths listed in this task. Retain `manifests/deepseek-v4-flash-0731.json` only if a native-v1 source-policy test still consumes it; otherwise remove it and update that test to use the exact ModelVersion artifact inventory.

- [ ] **Step 4: Keep generic workload metadata parsing but remove the DS4 fixture assertion**

Delete only the assertion that loads `release/workloads/ds4-v0.5.3-spark-runtime.json` from `tests/scripts/test_workload_artifact_metadata.py`; retain synthetic parser, digest, and policy tests.

- [ ] **Step 5: Pass the repository absence gate**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest tests/test_no_prototype_model_authority.py tests/scripts/test_workload_artifact_metadata.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add adapters config inventory locks manifests release tests
git commit -m "refactor: remove prototype model authority"
```

### Task 3: Remove the retired root model-profile controller

**Files:**
- Delete: `bin/vonkctl-legacy`
- Delete: `src/cluster_profiles/admission.py`
- Delete: `src/cluster_profiles/backend.py`
- Delete: `src/cluster_profiles/catalog.py`
- Delete: `src/cluster_profiles/contracts.py`
- Delete: `src/cluster_profiles/health.py`
- Delete: `src/cluster_profiles/legacy_cli.py`
- Delete: `src/cluster_profiles/profile_compat.py`
- Delete: `src/cluster_profiles/state.py`
- Delete: `src/cluster_profiles/switcher.py`
- Delete: `src/cluster_profiles/fleet/legacy.py`
- Delete: `src/cluster_profiles/workload_packages/legacy.py`
- Delete obsolete schemas: `src/cluster_profiles/schemas/accepted-cluster-profiles.schema.json`, `src/cluster_profiles/schemas/model-definitions.schema.json`, `src/cluster_profiles/schemas/node-health-raw.schema.json`, `src/cluster_profiles/schemas/node-health.schema.json`
- Modify: `src/cluster_profiles/__init__.py`
- Modify: `src/cluster_profiles/cli.py`
- Modify: `src/cluster_profiles/workload_packages/__init__.py`
- Modify: `pyproject.toml`
- Delete: `tests/cluster_profiles/fleet/test_legacy.py`
- Modify current CLI/package/fleet tests under `tests/cluster_profiles/`
- Modify: `tests/test_no_prototype_model_authority.py`

**Interfaces:**
- Consumes: current generated control client, fleet/install contracts, placement, generic package contracts, platform update/release contracts, and SSH transport.
- Produces: `vonkctl` with only current API, node, package, and platform-update administration commands.

- [ ] **Step 1: Write a failing public-module/CLI guard**

```python
def test_root_package_exports_no_profile_contract() -> None:
    import cluster_profiles
    assert set(cluster_profiles.__all__) == set()


def test_vonkctl_help_has_no_profile_or_legacy_commands(run_vonkctl) -> None:
    result = run_vonkctl("--help")
    assert result.returncode == 0
    assert "profile" not in result.stdout.lower()
    assert "model-switch" not in result.stdout.lower()
```

- [ ] **Step 2: Verify RED**

Run: `uv run --frozen python -m pytest tests/cluster_profiles -q -k 'cli or legacy or profile'`

Expected: FAIL because legacy exports and commands still exist.

- [ ] **Step 3: Remove retired modules and prune command registration**

Keep `cli.py` itself. Remove only imports/parsers/handlers backed by deleted profile/catalog/health modules. Keep API authentication, node/fleet/install, package, deployment, platform update, and transport commands.

- [ ] **Step 4: Remove obsolete schema packaging**

Delete the four schema `force-include` entries from `pyproject.toml`; keep fleet, topology, placement, generic package, platform update/release, and deployment bundle schemas.

- [ ] **Step 5: Pass the current root administration suite**

Run: `uv run --frozen python -m pytest tests/cluster_profiles tests/test_no_prototype_model_authority.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add bin src/cluster_profiles pyproject.toml tests
git commit -m "refactor: remove legacy profile controller"
```

### Task 4: Remove profile reconciliation from the control API and web UI

**Files:**
- Delete: `control/src/vonk_control/desired_state.py`
- Delete: `control/src/vonk_control/acceptance.py`
- Delete: `control/src/vonk_control/legacy_runtime.py`
- Delete: `control/src/vonk_control/legacy_route_runtime.py`
- Modify: `control/src/vonk_control/reconcile.py` (retain `ChangeService`, `ReconciliationPlan`, and `resolved_reconciliation_plan`; remove the profile orchestrator and compatibility readers)
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/auth.py`
- Modify: `control/src/vonk_control/repository.py`
- Modify: `control/src/vonk_control/serializers.py`
- Delete: `control/tests/test_desired_state.py`
- Delete: `control/tests/test_runtime_handlers.py`
- Delete: `control/tests/test_legacy_route_runtime.py`
- Modify: `control/tests/test_reconcile.py` to cover only retained canonical plan construction
- Delete: `control/tests/test_reconcile_postgres.py`
- Modify: `control/tests/test_repository.py`
- Modify API/auth/security tests that reference profile reconciliation
- Delete: `control/web/src/pages/profiles.tsx`
- Delete: `control/web/src/pages/profiles.test.tsx`
- Delete: `control/web/src/pages/models.tsx`
- Delete: `control/web/src/components/repository-editor.tsx`
- Delete: `control/web/src/components/repository-editor.test.tsx`
- Delete: `control/web/src/components/reconciliation-plan.tsx`
- Delete: `control/web/src/components/reconciliation-plan.test.tsx`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/components/app-shell.tsx`
- Modify: `control/web/src/components/app-shell.test.tsx`
- Modify: `control/web/src/api/client.ts`
- Modify: `control/web/src/api/types.ts`
- Regenerate: `control/openapi.json`, `src/cluster_profiles/generated_control/`, `control/web/src/api/generated.d.ts`

**Interfaces:**
- Consumes: current v1 Catalog, Library, recipe plan/operation endpoints, package services, Fleet, and repository inventory/topology reads.
- Produces: no `/api/v1/profiles/*`, profile reconciliation, `models` document editor, or profile web route; Catalog and Library remain the model UX.

- [ ] **Step 1: Write failing OpenAPI and navigation absence tests**

```python
def test_openapi_has_no_profile_reconciliation_or_legacy_document_editor(app) -> None:
    paths = app.openapi()["paths"]
    assert not any(path.startswith("/api/v1/profiles/") for path in paths)
    assert "/api/v1/reconciliations/plan" not in paths
    assert "/api/v1/reconciliations" not in paths
    assert "/api/v1/documents" not in paths
```

```tsx
expect(screen.queryByRole("link", {name: "Profiles"})).not.toBeInTheDocument();
expect(screen.queryByRole("link", {name: "Models"})).not.toBeInTheDocument();
expect(screen.getByRole("link", {name: "Library"})).toBeVisible();
expect(screen.getByRole("link", {name: "Catalog"})).toBeVisible();
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control --frozen python -m pytest control/tests -q -k 'openapi or reconcile or repository or auth'`

Run: `npm test --prefix control/web -- --run src/components/app-shell.test.tsx`

Expected: FAIL because profile endpoints and navigation remain.

- [ ] **Step 3: Remove only profile desired-state authority**

Delete profile plan/apply/cancel API routes, mutation-role entries, profile document listing, profile reconciler construction, prototype acceptance simulators, and legacy runtime modules. Keep repository service for immutable inventory/topology, package definitions, deployment bundles, update trust, and proposal paths still consumed by current non-model administration.

- [ ] **Step 4: Simplify reconciliation to current planners**

Delete `CompatibilityDefinitions`, `RepositoryDefinitions`, `DesiredStatePlanner`, `Reconciler`, `ReconciliationResult`, `IneligibleCommit`, and `StaleFleetEvidence`. Retain `ChangeService`, immutable `ReconciliationPlan`, and `resolved_reconciliation_plan` because `PackageDesiredStateResolver` consumes the latter two. Delete profile plan/apply/cancel orchestration tests; package rollout tests remain the authority for current generic package planning.

- [ ] **Step 5: Remove old web routes and API client methods**

Delete `profiles` and `models` from `AppRoute`, navigation, page map, `ControlApi.documents`, `planProfile`, and reconciliation methods used only by deleted components. Keep Catalog, Library, cluster mapping, recipe operations, package/deployment, updates, jobs, Fleet, Agents, and Audit.

- [ ] **Step 6: Regenerate contracts**

Run: `scripts/generate-control-clients`

Expected: generated Python and TypeScript clients contain no profile reconciliation or document-editor operations.

- [ ] **Step 7: Pass control and web focused suites**

Run: `uv run --project control --frozen python -m pytest control/tests/test_reconcile.py control/tests/test_reconcile_postgres.py control/tests/test_repository.py control/tests/security/test_no_routine_ssh.py -q`

Run: `npm test --prefix control/web -- --run`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add control src/cluster_profiles/generated_control
git commit -m "refactor: remove profile reconciliation authority"
```

### Task 5: Make Hermes consume an exact v1 recipe run alias

**Files:**
- Delete: `config/hermes-agent-policy.toml`
- Delete: `control/src/vonk_control/hermes_policy.py`
- Delete: `control/src/vonk_control/hermes_routes.py`
- Delete: `control/tests/test_hermes_policy.py`
- Modify route/API tests that construct `RepositoryHermesRoutePolicy`
- Modify: `control/src/vonk_control/api.py`
- Modify: `docs/runbooks/hermes-agent.md`
- Modify: `docs/runbooks/platform-operations.md`
- Modify: `docs/architecture-overview.md`
- Modify: `docs/vonk-forge-architecture.html`

**Interfaces:**
- Consumes: `RecipeRouteService`, exact resolved recipe revision, and operator-selected unique `RecipeRun.alias`.
- Produces: Hermes requests fixed LiteLLM name `hermes-agent`, which exists only while one accepted v1 recipe run is started with exact alias `hermes-agent`.

- [ ] **Step 1: Write a failing v1 alias contract test**

```python
def test_hermes_alias_comes_only_from_recipe_route_publication(tmp_path: Path) -> None:
    service, _publisher, applied, run_id = setup(
        tmp_path,
        run_alias="hermes-agent",
        runtime_model_aliases=("deepseek-v4-flash-dspark",),
    )
    service.publish_run(run_id)
    document = json.loads(applied[-1])
    assert [row["model_name"] for row in document["model_list"]] == ["hermes-agent"]
    assert document["model_list"][0]["litellm_params"]["model"] == "openai/deepseek-v4-flash-dspark"
    root = Path(__file__).resolve().parents[2]
    assert "RepositoryHermesRoutePolicy" not in (root / "control/src/vonk_control/api.py").read_text()
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control --frozen python -m pytest control/tests/test_recipe_routes.py -q -k hermes`

Expected: FAIL until the exact run-alias contract and test exist.

- [ ] **Step 3: Remove repository maturity fallback policy**

Remove `RepositoryHermesRoutePolicy` construction and all reads of `inventory/reports/model-definitions.json` and `config/hermes-agent-policy.toml`. Do not add a fallback compatibility alias. Recipe route publication already maps the unique run alias to the exact recipe's declared upstream model alias and accepted endpoint evidence.

- [ ] **Step 4: Document selection and switching**

Document that an operator resolves and installs one exact recipe revision, starts its run with alias `hermes-agent`, canaries it, and only then points Hermes at the resulting LiteLLM group. Switching requires starting/canarying the replacement under a temporary alias, stopping the old `hermes-agent` run, and starting the accepted replacement with `hermes-agent`; no two active runs may share that alias.

- [ ] **Step 5: Pass route and Hermes runtime suites**

Run: `uv run --project control --frozen python -m pytest control/tests/test_recipe_routes.py control/tests/test_route_runtime.py control/tests/test_worker_authority.py -q`

Run: `bash deploy/compose/tests/hermes-agent-runtime.sh`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add config control docs
git commit -m "refactor: route Hermes through v1 recipes"
```

### Task 6: Close documentation, supply-chain, and complete-suite gates

**Files:**
- Modify or delete current runbooks/README sections that advertise `vonkctl-legacy`, model profiles, old runtime releases, or direct adapter lifecycle commands
- Preserve historical `docs/superpowers/specs/`, `docs/superpowers/plans/`, and audit records as explicitly historical evidence; remove only links that present them as current operations
- Modify: `scripts/verify-supply-chain`
- Modify generated supply-chain manifests/evidence required by that script
- Modify: `.github/workflows/ci.yml` only if test ownership paths changed; do not weaken suite coverage
- Modify: `docs/superpowers/plans/2026-08-15-execution-harness-foundation.md`

**Interfaces:**
- Consumes: Tasks 1–5 and the existing v1 foundation design.
- Produces: one reproducible current documentation path and green complete repository/control/agent/web/supply-chain gates.

- [ ] **Step 1: Add current-documentation negative tests**

```python
def test_current_docs_do_not_advertise_prototype_operations() -> None:
    forbidden = ("vonkctl-legacy", "config/cluster-profiles/", "config/workloads/", "deepseek-agent-single", "deepseek-agent-dual")
    offenders = {path: token for path in CURRENT_DOCS for token in forbidden if token in path.read_text()}
    assert offenders == {}
```

- [ ] **Step 2: Verify RED and update current docs**

Run: `uv run --frozen python -m pytest tests/runbooks tests/test_docs_contract.py -q`

Expected: FAIL until current runbooks point to Catalog, Library, recipe operations, and execution-harness guides.

- [ ] **Step 3: Refresh generated evidence**

Run: `scripts/update-global-contracts`

Run: `scripts/verify-supply-chain --write-manifest`

Expected: generated outputs include only retained current source/assets and pass a subsequent read-only verification.

- [ ] **Step 4: Run negative source scans**

Run: `rg -n 'deepseek-agent-single|deepseek-agent-dual|vonkctl-legacy|Catalog\.load\(|config/cluster-profiles/|config/workloads/|runtime-manifest\.json' src control config inventory locks manifests release adapters tests scripts docs --glob '!docs/superpowers/**' --glob '!docs/audits/**'`

Expected: no active-code/current-documentation matches. Historical specs/audits may retain explicit historical names.

- [ ] **Step 5: Run complete verification**

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest --collect-only -q`

Run: `uv run --python 3.12 --frozen --with pytest==9.1.1 pytest -q`

Run: `uv run --project control --frozen --with-editable . pytest control/tests -q`

Run: `uv run --project agent --frozen pytest agent/tests -q`

Run: `cargo test --manifest-path agent/rust/Cargo.toml --all-targets`

Run: `npm test --prefix control/web -- --run && npm run build --prefix control/web`

Run: `scripts/verify-supply-chain`

Run: `git diff --check && git status --short`

Expected: all suites pass; status lists only intentional plan implementation files.

- [ ] **Step 6: Commit**

```bash
git add .github docs scripts
git commit -m "docs: close the v1 model cutover"
```

## Self-review

- Spec coverage: the existing-runtime replacement boundary, no-compatibility rule, native-v1 asset retention, Fleet/Library presentation, fresh reset, and exact recipe route publication each map to an explicit task.
- Placeholder scan: the plan contains no deferred implementation markers; every deletion, retained boundary, test command, and expected state is concrete.
- Type consistency: recipe tests move into the control dependency graph; `Reconciler` retains one callable planner protocol; Hermes uses the existing unique `RecipeRun.alias`; repository/platform/package interfaces remain unchanged unless explicitly pruned.
