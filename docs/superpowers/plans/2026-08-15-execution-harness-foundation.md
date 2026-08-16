# Execution Harness Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the prototype recipe implementation with the first supported v1 model catalog and execution-harness contract, then recreate DS4 and Mia as freshly tested v1 recipes.

**Architecture:** Keep immutable recipe revisions in the existing recipe tables and add one generic immutable entity store for model groups, models, model versions, harnesses, runtime distributions, and patch bundles. A recipe references exact resolved entities by kind, publisher, slug, and content digest and contains exactly one topology. A versioned built-in harness compiler converts that binding into the existing agent lifecycle; model-specific behavior remains in signed source bundles, distributions, or exact patch bundles rather than inventing model-specific harnesses.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL, JSON Schema Draft 2020-12, pytest, Podman/OCI, Bash, DGX Spark ARM64.

## Global Constraints

- `schema_version: 1` is the only supported catalog and recipe version after this cutover.
- The discarded prototype is not a public v1: add no compatibility parser, conversion layer, or dual-contract period.
- This is pre-production: clear prototype recipe, mapping, build, installation, run, route, and acceptance state instead of translating it.
- Identity is exactly `ModelGroup -> Model -> ModelVersion`; every recipe selects one exact primary model version and zero or more exact auxiliary versions.
- Exact references contain `kind`, `publisher`, `slug`, and a 64-character lowercase `content_sha256`.
- One recipe contains one node count and topology; replica and genuinely distributed topologies are distinct.
- Harness, runtime distribution, and optional patch bundle are separate exact identities.
- Built-in harnesses are `vllm`, `sglang`, `tensorrt-llm`, `llama-cpp`, `ds4`, `diffusers`, `comfyui`, and `pytorch-pipeline`.
- Interfaces and validators are engine-independent. Only OpenAI-compatible interfaces publish through LiteLLM.
- Runtime images are ARM64, digest-pinned, non-root, offline after installation, socket-free, capability-minimal, and mount model artifacts read-only.
- User-authored adapters obey the same source-bundle, build-policy, lifecycle, security, and evidence contracts.
- DS4 and Mia receive fresh structural, container, and physical acceptance; prototype evidence is not reused.
- Add no excluded-territory field, jurisdiction detection, regional filtering, or geographic enforcement.

## File structure

- `schemas/global/catalog-entity-v1.schema.json`: all non-recipe entity documents and kind-specific payloads.
- `schemas/global/recipe-v1.schema.json`: the replacement one-topology recipe contract.
- `schemas/global/harness-evidence-v1.schema.json`: canonical lifecycle and invocation evidence.
- `control/src/vonk_control/catalog_contract.py`: strict parsing, canonicalization, hashing, entity validation, and exact references.
- `control/src/vonk_control/recipe_contract.py`: replacement recipe validation and reference extraction.
- `control/src/vonk_control/catalog_entities.py`: immutable entity revision service and exact lookup.
- `control/src/vonk_control/harnesses/`: shared compiler contract, registry, and eight built-in compilers.
- `control/src/vonk_control/interface_adapters.py`: LiteLLM versus artifact-job publication policy.
- `control/src/vonk_control/harness_conformance.py`: deterministic synthetic lifecycle acceptance.
- `control/migrations/versions/0027_execution_harness_catalog.py`: fresh v1 schema head after the merged control-plane migrations; it contains no prototype data conversion.
- `config/model-groups/`, `config/models/`, `config/model-versions/`, `config/execution-harnesses/`, `config/runtime-distributions/`, `config/patch-bundles/`, and `config/recipes/`: authoritative built-in documents.

---

### Task 1: Define the replacement v1 contracts

**Files:**
- Create: `schemas/global/catalog-entity-v1.schema.json`
- Replace: `schemas/global/recipe-v1.schema.json`
- Create: `schemas/global/harness-evidence-v1.schema.json`
- Create: `control/src/vonk_control/catalog_contract.py`
- Replace: `control/src/vonk_control/recipe_contract.py`
- Modify: `control/src/vonk_control/schema_resources.py`
- Modify: `control/pyproject.toml`
- Modify: `control/Dockerfile`
- Create: `control/tests/fixtures/catalog/model-version-v1-minimal.json`
- Replace: `control/tests/fixtures/global/recipe-v1-minimal.json`
- Delete: `control/tests/fixtures/global/recipe-v1-multinode.json`
- Create: `control/tests/test_catalog_contract.py`
- Replace: `control/tests/test_recipe_contract.py`

**Interfaces:**
- Produces: `CatalogKind`, `CatalogReference`, `parse_catalog_json()`, `canonical_catalog_document()`, `catalog_content_sha256()`, `validate_catalog_document()`, and `parse_catalog_reference()`.
- Produces: `parse_recipe_json()`, `canonical_recipe()`, `recipe_content_sha256()`, `validate_recipe()`, `recipe_topology()`, and `recipe_references()`.

- [ ] **Step 1: Write failing exact-reference and entity tests**

```python
def test_exact_reference_has_portable_immutable_identity() -> None:
    reference = parse_catalog_reference({
        "kind": "model-version",
        "publisher": "vonk-forge",
        "slug": "synthetic-tiny-fp16",
        "content_sha256": "a" * 64,
    }, expected_kind=CatalogKind.MODEL_VERSION)
    assert reference.portable_identity == (
        "model-version", "vonk-forge", "synthetic-tiny-fp16", "a" * 64
    )


def test_catalog_contract_rejects_territory_automation() -> None:
    document = json.loads(MODEL_VERSION_FIXTURE.read_text())
    document["license"]["excluded_territories"] = ["NL"]
    with pytest.raises(CatalogContractError, match="additionalProperties"):
        validate_catalog_document(document)
```

- [ ] **Step 2: Write failing replacement recipe tests**

```python
def test_recipe_has_one_topology_and_exact_bindings() -> None:
    document = parse_recipe_json(RECIPE_FIXTURE.read_bytes())
    validate_recipe(document)
    assert recipe_topology(document)["node_count"] == 1
    assert {item.kind for item in recipe_references(document)} == {
        CatalogKind.MODEL_VERSION,
        CatalogKind.EXECUTION_HARNESS,
        CatalogKind.RUNTIME_DISTRIBUTION,
    }


def test_prototype_multi_profile_shape_is_rejected() -> None:
    document = json.loads(RECIPE_FIXTURE.read_text())
    document["deployment_profiles"] = []
    with pytest.raises(RecipeContractError, match="additionalProperties"):
        validate_recipe(document)
```

- [ ] **Step 3: Confirm the tests fail**

Run: `uv run --project control --frozen python -m pytest control/tests/test_catalog_contract.py control/tests/test_recipe_contract.py -q`

Expected: FAIL because the entity contract does not exist and the current recipe schema accepts the prototype shape.

- [ ] **Step 4: Add complete strict schemas**

The entity schema uses a discriminated `oneOf` for `model-group`, `model`, `model-version`, `execution-harness`, `runtime-distribution`, and `patch-bundle`. The recipe schema requires these keys and rejects all others:

```json
[
  "schema_version", "identity", "metadata", "model", "execution",
  "build", "parameters", "artifacts", "runtime", "topology",
  "interfaces", "validation", "provenance"
]
```

Reuse the proven strict digest, scalar, path, environment, source-bundle, network-host, resource, security, and lifecycle definitions. `topology` includes one name, mode, node count, roles, parallelism, fabric, start order, and stop order. Interface adapters are exactly `openai`, `image-job`, `audio-job`, `video-job`, `mesh-job`, and `artifact-job`.

- [ ] **Step 5: Implement canonical entity parsing and references**

```python
class CatalogKind(StrEnum):
    MODEL_GROUP = "model-group"
    MODEL = "model"
    MODEL_VERSION = "model-version"
    EXECUTION_HARNESS = "execution-harness"
    RUNTIME_DISTRIBUTION = "runtime-distribution"
    PATCH_BUNDLE = "patch-bundle"


@dataclass(frozen=True, slots=True)
class CatalogReference:
    kind: CatalogKind
    publisher: str
    slug: str
    content_sha256: str

    @property
    def portable_identity(self) -> tuple[str, str, str, str]:
        return (self.kind.value, self.publisher, self.slug, self.content_sha256)
```

Copy the proven duplicate-key rejection, float rejection, canonical JSON, bounded errors, and most-specific JSON Schema failure behavior from the current recipe contract.

- [ ] **Step 6: Implement replacement recipe semantics**

Validate exact references, parameter types and bounds, artifact-role coverage, one topology, world-size arithmetic, role counts, exactly one endpoint owner, fabric and host-network requirements, interface uniqueness, and validator/interface compatibility. Delete `deployment_profile()` and expose only `recipe_topology()`.

- [ ] **Step 7: Package schemas and pass focused tests**

Run: `uv run --project control --frozen python -m pytest control/tests/test_catalog_contract.py control/tests/test_recipe_contract.py control/tests/test_wheel_runtime_assets.py -q`

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add schemas/global control/src/vonk_control/catalog_contract.py control/src/vonk_control/recipe_contract.py control/src/vonk_control/schema_resources.py control/pyproject.toml control/Dockerfile control/tests
git commit -m "feat: define the v1 model recipe contract"
```

### Task 2: Store and resolve immutable catalog entities

**Files:**
- Modify: `control/src/vonk_control/models.py`
- Create: `control/src/vonk_control/catalog_entities.py`
- Modify: `control/src/vonk_control/catalog_service.py`
- Modify: `control/src/vonk_control/recipe_api.py`
- Create: `control/tests/test_catalog_entities.py`
- Modify: `control/tests/test_catalog_service.py`
- Modify: `control/tests/test_catalog_api.py`

**Interfaces:**
- Consumes: Task 1 contract functions and types.
- Produces: `CatalogEntity`, `CatalogEntityRevision`, and `CatalogEntityService` methods `create_draft()`, `revise()`, `resolve()`, `lookup_exact()`, and `list_entities()`.

- [ ] **Step 1: Write failing immutable-resolution tests**

```python
def test_resolved_entity_revision_is_immutable(session, service) -> None:
    draft = service.create_draft(MODEL_VERSION, actor="admin")
    resolved = service.resolve(draft.id, actor="admin")
    resolved.document["metadata"]["title"] = "changed"
    with pytest.raises(ValueError, match="immutable"):
        session.commit()


def test_recipe_resolution_never_falls_back_to_latest(catalog_service) -> None:
    with pytest.raises(CatalogConflict, match="exact model-version"):
        catalog_service.resolve_recipe_revision(RECIPE_WITH_UNKNOWN_DIGEST, actor="admin")
```

- [ ] **Step 2: Confirm the tests fail**

Run: `uv run --project control --frozen python -m pytest control/tests/test_catalog_entities.py control/tests/test_catalog_service.py -q`

Expected: FAIL because the generic entity models and service do not exist.

- [ ] **Step 3: Add generic entity and immutable revision models**

`CatalogEntity` has `id`, `kind`, `publisher`, `slug`, `title`, `created_by`, `created_at`, and `updated_at`. `CatalogEntityRevision` has `id`, `entity_id`, `revision_number`, `lifecycle`, `schema_version`, `document`, `content_sha256`, `created_by`, and `created_at`. Enforce unique `(kind,publisher,slug)`, unique revision numbers and content digests per entity, valid kinds, `draft|blocked|resolved|deprecated`, lowercase digests, and resolved-revision immutability.

- [ ] **Step 4: Implement exact lookup and recipe dependency resolution**

`lookup_exact()` selects kind, publisher, slug, digest, and lifecycle `resolved`; it never selects a newer digest. Recipe resolution verifies that the model version links to one exact model and group, the distribution supports the harness, and an optional patch declares that exact distribution before persisting the recipe digest.

- [ ] **Step 5: Add authenticated entity API routes**

Expose list/detail/draft/revise/resolve under `/api/v1/catalog/entities`. Responses include immutable revision identity and omit credentials, source-gating tokens, and secret values.

- [ ] **Step 6: Pass service and API tests**

Run: `uv run --project control --frozen python -m pytest control/tests/test_catalog_entities.py control/tests/test_catalog_service.py control/tests/test_catalog_api.py control/tests/test_recipe_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add control/src/vonk_control/models.py control/src/vonk_control/catalog_entities.py control/src/vonk_control/catalog_service.py control/src/vonk_control/recipe_api.py control/tests
git commit -m "feat: resolve immutable catalog entities"
```

### Task 3: Replace profile selection with one exact topology

**Files:**
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/src/vonk_control/cluster_mappings.py`
- Modify: `control/src/vonk_control/topology.py`
- Modify: `control/src/vonk_control/recipe_operations.py`
- Modify: `control/src/vonk_control/recipe_builds.py`
- Modify: `control/src/vonk_control/install_admission.py`
- Modify: `control/src/vonk_control/run_admission.py`
- Modify: `control/src/vonk_control/recipe_runtime_specs.py`
- Modify: `control/src/vonk_control/agent_api.py`
- Rewrite: `control/tests/test_cluster_mappings.py`
- Rewrite: `control/tests/test_topology.py`
- Rewrite: `control/tests/test_install_admission.py`
- Rewrite: `control/tests/test_run_admission.py`
- Rewrite: `control/tests/test_recipe_runtime_specs.py`

**Interfaces:**
- Consumes: `recipe_topology()` and exact resolved recipe dependencies.
- Produces: `ClusterMapping.topology_name`, `ClusterMappingService.preview(recipe_revision_id, node_ids, parameters, actor)`, and `compile_runtime_spec(document, resolved_entities, parameters, role, rank, recipe_build_id, image_digest)`.

- [ ] **Step 1: Rewrite mapping tests without profile input**

```python
preview = service.preview(
    recipe_revision_id=recipe_revision.id,
    node_ids=[node_a.node_id, node_b.node_id],
    parameters={},
    actor="admin",
)
assert preview.topology_name == "distributed-two-node"
assert [item.rank for item in preview.nodes] == [0, 1]
```

Add explicit failures for wrong node count, replicas used as distributed ranks, missing fabric capability, duplicate ranks, and multiple endpoint owners.

- [ ] **Step 2: Confirm profile-dependent code fails the new tests**

Run: `uv run --project control --frozen python -m pytest control/tests/test_cluster_mappings.py control/tests/test_topology.py control/tests/test_install_admission.py control/tests/test_run_admission.py control/tests/test_recipe_runtime_specs.py -q`

Expected: FAIL because services still require `profile_name` and read `deployment_profiles`.

- [ ] **Step 3: Derive placement exclusively from recipe topology**

Rename ORM and service fields from `profile_name` to `topology_name`; remove profile names from request models and operation payloads. Derive role, rank, endpoint owner, fabric, parallelism, start order, and stop order from the one topology document.

- [ ] **Step 4: Bind compiled runtime specs to every selected identity**

```python
compiled["identity"] = {
    "recipe_revision_sha256": recipe_digest,
    "model_version_sha256": model_version.content_sha256,
    "harness_sha256": harness.content_sha256,
    "runtime_distribution_sha256": distribution.content_sha256,
    "patch_bundle_sha256": patch.content_sha256 if patch else None,
}
compiled["topology"] = {
    "name": topology["name"],
    "node_count": topology["node_count"],
    "rank": rank,
    "role": role,
}
```

- [ ] **Step 5: Update build, install, and run admission**

Before queueing work, reject unresolved dependencies, stale digests, incompatible harness/distribution/patch bindings, non-accepted recipes, topology mismatch, insufficient resources, missing fabric, and mutable image identity.

- [ ] **Step 6: Pass focused topology and admission tests**

Run: `uv run --project control --frozen python -m pytest control/tests/test_cluster_mappings.py control/tests/test_topology.py control/tests/test_recipe_builds.py control/tests/test_install_admission.py control/tests/test_run_admission.py control/tests/test_recipe_operations.py control/tests/test_recipe_runtime_specs.py control/tests/test_agent_api.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add control/src/vonk_control control/tests
git commit -m "refactor: bind each recipe to one topology"
```

### Task 4: Add harness registry, interfaces, and synthetic conformance

**Files:**
- Create: `control/src/vonk_control/harnesses/__init__.py`
- Create: `control/src/vonk_control/harnesses/contracts.py`
- Create: `control/src/vonk_control/harnesses/common.py`
- Create: `control/src/vonk_control/harnesses/registry.py`
- Create: `control/src/vonk_control/interface_adapters.py`
- Create: `control/src/vonk_control/harness_conformance.py`
- Modify: `control/src/vonk_control/recipe_routes.py`
- Create: `control/tests/test_harness_registry.py`
- Create: `control/tests/test_harness_conformance.py`
- Create: `control/tests/test_interface_adapters.py`
- Rewrite: `control/tests/test_recipe_routes.py`

**Interfaces:**
- Produces: `HarnessCompiler`, `HarnessProjection`, `HarnessRegistry.register()`, `HarnessRegistry.compile()`, `InterfaceAdapter`, `interface_adapter()`, `run_synthetic_conformance()`, and `HarnessEvidence`.

- [ ] **Step 1: Write failing lifecycle, security, and routing tests**

```python
@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_harness_completes_synthetic_lifecycle(slug: str) -> None:
    evidence = run_synthetic_conformance(slug)
    assert evidence.phases == (
        "inspect", "prepare", "verify", "start", "ready", "invoke",
        "inspect", "stop", "verify-stopped",
    )
    assert evidence.offline_runtime is True
    assert evidence.security["docker_socket"] is False


def test_video_jobs_do_not_publish_to_litellm() -> None:
    assert interface_adapter("video-job").publication == "artifact"
```

- [ ] **Step 2: Confirm the tests fail**

Run: `uv run --project control --frozen python -m pytest control/tests/test_harness_registry.py control/tests/test_harness_conformance.py control/tests/test_interface_adapters.py control/tests/test_recipe_routes.py -q`

Expected: FAIL because the registry and interface adapters do not exist.

- [ ] **Step 3: Implement the typed compiler boundary**

```python
class HarnessCompiler(Protocol):
    slug: str
    contract_version: int

    def compile(
        self,
        recipe: Mapping[str, object],
        distribution: Mapping[str, object],
        patch: Mapping[str, object] | None,
        parameters: Mapping[str, object],
        topology: Mapping[str, object],
        role: str,
        rank: int,
    ) -> HarnessProjection: ...
```

Lookup uses the resolved harness slug and contract version. Unknown built-in slugs fail closed. A custom adapter must point to an exact signed source bundle and pass the same structured-command and security checks.

- [ ] **Step 4: Implement orthogonal interface adapters**

`openai` has `publication="litellm"`. `image-job`, `audio-job`, `video-job`, `mesh-job`, and `artifact-job` have `publication="artifact"`. Each adapter defines readiness, invocation request, evidence extraction, and withdrawal. Update `recipe_routes.py` so only LiteLLM interfaces create LiteLLM routes.

- [ ] **Step 5: Implement deterministic lifecycle conformance**

Exercise success order, idempotent inspect, interrupted-start recovery, interrupted-stop recovery, bounded stop, read-only model mounts, isolated writable outputs, no socket, numeric non-root UID, no-new-privileges, dropped capabilities, offline invocation, and evidence validation against `harness-evidence-v1.schema.json`.

- [ ] **Step 6: Pass conformance and routing tests**

Run: `uv run --project control --frozen python -m pytest control/tests/test_harness_registry.py control/tests/test_harness_conformance.py control/tests/test_interface_adapters.py control/tests/test_recipe_routes.py -q`

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add control/src/vonk_control/harnesses control/src/vonk_control/interface_adapters.py control/src/vonk_control/harness_conformance.py control/src/vonk_control/recipe_routes.py control/tests
git commit -m "feat: add execution harness conformance"
```

### Task 5: Implement the eight target-driven harness compilers

**Files:**
- Create: `control/src/vonk_control/harnesses/vllm.py`
- Create: `control/src/vonk_control/harnesses/sglang.py`
- Create: `control/src/vonk_control/harnesses/tensorrt_llm.py`
- Create: `control/src/vonk_control/harnesses/llama_cpp.py`
- Create: `control/src/vonk_control/harnesses/ds4.py`
- Create: `control/src/vonk_control/harnesses/diffusers.py`
- Create: `control/src/vonk_control/harnesses/comfyui.py`
- Create: `control/src/vonk_control/harnesses/pytorch_pipeline.py`
- Delete: `control/src/vonk_control/runtime_compilers/`
- Create: `config/execution-harnesses/vllm.json`
- Create: `config/execution-harnesses/sglang.json`
- Create: `config/execution-harnesses/tensorrt-llm.json`
- Create: `config/execution-harnesses/llama-cpp.json`
- Create: `config/execution-harnesses/ds4.json`
- Create: `config/execution-harnesses/diffusers.json`
- Create: `config/execution-harnesses/comfyui.json`
- Create: `config/execution-harnesses/pytorch-pipeline.json`
- Create: `control/tests/test_builtin_harnesses.py`

**Interfaces:**
- Consumes: Task 4 compiler protocol, strict structured argument helpers, interface adapters, and conformance runner.
- Produces: eight registered compilers with `contract_version = 1` and exact catalog documents.

- [ ] **Step 1: Write failing parameterized compiler tests**

```python
@pytest.mark.parametrize("slug", [
    "vllm", "sglang", "tensorrt-llm", "llama-cpp",
    "ds4", "diffusers", "comfyui", "pytorch-pipeline",
])
def test_builtin_harness_emits_a_shell_free_projection(slug: str) -> None:
    projection = registry.compile(slug, fixture_for(slug))
    assert projection.entrypoint
    assert projection.shell is False
    assert projection.contract_version == 1
```

Add per-engine tests for accepted flags, rejected flags, topology requirements, interface compatibility, output mounts, and environment allowlists.

- [ ] **Step 2: Confirm all eight registrations are absent**

Run: `uv run --project control --frozen python -m pytest control/tests/test_builtin_harnesses.py -q`

Expected: FAIL because the compilers are not registered.

- [ ] **Step 3: Port vLLM, SGLang, and llama.cpp safely**

Move their proven tokenization, placeholder, flag, environment, numeric-bound, and shell-metacharacter rejection into `harnesses/`. Change input from `WorkloadRunSource` to resolved recipe values without relaxing any allowlist.

- [ ] **Step 4: Add TensorRT-LLM and DS4**

Require exact executable paths, read-only model mount, numeric rank/world size, declared endpoint port, and topology consistency. TensorRT-LLM accepts only documented `trtllm-serve` options. DS4 invokes `/opt/vonk/bin/ds4-serve`; it is an engine harness, not a model adapter.

- [ ] **Step 5: Add Diffusers, ComfyUI, and generic PyTorch pipeline**

Use job interfaces with read-only model mounts and separate writable output mounts. Require one declared validator per output MIME type. ComfyUI workflow JSON is immutable recipe input. Generic PyTorch entrypoints are inside the signed source bundle and never invoke a shell.

- [ ] **Step 6: Seed and resolve built-in harness documents**

Each document declares contract version 1, compiler slug, topology modes, interfaces, capability requirements, and bounded security exceptions. Load them through `CatalogEntityService`; do not maintain a second hard-coded metadata catalog.

- [ ] **Step 7: Pass all built-in harness tests**

Run: `uv run --project control --frozen python -m pytest control/tests/test_builtin_harnesses.py control/tests/test_harness_registry.py control/tests/test_harness_conformance.py control/tests/test_interface_adapters.py -q`

Expected: PASS for all eight harnesses.

- [ ] **Step 8: Commit**

```bash
git add control/src/vonk_control/harnesses config/execution-harnesses control/tests/test_builtin_harnesses.py
git rm -r control/src/vonk_control/runtime_compilers
git commit -m "feat: implement built-in execution harnesses"
```

### Task 6: Reconcile the merged Fleet and Library experience with v1

**Files:**
- Modify: `control/src/vonk_control/library_contract.py`
- Modify: `control/src/vonk_control/library_projection.py`
- Modify: `control/src/vonk_control/fleet_projection.py`
- Modify: `control/src/vonk_control/recipe_api.py`
- Modify: `control/src/vonk_control/recipe_action_plans.py`
- Modify: `control/web/src/api/`
- Modify: `control/web/src/components/`
- Modify: `control/web/src/pages/`
- Modify: `control/web/src/lib/`
- Modify: `control/web/src/test-fixtures/`
- Modify: `control/openapi.json`
- Modify: `src/cluster_profiles/generated_control/`
- Modify: focused backend, web, generated-client, and browser tests covering these surfaces

**Interfaces:**
- Consumes: the merged responsive Fleet and Library presentation from `origin/main` plus the replacement one-topology recipe and exact catalog entity contracts.
- Produces: Fleet, Library, preview, and action payloads that expose `topology_name`, never ask an operator to select a profile, and visualize exact model-version, harness, runtime-distribution, and optional patch identities.

- [ ] **Step 1: Add failing v1 projection and browser tests**

Cover one topology per recipe, topology-derived rank placement, exact execution identities, one install choice per recipe revision, and the absence of `deployment_profiles`, `profile_name`, and `runtime.adapter` in active UI and API payloads.

- [ ] **Step 2: Confirm the merged prototype assumptions fail**

Run: `uv run --project control --frozen python -m pytest control/tests/test_library_projection.py control/tests/test_library_api.py control/tests/test_fleet_projection.py control/tests/test_recipe_api.py -q`

Run the focused web tests defined by `control/web/package.json`.

Expected: FAIL because the merged experience still projects deployment profiles and the prototype runtime adapter.

- [ ] **Step 3: Replace profile projection with the exact topology**

Rename public and internal placement fields to `topology_name`. Compute one placement section from `recipe_topology(document)`. Mapping previews and applies take recipe revision, nodes, and parameters only; topology identity comes from the immutable recipe revision.

- [ ] **Step 4: Render exact v1 execution identity**

The Library detail view presents the selected model version, execution harness, runtime distribution, optional patch bundle, interfaces, topology, resources, and lifecycle ordering from the strict v1 document. Keep the merged visual design and responsive behavior.

- [ ] **Step 5: Regenerate OpenAPI and clients**

Run the repository generators rather than hand-editing generated artifacts. Assert generated clients contain `topology_name` and no prototype profile-selection fields.

- [ ] **Step 6: Pass focused backend and web suites**

Run the backend tests from Step 2 plus recipe operation, admission, route, OpenAPI-client, and admin-equivalence tests. Run the web unit suite and the Fleet/Library browser slice.

- [ ] **Step 7: Commit**

```bash
git add control src/cluster_profiles/generated_control tests/control tests/e2e
git commit -m "refactor: align the control experience with recipe v1"
```

### Task 7: Create the fresh pre-production v1 schema and remove the prototype

**Files:**
- Create: `control/migrations/versions/0027_execution_harness_catalog.py`
- Create: `control/tests/test_execution_harness_catalog_migration.py`
- Delete: `control/tests/test_recipe_catalog_migration.py`
- Modify: `schemas/global/contract.lock.json`
- Modify: `scripts/update-global-contracts`
- Modify: `control/src/vonk_control/catalog_seeds.py`
- Modify: every remaining source returned by `rg -l 'deployment_profiles|profile_name|mia_dsv4_flash|ds4_smoke' control schemas config scripts deploy tests`

**Interfaces:**
- Consumes: new entity models and `ClusterMapping.topology_name`.
- Produces: Alembic head `0027_execution_harness_catalog`, based on merged head `0026_telemetry_maintenance_state`, with no prototype state, compatibility layer, or data-conversion path.

- [ ] **Step 1: Write a failing fresh-schema migration test**

```python
def test_fresh_database_reaches_the_v1_catalog_head(connection) -> None:
    upgrade_fresh_database_to_head(connection)
    assert scalar(connection, "select count(*) from local_recipe_revisions") == 0
    assert table_exists(connection, "catalog_entities")
    assert table_exists(connection, "catalog_entity_revisions")
    assert column_exists(connection, "cluster_mappings", "topology_name")
    assert not column_exists(connection, "cluster_mappings", "profile_name")
```

- [ ] **Step 2: Confirm revision 0027 is absent**

Run: `uv run --project control --frozen python -m pytest control/tests/test_execution_harness_catalog_migration.py -q`

Expected: FAIL because the cutover revision does not exist.

- [ ] **Step 3: Implement the post-merge v1 schema head**

Set `down_revision` to `0026_telemetry_maintenance_state`. Create the entity tables and replace `profile_name` with `topology_name` in the schema reached by a fresh database. Do not translate, preserve, or test upgrade of prototype recipe, catalog, installation, run, route, acceptance, user, session, or agent rows. The development deployment is reset and re-enrolled before this schema is used.

- [ ] **Step 4: Remove every prototype semantic path**

Run: `rg -n 'deployment_profiles|profile_name|mia_dsv4_flash|ds4_smoke' control schemas config scripts deploy tests`

Expected after edits: no matches except migration test setup that explicitly names discarded historical columns.

- [ ] **Step 5: Regenerate locks and pass migration tests**

Run: `scripts/update-global-contracts`

Run: `uv run --project control --frozen python -m pytest control/tests/test_execution_harness_catalog_migration.py control/tests/test_admission_migration.py control/tests/test_wheel_runtime_assets.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add control/migrations control/src control/tests schemas scripts config deploy tests
git commit -m "refactor: replace prototype recipe state"
```

### Task 8: Recreate DS4 and Mia as native v1 recipes

**Files:**
- Rewrite: `adapters/deepseek/ds4/`
- Rewrite: `adapters/deepseek/mia-vllm/`
- Create: `config/model-groups/deepseek-flash.json`
- Create: `config/models/deepseek-v4-flash-0731.json`
- Create: `config/model-versions/deepseek-v4-flash-0731-ds4.json`
- Create: `config/model-versions/deepseek-v4-flash-0731-official.json`
- Create: `config/runtime-distributions/ds4-spark.json`
- Create: `config/runtime-distributions/anemll-vllm-mia.json`
- Create: `config/patch-bundles/mia-deepseek-v4-flash-0731.json`
- Create: `config/recipes/deepseek-v4-flash-0731-ds4-single.json`
- Create: `config/recipes/deepseek-v4-flash-0731-mia-dual.json`
- Delete: `config/recipes/development/model-smoke.json`
- Delete: `config/recipes/development/model-smoke.context/`
- Delete: `config/recipes/development/mia-deepseek-v4-flash.json`
- Delete: `config/recipes/development/mia-deepseek-v4-flash.context/`
- Rewrite: `control/tests/test_development_recipe_fixture.py`
- Create: `tests/recipes/test_deepseek_v4_flash_ds4.py`
- Rewrite: `tests/recipes/test_mia_deepseek_v4_flash.py`
- Create: `scripts/recipe-source-bundle`
- Create: `scripts/qualify-recipe`
- Create: `scripts/tests/test_recipe_source_bundle.py`
- Create: `scripts/tests/test_qualify_recipe.py`

**Interfaces:**
- Consumes: DS4 and vLLM harnesses, exact entities, source-bundle build policy, one-node and two-node topologies, and OpenAI validation.
- Produces: one exact single-Spark DS4 recipe and one exact genuinely distributed two-Spark Mia recipe.

- [ ] **Step 1: Write failing DS4 identity tests**

```python
def test_ds4_recipe_is_one_node_and_uses_ds4_harness() -> None:
    recipe = load_recipe("deepseek-v4-flash-0731-ds4-single")
    assert recipe["topology"]["mode"] == "single"
    assert recipe["topology"]["node_count"] == 1
    assert resolve(recipe["execution"]["harness"])["compiler"] == "ds4"
    assert resolve_primary(recipe)["format"]["container"] == "gguf"
    assert resolve_primary(recipe)["format"]["quantization"] == "iq2_xxs-q2_k-mixed"
```

- [ ] **Step 2: Write failing Mia decomposition tests**

```python
def test_mia_is_vllm_distribution_plus_patch() -> None:
    recipe = load_recipe("deepseek-v4-flash-0731-mia-dual")
    assert resolve(recipe["execution"]["harness"])["compiler"] == "vllm"
    assert resolve(recipe["execution"]["runtime_distribution"])["identity"]["slug"] == "anemll-vllm-mia"
    assert resolve(recipe["execution"]["patch_bundle"])["identity"]["slug"] == "mia-deepseek-v4-flash-0731"
    assert recipe["topology"]["mode"] == "distributed"
    assert recipe["topology"]["node_count"] == 2
    assert recipe["topology"]["parallelism"]["world_size"] == 2
```

- [ ] **Step 3: Confirm both native recipes are absent**

Run: `uv run --frozen python -m pytest tests/recipes/test_deepseek_v4_flash_ds4.py tests/recipes/test_mia_deepseek_v4_flash.py control/tests/test_development_recipe_fixture.py scripts/tests/test_recipe_source_bundle.py scripts/tests/test_qualify_recipe.py -q`

Expected: FAIL because the new entity documents and recipes do not exist.

- [ ] **Step 4: Author DS4 exact entities, source bundle, and recipe**

Use the researched immutable DS4 source and DeepSeek weight revisions. The current
128-GB Spark default is the imatrix GGUF with routed expert gate/up tensors at
`IQ2_XXS` and down tensors at `Q2_K`; do not mislabel it as NVFP4. Record every
artifact hash and size, source URL, derivation, exact mixed quantization, license/access
facts, one-node resources, no-fabric topology, build host allowlist, offline runtime,
and bounded OpenAI validation. No `ds4_smoke` identity remains.

- [ ] **Step 5: Author Mia exact model, distribution, patch, and recipe**

Pin the latest approved Mia upstream commit, Anemll/vLLM distribution, every patch hash, post-patch tree digest, build host allowlist, NCCL/fabric requirements, ranks, readiness, failure withdrawal, exact official weights, and two-node resources. Patches apply and verify during image build; startup performs no patching or network access. No `mia_dsv4_flash` identity remains.

- [ ] **Step 6: Build deterministic source bundles and pass structural tests**

Run: `scripts/recipe-source-bundle adapters/deepseek/ds4 --output-dir .artifacts/recipe-sources/ds4`

Run: `scripts/recipe-source-bundle adapters/deepseek/mia-vllm --output-dir .artifacts/recipe-sources/mia`

Run: `uv run --frozen python -m pytest tests/recipes/test_deepseek_v4_flash_ds4.py tests/recipes/test_mia_deepseek_v4_flash.py control/tests/test_development_recipe_fixture.py -q`

Expected: PASS and repeated bundle builds produce identical digests.

- [ ] **Step 7: Run container and synthetic distributed acceptance**

Run: `scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --level container`

Run: `scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --level container`

Expected: PASS for ARM64 build, non-root policy, declared networking, offline fixture, health, invoke, bounded stop, restart, Mia collective, endpoint-owner readiness, rank-loss withdrawal, and recovery.

- [ ] **Step 8: Commit**

```bash
git add adapters/deepseek config control/tests/test_development_recipe_fixture.py tests/recipes scripts/recipe-source-bundle scripts/qualify-recipe scripts/tests
git commit -m "feat: recreate DS4 and Mia as v1 recipes"
```

### Task 9: Fresh reset, physical acceptance, and operator documentation

**Files:**
- Create: `scripts/reset-development-recipe-domain`
- Create: `scripts/accept-recipe`
- Create: `scripts/tests/test_reset_development_recipe_domain.py`
- Create: `scripts/tests/test_accept_recipe.py`
- Create: `docs/operators/execution-harnesses.md`
- Create: `docs/operators/model-catalog.md`
- Modify: `docs/runbooks/development-nas-installation.md`
- Modify: `docs/runbooks/fresh-development-install.md`
- Modify: `docs/vonk-forge-architecture.html`
- Modify: `scripts/run-development-slices`

**Interfaces:**
- Consumes: replacement contract, built-in harnesses, DS4 and Mia recipes, NAS development deployment, and enrolled Sparks.
- Produces: reproducible clean-reset and acceptance commands plus fresh one-node DS4 and two-node Mia evidence.

- [ ] **Step 1: Write failing safety and evidence tests**

```python
def test_reset_requires_exact_confirmation(run_script) -> None:
    result = run_script("scripts/reset-development-recipe-domain")
    assert result.returncode != 0
    assert "--confirm-destructive-preproduction-reset" in result.stderr


def test_acceptance_never_overstates_available_nodes(run_script) -> None:
    result = run_script(
        "scripts/accept-recipe", "--recipe", FOUR_NODE_RECIPE,
        "--nodes", "dgx-spark-1,dgx-spark-2",
    )
    assert result.returncode != 0
    assert "requires exactly 4 nodes" in result.stderr
```

- [ ] **Step 2: Confirm the scripts are absent**

Run: `uv run --frozen python -m pytest scripts/tests/test_reset_development_recipe_domain.py scripts/tests/test_accept_recipe.py -q`

Expected: FAIL because the scripts do not exist.

- [ ] **Step 3: Implement bounded reset and acceptance scripts**

The reset script requires the exact development-only confirmation flag, stops Vonk Forge workloads, removes the complete pre-production Vonk Forge control database and runtime state, applies Alembic head to a fresh database, seeds the supported v1 catalog, and requires users and agents to be created or enrolled again. It may retain only independently content-addressed model/build caches after verifying their digests. The acceptance script validates exact node count, records node/image/entity identities, executes each ladder phase, stores canonical evidence, and never advances state beyond completed evidence.

- [ ] **Step 4: Publish repository and website-ready explanations**

Document model group/model/model version/recipe, harness/distribution/patch, one versus many Sparks, replicas versus distributed execution, custom recipes, license responsibility, clean install/reset, install/invoke/stop/update, exact-revision rollback, and acceptance evidence. Update the HTML architecture overview with catalog resolution and interface-specific publication paths.

- [ ] **Step 5: Pass local script and documentation tests**

Run: `uv run --frozen python -m pytest scripts/tests/test_reset_development_recipe_domain.py scripts/tests/test_accept_recipe.py tests/test_docs_contract.py -q`

Expected: PASS.

- [ ] **Step 6: Run non-destructive preflight on the actual Sparks**

Run: `scripts/accept-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --nodes dgx-spark-1 --preflight-only`

Run: `scripts/accept-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --nodes dgx-spark-1,dgx-spark-2 --preflight-only`

Expected: PASS for architecture, driver, native NVIDIA container runtime, disk, memory, fabric, image access, artifact access, and exact topology.

- [ ] **Step 7: Perform the clean development recipe-domain reset**

Run: `scripts/reset-development-recipe-domain --environment development --confirm-destructive-preproduction-reset`

Expected: supported v1 catalog seeded, prototype recipe state absent, and all
pre-production users, sessions, and agent enrollments removed. Recreate the
development administrator and re-enroll both Sparks before physical acceptance;
no pre-reset browser login or enrollment is treated as valid.

- [ ] **Step 8: Run fresh physical acceptance**

Run: `scripts/accept-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --nodes dgx-spark-1 --level spark`

Run: `scripts/accept-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --nodes dgx-spark-1,dgx-spark-2 --level spark`

Expected: each reaches `spark-accepted` only after exact download verification, offline restart, inference, evidence validation, stop/restart recovery, and route publication/withdrawal; Mia additionally passes rank-loss withdrawal and recovery.

- [ ] **Step 9: Run complete verification**

Run: `uv run --project control --frozen python -m pytest control/tests -q`

Run: `uv run --frozen python -m pytest tests scripts/tests -q`

Run: `uv run --project agent --frozen python -m pytest agent/tests -q`

Run: `cargo test --manifest-path agent/rust/Cargo.toml --all-targets`

Run: `git diff --check && git status --short`

Expected: all suites pass and only intentional implementation files are changed.

- [ ] **Step 10: Commit**

```bash
git add docs scripts
git commit -m "docs: publish execution harness operations"
```

## Follow-on implementation plans

After this foundation is physically accepted, create and execute separate TDD plans in this order:

1. language, reasoning, coding, and multimodal-understanding model versions and recipes;
2. image-generation and image-editing model versions and recipes;
3. video- and audio-generation model versions and recipes;
4. 3D-generation and rigging model versions and recipes;
5. curated defaults, alternatives, blocked-upstream tracker, website catalog, and fresh-reset default policy.

Each plan reuses these contracts and acceptance tools. It may add a runtime distribution or recipe-local patch bundle. It adds a new harness only when an accepted target cannot implement the universal lifecycle through one of the eight built-ins.
