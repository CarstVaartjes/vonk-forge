# Task 3 Report: One Exact Recipe Topology

## Status

Audited Task 3 from the requested base `abe2ddf` and implemented only the
remaining gaps. Task 2 review fixes already supplied the selector-free
`topology_name` cutover, current recipe documents, and the clean discarded-name
surface; this task did not recreate profile interfaces, Task 4's harness
registry, or Task 6's migration/reset.

Implementation commit: `66f73dd5931a4ddb7e3fe759443eff274f406dfe`
(`refactor: bind each recipe to one topology`).

## Audit matrix

| Task 3 requirement | Result | Evidence / action |
| --- | --- | --- |
| One selector-free exact topology | Green | Mapping persistence uses `topology_name`; both literal and concatenated-fragment scans below produced no matches for discarded profile/runtime-adapter terms. |
| Exact node count; no replica selector can substitute for a distributed topology | Green | `ClusterMappingService.plan()` rejects a selected count unequal to `topology.node_count`; regression covers it. There is no active replica/profile input or fallback path. |
| Role, rank, and endpoint owner are topology identities | Fixed | `validate_topology()` now compares each rank's `(role, endpoint_owner)` to the expanded exact topology. Mapping materialization repeats the validation and recomputes the placement identity before persistence. |
| Required runtime and fabric capability | Fixed | Mapping preview checks each selected node's claimed capability against the exact topology; run admission continues to check fresh inventory/fabric evidence. Regression covers missing full-mesh fabric. |
| Duplicate ranks and nodes | Green | Existing topology regression checks non-contiguous/duplicate ranks and duplicate nodes; database uniqueness is retained. |
| Endpoint ownership | Fixed | A forged endpoint owner is rejected at mapping materialization and again by install admission before install work can be queued. |
| Compiled runtime identity | Fixed | `compile_runtime_spec(document, resolved_entities, parameters, role, rank, recipe_build_id, image_digest)` now binds exact recipe/model-version/harness/distribution/optional-patch digests plus topology name, node count, role, and rank. The agent route resolves the same exact graph before returning a spec. |
| Build/install/run exact-dependency admission | Fixed | Shared exact-entity resolution verifies model-version lineage, harness/distribution compatibility, and optional patch/distribution compatibility. Build planning, install admission, run admission, and agent spec serving reject stale/unresolved graphs. |
| Accepted recipe, stale digest, resources, and mutable image identity | Green | Existing resolved-lifecycle, re-preview/re-lock, capacity, inventory, and OCI digest checks remain in force. New coverage proves the database rejects a mutable image identity and build planning rejects missing exact dependencies. |
| Install topology mismatch | Fixed | Install admission now validates persisted mapping placements and capabilities before producing an install plan; regression mutates endpoint ownership and observes rejection. |

## RED evidence

The initial targeted regression run after adding only gap tests was:

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_cluster_mappings.py \
  control/tests/test_topology.py \
  control/tests/test_install_admission.py \
  control/tests/test_recipe_runtime_specs.py -q
```

Result:

```text
5 failed, 11 passed in 6.32s
```

The genuine failures showed that mapping accepted missing fabric, forged
role/rank/endpoint data, and topology accepted a role moved to a different
rank; runtime compilation did not accept `resolved_entities` or `rank`. The
fifth failure showed the test attempted to store a mutable tag, which the
existing database constraint already rejected. That test was corrected to
record the pre-existing guard rather than adding redundant production logic.

Exact-dependency enforcement was independently reproduced at the agent
boundary:

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_agent_api.py::test_agent_rejects_recipe_spec_without_exact_resolved_dependencies \
  control/tests/test_install_admission.py::test_database_rejects_mutable_built_image_identity -q
```

Result before the dependency gate:

```text
1 failed, 1 passed in 1.88s
```

The agent returned `200` for a persisted recipe with no exact resolved catalog
entities. After self-review, install mismatch coverage was added first and
reproduced:

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_install_admission.py::test_install_rejects_mapping_with_wrong_endpoint_owner -q
```

Result before the install validation:

```text
1 failed in 1.00s
```

## GREEN evidence

Focused Task 3 suite:

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_cluster_mappings.py \
  control/tests/test_topology.py \
  control/tests/test_recipe_builds.py \
  control/tests/test_install_admission.py \
  control/tests/test_run_admission.py \
  control/tests/test_recipe_operations.py \
  control/tests/test_recipe_runtime_specs.py \
  control/tests/test_agent_api.py -q
```

Result:

```text
160 passed in 83.85s (0:01:23)
```

Focused regression during the install integration fix:

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_install_admission.py \
  control/tests/test_recipe_operations.py -q
```

Result:

```text
31 passed in 19.65s
```

Static and terminology verification:

```text
uvx ruff==0.16.1 check [15 scoped source/test files]
uvx ruff==0.16.1 format --check [15 scoped source/test files]
git diff --check
uv run --project control --frozen python -m compileall -q control/src/vonk_control [scoped tests]
rg -n 'deployment_profiles|deployment_profile|profile_name|profile_node_counts|profile_resources|profile_fabric|runtime\\.adapter|/runtime/adapter' control/src control/tests config scripts deploy tests
rg -n '"deployment_"|"_profiles"|"profile_"|"runtime\\.adapter"|"/runtime/adapter"|"deployment"\\s*\\+|"profile"\\s*\\+' control/src control/tests config scripts deploy tests
```

Results: Ruff reported `All checks passed!`; formatting reported `15 files
already formatted`; diff and compilation exited cleanly; both `rg` scans
produced no matches.

## Self-review

- Re-read every Task 3 brief requirement against the final source and tests.
- Confirmed the compiler signature accepts all required bindings and rejects a
  role that does not own the supplied rank.
- Confirmed topology is checked at preview, mapping persistence, install, and
  run boundaries; no caller-supplied rank, endpoint owner, or fabric claim can
  alter a persisted mapping unnoticed.
- Confirmed build/install/run admission re-resolves exact immutable dependency
  identities; compatibility is checked from the distribution and patch entity
  documents rather than inferred from names.
- Confirmed image identity remains content-addressed by both schema and build
  result validation.
- Reviewed the staged production/test diff for scope. It contains no harness
  registry, migration/reset, profile compatibility shim, or prototype alias.
- A reviewer subagent is unavailable in this harness, so the requirements and
  final diff review were performed in-session.

## Commits

- `66f73dd5931a4ddb7e3fe759443eff274f406dfe` —
  `refactor: bind each recipe to one topology`
- This report is committed separately as Task 3 audit evidence.

## Concerns

- No schema migration or destructive reset was added; Task 6 remains the
  explicit owner of that work.
- Task 3 validates `start_order` and `stop_order` as exact topology contract
  data. The existing gang-aware operation queue does not introduce a new
  scheduler/dependency mechanism to serialize those orders; adding such a
  mechanism would be a broader workflow change outside this topology/admission
  cutover.
