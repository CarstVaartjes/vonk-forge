# Vonk Local Catalog Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make local PostgreSQL authoritative for recipe families, authored/imported/global recipes, immutable revisions, and materialized deployment intent without requiring Git for recipe operations.

**Architecture:** New catalog tables coexist with the current package operational projection while adapters migrate from repository documents to exact recipe revision digests. Canonical recipe JSON is validated against a vendored global v1 schema. Existing Git/TUF artifact trust remains available, but `base_commit` stops being the authorization identity for recipe create, install, run, or route operations.

**Tech Stack:** Python 3.12, FastAPI, Pydantic, SQLAlchemy, Alembic, PostgreSQL/SQLite tests, jsonschema, React/TypeScript, Vitest.

## Global Constraints

- `schemas/global/contract.lock.json` pins exact public-contract commit and SHA-256 values.
- Recipe canonical JSON contains no secrets, raw shell, installers, mutable image tags, or privileged host requests.
- Draft edits use optimistic version fences; resolved revisions are immutable.
- Built-in package families are idempotently seeded by migration/service startup and never overwrite user-modified rows.
- Existing operational records remain readable throughout expand/migrate/contract changes.
- Local operation remains functional when the Git remote and `api.vonkforge.ai` are unavailable.
- Every mutation is actor-bound, audited, and exposed identically to CLI and web through the local API.

---

### Task 1: Pin and validate the public recipe contract

**Files:**
- Create: `schemas/global/recipe-v1.schema.json`
- Create: `schemas/global/problem-v1.schema.json`
- Create: `schemas/global/test-report-v1.schema.json`
- Create: `schemas/global/contract.lock.json`
- Create: `control/src/vonk_control/recipe_contract.py`
- Create: `control/tests/test_recipe_contract.py`
- Create: `scripts/update-global-contracts`

**Interfaces:**
- Produces: `canonical_recipe(document: Mapping[str, object]) -> bytes`
- Produces: `recipe_content_sha256(document: Mapping[str, object]) -> str`
- Produces: `validate_recipe(document: Mapping[str, object]) -> None`

- [ ] **Step 1: Write failing canonical fixture tests**

```python
def test_recipe_hash_matches_global_fixture(global_recipe_fixture, global_contract_lock) -> None:
    expected = global_contract_lock["fixtures"]["recipe-v1-minimal.json"]["content_sha256"]
    assert recipe_content_sha256(global_recipe_fixture) == expected
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_recipe_contract.py -v`

Expected: FAIL because `vonk_control.recipe_contract` is absent.

- [ ] **Step 3: Vendor the exact released global contract**

Run:

```bash
scripts/update-global-contracts \
  --repo /home/carst/vonk-forge-web \
  --commit $(git -C /home/carst/vonk-forge-web rev-parse HEAD)
```

The script copies only the three public schema files, records their lowercase SHA-256 values and exact commit, rejects a dirty global source tree, and writes canonical JSON with a final newline.

- [ ] **Step 4: Implement local canonical validation**

Match the global serializer: UTF-8, sorted keys, compact separators, no NaN/Infinity, no floats, no duplicate keys, and lowercase SHA-256. Compile Draft 2020-12 validators once and return bounded path/code details instead of raw `jsonschema` exception text.

- [ ] **Step 5: Verify local/global parity**

Run: `uv run --project control pytest control/tests/test_recipe_contract.py -v`

Expected: PASS for both global fixtures and rejection of a float, duplicate key, mutable image tag, raw command, and privileged recipe.

- [ ] **Step 6: Commit contract pin**

```bash
git add schemas/global control/src/vonk_control/recipe_contract.py control/tests/test_recipe_contract.py scripts/update-global-contracts
git commit -m "feat: pin public recipe contract"
```

### Task 2: Add local catalog database tables

**Files:**
- Create: `control/migrations/versions/0015_recipe_catalog.py`
- Modify: `control/src/vonk_control/models.py`
- Create: `control/tests/test_recipe_catalog_migration.py`

**Interfaces:**
- Produces SQLAlchemy models: `PackageFamily`, `LocalRecipe`, `LocalRecipeRevision`, `RecipeImport`, `RecipeImportItem`, `RecipeGlobalLink`, `MaterializedDeployment`, `MaterializedDeploymentNode`

- [ ] **Step 1: Write the failing migration-head test**

```python
def test_recipe_catalog_is_the_linear_head(alembic_config) -> None:
    script = ScriptDirectory.from_config(alembic_config)
    assert script.get_heads() == ["0015_recipe_catalog"]
    assert script.get_revision("0015_recipe_catalog").down_revision == "0014_package_action_plans"
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_recipe_catalog_migration.py -v`

Expected: FAIL because revision `0015_recipe_catalog` is missing.

- [ ] **Step 3: Define catalog tables and constraints**

Create these exact identities:

- `package_families(id VARCHAR(128) PK, display_name, provider_kind, schema_version, definition JSON, builtin, created_at, updated_at)`;
- `local_recipes(id UUID PK, slug UNIQUE, title, description, source_kind, created_by, created_at, updated_at)`;
- `local_recipe_revisions(id UUID PK, recipe_id FK, revision_number, lifecycle, schema_version, document JSON, content_sha256, created_by, created_at)`;
- `recipe_imports(id UUID PK, recipe_id FK, source_kind, source_reference, source_sha256, redacted_source JSON, created_by, created_at)`;
- `recipe_import_items(id UUID PK, import_id FK, source_path, disposition, destination_path, reason_code, detail, blocking)`;
- `recipe_global_links(recipe_id PK/FK, global_recipe_id, global_publisher, global_slug, global_revision, global_content_sha256, sync_state, synced_at)`;
- `materialized_deployments(id UUID PK, recipe_revision_id FK, alias, state, placement_digest, config JSON, created_by, created_at, updated_at)`;
- `materialized_deployment_nodes(id UUID PK, deployment_id FK, node_id FK, rank, role, state, reserved_disk_bytes, reserved_memory_bytes, observed_memory_bytes, endpoint JSON, updated_at)`.

Use unique constraints on revision number/content per recipe, alias, `(deployment_id,node_id)`, and `(deployment_id,rank)`. Lifecycle accepts only `draft`, `blocked`, `resolved`, `deprecated`; source kind accepts `local`, `workload_run`, `global`. Resolved revisions require a content hash and can never be updated through service code.

- [ ] **Step 4: Implement SQLAlchemy parity**

Add models to the existing `models.py` so `Base.metadata` includes them without import side effects. Change the module docstring from “Git remains definition authority” to a precise split between local database catalog authority and operational state.

- [ ] **Step 5: Verify constraints, upgrade, and downgrade**

Run: `uv run --project control pytest control/tests/test_recipe_catalog_migration.py control/tests/test_migrations.py -v`

Expected: PASS, including cascade rules, duplicate hash rejection, invalid lifecycle rejection, and downgrade back to `0014_package_action_plans` with earlier tables/data intact.

- [ ] **Step 6: Commit the migration**

```bash
git add control/migrations/versions/0015_recipe_catalog.py control/src/vonk_control/models.py control/tests
git commit -m "feat: add local recipe catalog tables"
```

### Task 3: Seed standard package families idempotently

**Files:**
- Create: `control/src/vonk_control/catalog_seeds.py`
- Create: `control/tests/test_catalog_seeds.py`
- Modify: `control/src/vonk_control/api.py`

**Interfaces:**
- Produces: `seed_standard_families(session: Session, now: datetime) -> SeedResult`
- Produces IDs: `oci`, `huggingface-snapshot`, `vllm`, `sglang`, `llama-cpp`

- [ ] **Step 1: Write failing seed tests**

```python
def test_standard_seed_is_idempotent_and_preserves_user_edits(session, now) -> None:
    first = seed_standard_families(session, now)
    session.get(PackageFamily, "vllm").display_name = "My vLLM"
    session.commit()
    second = seed_standard_families(session, now)
    assert first.created == 5
    assert second.created == 0
    assert session.get(PackageFamily, "vllm").display_name == "My vLLM"
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_catalog_seeds.py -v`

Expected: FAIL because `catalog_seeds` does not exist.

- [ ] **Step 3: Implement versioned insert-only seeds**

Each definition declares provider kind, supported recipe schema, architecture `linux/arm64`, and typed capability identifier. Existing rows are never overwritten. A future seed version inserts a new stable ID or performs an explicit migration, not an upsert over user data.

- [ ] **Step 4: Invoke seeding after migration readiness**

Run seeding in the control initialization transaction after Alembic head is verified and before API readiness becomes true. A seed failure keeps readiness false and emits no secret-bearing definition.

- [ ] **Step 5: Verify seed and startup behavior**

Run: `uv run --project control pytest control/tests/test_catalog_seeds.py control/tests/test_generation_readiness.py -v`

Expected: PASS for empty, already-seeded, user-edited, interrupted-transaction, and concurrent-start cases.

- [ ] **Step 6: Commit seeds**

```bash
git add control/src/vonk_control/catalog_seeds.py control/src/vonk_control/api.py control/tests
git commit -m "feat: seed standard recipe families"
```

### Task 4: Implement recipe repository and immutable revision service

**Files:**
- Create: `control/src/vonk_control/catalog_repository.py`
- Create: `control/src/vonk_control/catalog_service.py`
- Create: `control/tests/test_catalog_repository.py`
- Create: `control/tests/test_catalog_service.py`

**Interfaces:**
- Produces: `RecipeDraftInput`, `RecipeSummary`, `RecipeRevisionView`
- Produces: `CatalogService.create_recipe(actor, draft) -> RecipeRevisionView`
- Produces: `CatalogService.update_draft(recipe_id, expected_revision, document) -> RecipeRevisionView`
- Produces: `CatalogService.resolve(recipe_id, expected_revision) -> RecipeRevisionView`
- Produces: `CatalogService.fork(recipe_id, revision, slug, actor) -> RecipeRevisionView`

- [ ] **Step 1: Write the failing immutability test**

```python
def test_resolve_creates_immutable_revision_and_repeated_resolve_is_idempotent(service, recipe_document) -> None:
    draft = service.create_recipe("admin", RecipeDraftInput(slug="qwen3", document=recipe_document))
    resolved = service.resolve(draft.recipe_id, draft.revision_number, "admin")
    repeated = service.resolve(draft.recipe_id, draft.revision_number, "admin")
    assert repeated.id == resolved.id
    assert repeated.content_sha256 == resolved.content_sha256
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_catalog_service.py -v`

Expected: FAIL because the service is absent.

- [ ] **Step 3: Implement transactional repository operations**

Use `SELECT ... FOR UPDATE` on recipe rows for revision allocation. Draft update compares the expected revision number and returns stable conflict code `catalog.stale_revision`. Resolve validates canonical document, checks all external identities are immutable, writes a new `resolved` revision once, and never updates its JSON/hash.

- [ ] **Step 4: Implement safe source handling**

Reject values under keys matching `authorization`, `credential`, `password`, `secret`, `token`, `private_key`, and `certificate`. Imported raw source is separately redacted before persistence. Audit payloads contain IDs/hashes, not full recipe documents.

- [ ] **Step 5: Verify concurrency and failure cases**

Run: `uv run --project control pytest control/tests/test_catalog_repository.py control/tests/test_catalog_service.py -v`

Expected: PASS for concurrent revision allocation, stale edit, duplicate slug, duplicate resolve, fork attribution, invalid schema, secret rejection, and transaction rollback.

- [ ] **Step 6: Commit catalog service**

```bash
git add control/src/vonk_control/catalog_repository.py control/src/vonk_control/catalog_service.py control/tests
git commit -m "feat: add local recipe authoring service"
```

### Task 5: Expose local recipe API

**Files:**
- Create: `control/src/vonk_control/catalog_api.py`
- Create: `control/tests/test_catalog_api.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/operation_api.py`

**Interfaces:**
- Produces: `GET /api/v1/catalog/recipes`
- Produces: `POST /api/v1/catalog/recipes`
- Produces: `GET /api/v1/catalog/recipes/{recipe_id}`
- Produces: `PUT /api/v1/catalog/recipes/{recipe_id}/draft`
- Produces: `POST /api/v1/catalog/recipes/{recipe_id}/resolve`
- Produces: `POST /api/v1/catalog/recipes/{recipe_id}/fork`

- [ ] **Step 1: Write failing API authorization and stale-edit tests**

```python
def test_operator_cannot_author_recipe(client, operator_headers, recipe_document) -> None:
    response = client.post("/api/v1/catalog/recipes", headers=operator_headers, json={"slug": "qwen3", "document": recipe_document})
    assert response.status_code == 403


def test_stale_draft_returns_stable_problem(client, admin_headers, existing_recipe) -> None:
    response = client.put(f"/api/v1/catalog/recipes/{existing_recipe.id}/draft", headers=admin_headers, json={"expected_revision": 0, "document": existing_recipe.document})
    assert response.status_code == 409
    assert response.json()["code"] == "catalog.stale_revision"
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_catalog_api.py -v`

Expected: FAIL with `404` for catalog routes.

- [ ] **Step 3: Add strict Pydantic request/response models**

Forbid unknown fields, cap recipe JSON at 256 KiB, list responses at 100 items, title at 160 characters, description at 4096, and slug at 63 lowercase characters. Use cursor pagination and stable problem codes.

- [ ] **Step 4: Install routes into the existing application**

Reuse `Actor`, mutation-role checks, request IDs, audit sink, and bounded error middleware. Do not shell out to Git or instantiate `Repository` from any catalog route.

- [ ] **Step 5: Export OpenAPI and regenerate local frontend types**

Run the existing OpenAPI client generation path and assert each operation ID is stable: `listLocalRecipes`, `createLocalRecipe`, `getLocalRecipe`, `updateLocalRecipeDraft`, `resolveLocalRecipe`, `forkLocalRecipe`.

- [ ] **Step 6: Verify API and authorization**

Run: `uv run --project control pytest control/tests/test_catalog_api.py control/tests/test_openapi_clients.py -v`

Expected: PASS for viewer reads, admin authoring, operator denial, stale conflicts, size bounds, audit IDs, and no Git interaction.

- [ ] **Step 7: Commit local API**

```bash
git add control/src/vonk_control/catalog_api.py control/src/vonk_control/api.py control/src/vonk_control/operation_api.py control/tests control/web/src/api
git commit -m "feat: expose local recipe catalog API"
```

### Task 6: Remove the Git gate from recipe deployment authority

**Files:**
- Create: `control/migrations/versions/0016_recipe_deployment_authority.py`
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/src/vonk_control/agent_reconciliation.py`
- Create: deployment authority tests for the retained recipe placement path

**Interfaces:**
- Produces rollout identity: `recipe_revision_id + placement_digest + plan_digest`
- Deprecates required rollout authority: `base_commit`

- [ ] **Step 1: Write a failing offline-Git deployment test**

```python
def test_resolved_recipe_plans_without_git_remote(catalog_service, rollout_service, resolved_recipe, no_git_access) -> None:
    plan = rollout_service.preview_recipe(resolved_recipe.id, actor="admin")
    assert plan.recipe_revision_id == resolved_recipe.id
    assert no_git_access.calls == []
```

- [ ] **Step 2: Verify RED**

Run: `uv run --project control pytest control/tests/test_recipe_deployment_authority.py -v`

Expected: FAIL because rollout requires repository commit state.

- [ ] **Step 3: Expand rollout storage**

Add nullable `recipe_revision_id` FK and `authority_digest` to package rollouts, backfill existing rows with a deterministic legacy authority digest over `base_commit`, `deployment_digest`, and `release_digest`, then require exactly one of legacy or recipe authority during transition. New recipe rollouts set `base_commit` to null.

- [ ] **Step 4: Bind reconciliation to recipe revision**

The controller sends agents exact recipe content hash, materialized deployment ID, release/artifact digests, placement/rank, and operation fence. Git commit is not part of new recipe operation eligibility. Artifact TUF metadata may still authorize Vonk-built platform or adapter artifacts independently.

- [ ] **Step 5: Preserve legacy reads and remove legacy writes**

Existing rollouts remain inspectable and recoverable. New API paths cannot create legacy Git-bound rollouts. A later contract migration may remove nullable legacy columns only after no active/retained rollout references them.

- [ ] **Step 6: Verify migration and Git-offline behavior**

Run: `uv run --project control pytest control/tests/test_recipe_deployment_authority.py control/tests/test_workload_package_migration.py control/tests/test_agent_reconciliation.py -v`

Expected: PASS for legacy migration, new DB authority, Git outage, stale recipe hash, and exact agent payload binding.

- [ ] **Step 7: Commit authority migration**

```bash
git add control/migrations/versions/0016_recipe_deployment_authority.py control/src/vonk_control control/tests
git commit -m "feat: make database recipes deployment authority"
```

### Task 7: Add the local recipe catalog interface

**Files:**
- Create: `control/web/src/pages/catalog.tsx`
- Create: `control/web/src/pages/catalog.test.tsx`
- Create: `control/web/src/pages/recipe-editor.tsx`
- Create: `control/web/src/pages/recipe-editor.test.tsx`
- Create: `control/web/src/components/recipe-summary.tsx`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/styles.css`

**Interfaces:**
- Produces UI routes: `/catalog`, `/catalog/new`, `/catalog/:recipeId`
- Consumes generated operations from Task 5

- [ ] **Step 1: Write failing catalog rendering test**

```tsx
test("separates local, WorkloadRun, and global recipe origins", async () => {
  render(<CatalogPage client={catalogClientFixture()} />);
  expect(await screen.findByRole("heading", { name: "Recipe catalog" })).toBeVisible();
  expect(screen.getByText("Local")).toBeVisible();
  expect(screen.getByText("Imported from WorkloadRun")).toBeVisible();
  expect(screen.getByText("Downloaded from vonkforge.ai")).toBeVisible();
});
```

- [ ] **Step 2: Verify RED**

Run: `npm --prefix control/web test -- --run src/pages/catalog.test.tsx`

Expected: FAIL because `CatalogPage` is missing.

- [ ] **Step 3: Implement catalog list and filters**

Show title, origin, lifecycle, runtime, artifact variant, node range, declared/observed memory and disk, content hash prefix, and trust evidence. Search and filters are URL-addressable and API-backed; no client-side full-catalog fetch.

- [ ] **Step 4: Implement structured editor**

Provide fields for metadata, artifact, runtime, resources, topology, endpoint, and security. Never expose a shell/command textarea. Show validation paths inline and require optimistic revision when saving.

- [ ] **Step 5: Add resolve and fork actions**

Resolve shows the canonical hash and immutable fields before confirmation. Fork shows attribution and new local slug. Both use existing admin authorization and surface stable problem codes.

- [ ] **Step 6: Verify accessibility and build**

Run: `npm --prefix control/web test -- --run src/pages/catalog.test.tsx src/pages/recipe-editor.test.tsx && npm --prefix control/web run build`

Expected: PASS with keyboard-accessible forms, named error summaries, and no TypeScript errors.

- [ ] **Step 7: Commit catalog UI**

```bash
git add control/web
git commit -m "feat: add local recipe catalog UI"
```

## Plan acceptance

Run:

```bash
uv run --project control pytest control/tests/test_recipe_contract.py control/tests/test_recipe_catalog_migration.py control/tests/test_catalog_seeds.py control/tests/test_catalog_repository.py control/tests/test_catalog_service.py control/tests/test_catalog_api.py control/tests/test_recipe_deployment_authority.py -q
npm --prefix control/web test -- --run src/pages/catalog.test.tsx src/pages/recipe-editor.test.tsx
npm --prefix control/web run build
```

Then stop network access to the Git remote and global catalog and repeat create, edit, resolve, fork, preview, and agent-payload generation through the local API. Acceptance requires those operations to succeed, resolved documents to remain immutable, audit records to contain only bounded identities, and no new recipe rollout to contain a Git `base_commit` authority.
