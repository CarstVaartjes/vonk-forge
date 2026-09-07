# Source-First Recipes and Cluster Mapping Implementation Plan

> Retired historical plan. The authoring/import portions are preserved for
> provenance only; the active platform uses the canonical managed library.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the unreleased image-first recipe draft with one source-first v1 contract, build each workload once on a GPU node, distribute that exact OCI result, and bind portable deployment profiles to first-class local cluster mappings.

**Architecture:** Recipe JSON and an immutable source bundle are the portable input. A local build record binds those inputs to one OCI digest; a cluster mapping binds one recipe profile to exact GPU node identities, roles, and ranks; installations and fenced runs consume that mapping without changing recipe meaning. The global service stores recipe metadata plus source bundles and never requires a community workload registry.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/PostgreSQL, JSON Schema 2020-12, Rust 1.97, rootless Podman/Buildah, React/TypeScript, pytest, cargo test, Vitest, Playwright.

## Global Constraints

- Source-first is the sole `schema_version: 1`; add no recipe migration or image-first execution path.
- Every recipe references one content-addressed source bundle containing its Dockerfile and build context.
- Builds receive no Docker socket, host mounts, GPU device, credentials, private-network access, privileged mode, or additional Linux capabilities.
- One builder produces one OCI digest; every node in a mapping imports that exact digest.
- Deployment profiles use exact positive node counts with no power-of-two assumption.
- Recipe, cluster mapping, installed content, and fenced run remain separate authorities.
- Local PostgreSQL remains authoritative and Git is never a recipe execution gate.
- Unknown disk or memory blocks the corresponding action and is never zero.
- WorkloadRun is translated, never invoked as Vonk's scheduler.
- Global publication stores source bundles and evidence, not workload image layers or registry credentials.

---

### Task 1: Replace the shared contract and store canonical source bundles

**Files:**
- Modify (`vonk-forge-web`): `schemas/recipe/v1.schema.json`
- Modify (`vonk-forge-web`): `schemas/test-report/v1.schema.json`
- Modify (`vonk-forge-web`): `schemas/fixtures/recipe-v1-minimal.json`
- Modify (`vonk-forge-web`): `schemas/fixtures/recipe-v1-multinode.json`
- Modify (`vonk-forge-web`): `api/src/vonk_catalog/contracts.py`
- Create (`vonk-forge-web`): `api/src/vonk_catalog/source_bundles.py`
- Modify (`vonk-forge-web`): `api/src/vonk_catalog/models.py`
- Modify (`vonk-forge-web`): `api/migrations/versions/0001_catalog_foundation.py`
- Create (`vonk-forge-web`): `api/tests/test_source_bundles.py`
- Modify (`vonk-forge-web`): `api/tests/test_recipe_contract.py`
- Modify (`vonk-forge-web`): `api/tests/test_migration.py`
- Modify (`vonk-forge`): `schemas/global/recipe-v1.schema.json`
- Modify (`vonk-forge`): `schemas/global/test-report-v1.schema.json`
- Modify (`vonk-forge`): `control/tests/fixtures/global/recipe-v1-minimal.json`
- Modify (`vonk-forge`): `control/tests/fixtures/global/recipe-v1-multinode.json`
- Modify (`vonk-forge`): `control/src/vonk_control/recipe_contract.py`
- Create (`vonk-forge`): `control/src/vonk_control/source_bundles.py`
- Modify (`vonk-forge`): `control/src/vonk_control/models.py`
- Create (`vonk-forge`): `control/tests/test_source_bundles.py`
- Modify (`vonk-forge`): `control/tests/test_recipe_contract.py`
- Modify (`vonk-forge`): `control/tests/test_recipe_catalog_migration.py`

**Interfaces:**
- `deployment_profile(document: Mapping[str, object], name: str) -> Mapping[str, object]`
- `validate_recipe_semantics(document: Mapping[str, object]) -> None`
- `inspect_source_bundle(payload: BinaryIO, limits: BundleLimits) -> BundleManifest`
- `SourceBundleStore.put(expected_sha256: str, payload: BinaryIO) -> StoredBundle`

- [ ] **Step 1: Write failing contract and bundle tests**

```python
def test_three_node_source_recipe_is_valid(recipe_fixture) -> None:
    recipe = recipe_fixture("recipe-v1-multinode.json")
    assert recipe["build"]["context"]["sha256"] == "a" * 64
    assert recipe["deployment_profiles"][0]["node_count"] == 3
    validate_recipe(recipe)


def test_runtime_image_is_not_a_recipe_field(recipe_fixture) -> None:
    recipe = recipe_fixture("recipe-v1-minimal.json")
    recipe["runtime"]["image"] = "registry.example/x:latest"
    with pytest.raises(RecipeContractError):
        validate_recipe(recipe)


@pytest.mark.parametrize("name", ["/etc/passwd", "../escape", "a/../../escape"])
def test_bundle_rejects_paths_outside_context(name: str, tar_bytes) -> None:
    with pytest.raises(SourceBundleError):
        inspect_source_bundle(BytesIO(tar_bytes({name: b"x"})), LIMITS)
```

- [ ] **Step 2: Run tests and verify RED**

Run in `vonk-forge`: `uv run --project control pytest control/tests/test_recipe_contract.py control/tests/test_source_bundles.py -q`.

Run in `vonk-forge-web`: `uv run --project api pytest api/tests/test_recipe_contract.py api/tests/test_source_bundles.py -q`.

Expected: failures because `runtime.image` remains required and source-bundle modules do not exist.

- [ ] **Step 3: Implement the source-first schema and semantics**

Require `build`, `parameters`, `artifacts`, `runtime`, `deployment_profiles`, `validation`, and `provenance`. Require build context `{sha256, expected_bytes, media_type}`, a bundle-relative Dockerfile, `linux/arm64`, immutable artifacts, typed runtime argv/environment/security, and one or more exact profiles. Cross-field validation proves role counts sum to `node_count`, one role owns the endpoint, names and parameter references are unique, artifact role references exist, and unified memory is not duplicated.

- [ ] **Step 4: Implement canonical bundle inspection and atomic storage**

Normalize regular-file/directory paths, modes, file hashes, and sizes into canonical JSON. Reject links, devices, sockets, duplicate normalized paths, traversal, oversize files/archives, excess file count, and compression bombs. Stream to a same-filesystem temporary file, verify canonical digest, fsync, and rename to a digest key; database JSON stores only identity and manifest metadata.

- [ ] **Step 5: Verify, regenerate contract locks, and commit**

Run both scoped suites plus canonicalization and migration tests. Regenerate the vendored global contract with existing scripts and run `git diff --check` in both repositories.

Commit in each repository: `git commit -m "feat: define source-first recipe contract"`.

### Task 2: Make catalog metadata and WorkloadRun imports source-first

**Files:**
- Modify: `control/src/vonk_control/catalog_service.py`
- Modify: `control/src/vonk_control/catalog_api.py`
- Modify: `control/src/vonk_control/artifact_sizes.py`
- Modify: `control/src/vonk_control/workload_run_source.py`
- Modify: `control/src/vonk_control/import_report.py`
- Modify: `control/src/vonk_control/workload_run_importer.py`
- Modify: `control/src/vonk_control/import_resolution.py`
- Modify: `control/src/vonk_control/workload_run_workflow.py`
- Modify: `control/src/vonk_control/runtime_compilers/common.py`
- Modify: `control/tests/test_catalog_service.py`
- Modify: `control/tests/test_catalog_api.py`
- Modify: `control/tests/test_artifact_sizes.py`
- Modify: `control/tests/test_workload_run_source.py`
- Modify: `control/tests/test_workload_run_importer.py`
- Modify: `control/tests/test_import_resolution.py`
- Modify: `control/tests/test_workload_run_api.py`
- Add broad profiles under: `control/tests/fixtures/workload_run/`

**Interfaces:**
- `RecipeSummary` exposes `source_bundle_sha256`, `profile_node_counts`, `maximum_installed_bytes_per_node`, and `maximum_runtime_memory_bytes_per_node`.
- `WorkloadRunImportResult` adds `bundle: GeneratedSourceBundle`.
- Import dispositions add `incorporated` and `resolved`.

- [ ] **Step 1: Write failing summary and import tests**

```python
def test_container_and_mods_become_source_bundle(parsed_profile) -> None:
    result = import_workload_run(parsed_profile)
    dockerfile = result.bundle.files["Dockerfile"].decode()
    assert dockerfile.startswith("FROM ghcr.io/example/vllm@sha256:")
    assert "COPY mods/" in dockerfile
    assert result.draft_document["build"]["context"]["sha256"] == result.bundle.sha256


def test_catalog_exposes_exact_profile_counts(summary) -> None:
    assert summary.profile_node_counts == (1, 3)
    assert not hasattr(summary, "runtime_image")
```

- [ ] **Step 2: Run scoped tests and verify RED**

Run: `uv run --project control pytest control/tests/test_catalog_service.py control/tests/test_catalog_api.py control/tests/test_workload_run_importer.py control/tests/test_import_resolution.py -q`.

- [ ] **Step 3: Implement profile-aware catalog projections**

Calculate sorted exact node counts and conservative per-node maxima from role envelopes. Bind test reports to recipe digest, source-bundle digest, build-input digest, derived image digest, and deployment profile. Keep image digest as evidence only.

- [ ] **Step 4: Generate a wrapper bundle during WorkloadRun import**

Resolve `container` to an immutable ARM64 base, generate a normalized Dockerfile, copy bounded mods/tuning under `/opt/vonk`, and express pre-start behavior as confined container argv. Convert exact node bounds to one profile; convert supported ranges to compiler-supported exact profiles, including count three. Every parsed leaf retains exactly one reported disposition.

- [ ] **Step 5: Cover the live WorkloadRun surface and commit**

Add fixtures for vLLM distributed/Ray, SGLang, llama.cpp, TensorRT-LLM, arbitrary defaults, environment references, runtime configuration, mods, tuning, and benchmarks. Run all WorkloadRun/compiler/import/catalog tests.

Commit: `git commit -m "feat: import WorkloadRun as source recipes"`.

### Task 3: Build once and replace deployments with cluster mappings

**Files:**
- Create: `control/src/vonk_control/source_policy.py`
- Create: `control/tests/test_source_policy.py`
- Create: `control/src/vonk_control/recipe_builds.py`
- Create: `control/tests/test_recipe_builds.py`
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/src/vonk_control/agent_jobs.py`
- Modify: `control/src/vonk_control/recipe_operation_worker.py`
- Modify: `rust/crates/vonk-agent/src/workloads.rs`
- Modify: `rust/crates/vonk-agent/src/executor.rs`
- Modify: `rust/crates/vonk-agent/tests/workloads.rs`
- Replace: `control/src/vonk_control/recipe_deployments.py` with `control/src/vonk_control/cluster_mappings.py`
- Create: `control/tests/test_cluster_mappings.py`
- Modify: `control/src/vonk_control/install_admission.py`
- Modify: `control/src/vonk_control/run_admission.py`
- Modify: `control/src/vonk_control/topology.py`
- Modify: `control/src/vonk_control/recipe_operations.py`
- Modify: `control/tests/test_agent_jobs.py`
- Modify: `control/tests/test_install_admission.py`
- Modify: `control/tests/test_run_admission.py`
- Modify: `control/tests/test_topology.py`
- Modify: `control/tests/test_recipe_operations.py`
- Modify: `control/tests/test_recipe_catalog_migration.py`

**Interfaces:**
- `inspect_build_source(recipe, bundle) -> SourcePolicyReport`
- `RecipeBuildService.plan(recipe_revision_id, builder_node_id, now) -> BuildPlan`
- Agent operations `recipe.build.v1` and `recipe.image.import.v1`
- `ClusterMappingService.plan(recipe_revision_id: str, profile_name: str, node_ids: tuple[str, ...], parameters: Mapping[str, object], now: datetime) -> ClusterMappingPlan`
- Install/run plans consume `mapping_id` and `mapping_generation`.

- [ ] **Step 1: Write failing build and three-node mapping tests**

```python
def test_build_plan_has_no_free_form_command(build_plan) -> None:
    assert build_plan.agent_payload["kind"] == "recipe.build.v1"
    assert "command" not in build_plan.agent_payload
    assert build_plan.agent_payload["limits"]["gpu"] == 0


def test_three_node_profile_maps_deterministic_ranks(service, recipe, nodes) -> None:
    plan = service.plan(recipe.id, "triple-tp3", tuple(reversed(nodes)), parameters={})
    assert [(node.rank, node.role) for node in plan.nodes] == [
        (0, "entrypoint"), (1, "worker"), (2, "worker")
    ]
```

- [ ] **Step 2: Run Python/Rust tests and verify RED**

Run: `uv run --project control pytest control/tests/test_recipe_builds.py control/tests/test_cluster_mappings.py control/tests/test_install_admission.py control/tests/test_run_admission.py -q`.

Run: `cargo test -p vonk-agent --test workloads`.

- [ ] **Step 3: Implement durable build planning and typed agent execution**

Require a resolved recipe, verified bundle, compatible fresh builder, complete build envelope, and a passing source-policy report before sending build inputs to a node. Parse the Dockerfile and any Compose files in the canonical bundle; reject unpinned bases, remote `ADD`, root final users, host/privileged networking or namespaces, Docker sockets, host bind mounts, added capabilities, unconfined security profiles, secret/SSH build mounts, and paths outside the context. Reserve temporary capacity. Build through rootless typed Podman/Buildah argv with no shell/socket/host mounts/devices/secrets/privilege, public-only egress, and bounded CPU/memory/disk/process/time/output. Recheck the policy on the agent and record input and OCI output digests and sizes.

- [ ] **Step 4: Distribute one exact OCI layout**

Export/store one OCI layout, transfer it through the authenticated artifact operation, verify before and after import, and record `NodeArtifact(kind="image")`. A mapping cannot dispatch more than one build result digest.

- [ ] **Step 5: Rename the unreleased deployment domain directly**

Use initial tables/classes `ClusterMapping` and `ClusterMappingNode`, not compatibility aliases. Persist recipe revision, selected profile, generation, parameters, exact nodes, roles, ranks, placement digest, and endpoint owner. Install and run acceptance rejects stale mapping generations.

- [ ] **Step 6: Make capacity and topology profile-aware**

Place artifacts and disk per role; reserve startup, steady, growth, and system memory by declared memory kind; count GPU node unified memory once. Validate `none`, `connected`, `full_mesh`, and `switch` fabric requirements for arbitrary positive counts. Test a three-node group fence and compensation.

- [ ] **Step 7: Verify and commit**

Run build, agent, mapping, topology, install, run, route, operation, and migration tests.

Commit: `git commit -m "feat: build recipes and map them to clusters"`.

### Task 4: Publish source bundles and expose unified authoring/mapping UX

**Files (`vonk-forge-web`):**
- Modify: `api/src/vonk_catalog/drafts.py`
- Modify: `api/src/vonk_catalog/draft_api.py`
- Modify: `api/src/vonk_catalog/publication.py`
- Modify: `api/src/vonk_catalog/public_api.py`
- Modify: `api/src/vonk_catalog/search.py`
- Modify: `api/tests/test_drafts.py`
- Modify: `api/tests/test_publication.py`
- Modify: `api/tests/test_public_api.py`
- Modify: `api/tests/test_search.py`
- Modify: `web/src/pages/draft-editor.tsx`
- Modify: `web/src/pages/recipe-detail.tsx`

**Files (`vonk-forge`):**
- Modify: `control/src/vonk_control/global_catalog.py`
- Modify: `control/src/vonk_control/catalog_service.py`
- Modify: `control/tests/test_global_catalog_bridge.py`
- Create: `control/web/src/pages/recipe-create.tsx`
- Create: `control/web/src/pages/recipe-source.tsx`
- Create: `control/web/src/pages/cluster-mapping.tsx`
- Create tests beside each page
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/api/client.ts`

**Interfaces:**
- Draft upload accepts recipe JSON and one digest-bound source bundle.
- Public revision download returns immutable recipe and bundle metadata/content.
- `/catalog/new` offers WorkloadRun import, standard clone, and fully custom.
- `/catalog/:recipeId/map` previews and accepts one cluster mapping.

- [ ] **Step 1: Write failing publication and authoring tests**

```python
def test_publication_requires_matching_verified_bundle(draft, publication) -> None:
    with pytest.raises(Problem) as error:
        publication.publish(USER, "alice", draft.id, idempotency_key="pub")
    assert error.value.code == "publication.source_bundle_missing"
```

```tsx
it.each(["Import WorkloadRun", "Start from a standard", "Fully custom"])(
  "%s enters the same editor",
  async (choice) => {
    render(<RecipeCreate />);
    await userEvent.click(screen.getByRole("button", { name: choice }));
    expect(await screen.findByRole("heading", { name: "Recipe source" })).toBeVisible();
  },
);
```

- [ ] **Step 2: Run API/bridge/Vitest tests and verify RED**

Run global draft/publication/public/search tests, local global bridge tests, and the new UI tests.

- [ ] **Step 3: Attach verified bundles transactionally**

Bind bundle digest and bytes to private drafts and immutable revisions. Remove `runtime.image` registry checks from publication; validate source, Dockerfile policy, artifacts, exact profiles, resources, attribution, and evidence. Keep registry resolution only for imported upstream base images.

- [ ] **Step 4: Download source into local authority**

Local import fetches recipe and bundle with strict bounds, validates both, stores both by digest, and remains usable after global outage. Search indexes exact profile counts and conservative resource maxima.

- [ ] **Step 5: Implement one authoring editor and explicit mapping preview**

All origins edit overview, source, artifacts, parameters, runtime, profiles, resources, validation, and provenance. Mapping preview shows exact GPU nodes, arbitrary count, roles, ranks, fabric edges, reusable content, transfers, build/install disk, unified memory, safety margins, and blockers. Accepting a mapping never implicitly starts a run.

- [ ] **Step 6: Verify and commit in each repository**

Run API, bridge, UI accessibility, three-node display, stale-generation, offline, and web build tests.

Commit: `git commit -m "feat: publish and map source recipes"`.

### Task 5: Prove the lifecycle end to end

**Files:**
- Create: `tests/e2e/test_source_first_recipe.py`
- Create: `tests/e2e/test_three_node_mapping.py`
- Modify: `docs/runbooks/recipe-lifecycle.md`
- Modify: `docs/runbooks/end-to-end-acceptance.md`

**Interfaces:**
- One trace binds recipe, bundle, build-input, OCI, mapping-generation, installation, run-fence, route-generation, and evidence digests.

- [ ] **Step 1: Write the failing simulator lifecycle**

Cover all three authoring origins, source build, global publish/download, offline rebuild, exact-image distribution, one- and three-node mappings, disk/memory blockers, install, start, route, inference, stop, and rerun from one mapping.

- [ ] **Step 2: Run simulator acceptance and verify RED**

Run the new tests against disposable PostgreSQL and simulated agents.

- [ ] **Step 3: Close transport integration gaps**

Expose deterministic simulated inventory, build result, transfer failure, rank failure, and global outage through existing test transport interfaces. Do not add test-only branches to production admission or orchestration.

- [ ] **Step 4: Run full verification**

In `vonk-forge`, run `uv run --project control pytest control/tests -q`, `cargo test --workspace`, `npm --prefix control/web test -- --run`, `npm --prefix control/web run build`, and `git diff --check`.

In `vonk-forge-web`, run `uv run --project api pytest api/tests -q`, `npm --prefix web test -- --run`, `npm --prefix web run build`, and `git diff --check`.

- [ ] **Step 5: Record physical acceptance**

On one, two, and three compatible GPU nodes, record build isolation, one exact OCI digest, disk peak, unified-memory admission, deterministic ranks, fabric edges, group readiness, route publication, rank-failure withdrawal, and rerun from retained content.

- [ ] **Step 6: Commit evidence and runbooks**

Commit: `git commit -m "test: prove source-first recipe lifecycle"`.

## Completion criteria

Both repositories must pass their full suites and contract lock checks; all mapped nodes must verify one locally built OCI digest; arbitrary positive profile counts must have no count-specific assumptions; global publication must require source rather than a workload registry; and one-/two-/three-GPU node physical evidence must be recorded.
