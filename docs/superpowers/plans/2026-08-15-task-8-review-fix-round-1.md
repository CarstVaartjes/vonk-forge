# Task 8 Review Fix Round 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve all eight authoritative Task 8 review findings with executable, fail-closed native-v1 behavior while preserving every immutable upstream, model, and OCI identity.

**Architecture:** Extend the v1 recipe/distribution contracts with a repository-relative build context and an exact distributed launch contract, project the rank-specific contract through the trusted vLLM compiler, and consume it in the agent container launch path. Replace the structural-only qualifier with a contract-driven container state machine and use the existing controller route authority plus bounded restart sequencing for rank-loss recovery. Redirect development acceptance to native-v1 entities, vendor required license material into the Mia image, and remove the redundant self-asserted distribution digest.

**Tech Stack:** Python 3.12, JSON Schema 2020-12, pytest, Rust/serde, Docker-compatible CLI, SQLAlchemy controller services.

## Global Constraints

- Preserve Mia source `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`, DS4 source `84cc882352757baf628a1776badf7cc54d584e28`, Anemll source `47503f8e38dadd4dededca798150db2619594fce`, and all existing model/image revisions, sizes, and SHA-256 values.
- Runtime and qualification remain deterministic and offline after installation; no startup patching or network fetches.
- Evidence is canonical and failures are fail-closed.
- Do not claim physical ARM64/GPU/DGX Spark acceptance; that remains Task 9.
- Do not add compatibility readers, aliases, or migration code.

---

### Task 1: Distributed launch and lifecycle contracts

**Files:**
- Modify: `schemas/global/recipe-v1.schema.json`
- Modify: `schemas/global/catalog-entity-v1.schema.json`
- Modify: `control/src/vonk_control/catalog_contract.py`
- Modify: `control/src/vonk_control/recipe_contract.py`
- Modify: `control/src/vonk_control/harnesses/contracts.py`
- Modify: `control/src/vonk_control/harnesses/common.py`
- Modify: `control/src/vonk_control/harnesses/vllm.py`
- Modify: `control/src/vonk_control/recipe_runtime_specs.py`
- Modify: `rust/crates/vonk-agent/src/workloads.rs`
- Modify: `rust/crates/vonk-agent/src/oci.rs`
- Test: `control/tests/test_recipe_contract.py`
- Test: `control/tests/test_recipe_runtime_specs.py`
- Test: `tests/recipes/test_mia_deepseek_v4_flash.py`
- Test: `rust/crates/vonk-agent/tests/workloads.rs`

**Interfaces:**
- Consumes: v1 recipe topology, resolved runtime-distribution capability, controller placement `local_address`, `master_address`, and `master_port`.
- Produces: validated `runtime.distributed` launch selection, rank-bound fabric environment, and agent launch arguments containing rendezvous plus HCA/socket/GID values.

- [x] **Step 1: Write failing contract and runtime projection tests**

Add tests which remove one rendezvous/fabric field at a time and require schema/compiler refusal, assert rank 0/1 receive literal `NCCL_SOCKET_IFNAME`, `NCCL_IB_HCA`, `NCCL_IB_GID_INDEX`, `TP_SOCKET_IFNAME`, and `GLOO_SOCKET_IFNAME`, and assert the emitted spec declares placement-sourced local/master address and port inputs.

- [x] **Step 2: Run tests to verify RED**

Run: `uv run --project control --frozen python -m pytest control/tests/test_recipe_contract.py control/tests/test_recipe_runtime_specs.py tests/recipes/test_mia_deepseek_v4_flash.py -q`

Expected: FAIL because the current schema and projection contain no structured distributed launch contract.

- [x] **Step 3: Implement minimal strict projection**

Add a closed distributed-launch schema whose capability binding is `distributed_vllm`, whose rendezvous sources are exactly placement local/master address and master port, and whose two rank profiles define the pinned Mia RoCE HCA/socket/GID contract. Extend `HarnessProjection` with a closed placement/fabric projection and make the Rust launcher translate only those validated values into container environment.

- [x] **Step 4: Run Python and Rust tests to verify GREEN**

Run: `uv run --project control --frozen python -m pytest control/tests/test_recipe_contract.py control/tests/test_recipe_runtime_specs.py tests/recipes/test_mia_deepseek_v4_flash.py -q`

Run: `cargo test -p vonk-agent --test workloads`

Expected: PASS.

### Task 2: Executable rank-loss recovery consumer

**Files:**
- Modify: `control/src/vonk_control/recipe_operations.py`
- Modify: `control/src/vonk_control/recipe_routes.py`
- Test: `control/tests/test_recipe_operations.py`
- Test: `control/tests/test_recipe_routes.py`

**Interfaces:**
- Consumes: authenticated rank observations and the recipe lifecycle policy `withdraw-endpoint` / `restart-worker-then-entrypoint`.
- Produces: immediate route withdrawal, bounded worker-before-entrypoint restart jobs, and republish only after every exact rank is healthy.

- [x] **Step 1: Write failing lifecycle behavior tests**

Exercise a published two-rank run, report rank 1 lost, and assert route withdrawal precedes queued stop/start recovery; then report only the worker healthy and assert no publication; finally report both ranks healthy and assert publication occurs.

- [x] **Step 2: Run tests to verify RED**

Run: `uv run --project control --frozen python -m pytest control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py -q`

Expected: FAIL because rank observations currently update state but do not coordinate a bounded restart.

- [x] **Step 3: Implement the bounded controller consumer**

Consume only the declared distributed failure policy, reuse immutable run/mapping authority, withdraw through `RecipeRouteService`, enqueue worker then entrypoint phases with the lifecycle timeout as a bound, and retain the existing all-ranks candidate gate before republishing.

- [x] **Step 4: Run tests to verify GREEN**

Run: `uv run --project control --frozen python -m pytest control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py -q`

Expected: PASS.

### Task 3: Contract-driven executable container qualifier

**Files:**
- Rewrite: `scripts/qualify-recipe`
- Modify: `scripts/tests/test_qualify_recipe.py`

**Interfaces:**
- Consumes: `build.context.path`, recipe interface/readiness/validation checks, topology roles/orders, and resolved distribution capabilities.
- Produces: canonical evidence for build, start, health, invocation, bounded stop/restart, and distributed collective/withdrawal/recovery checks for built-in or user-authored v1 recipes.

- [x] **Step 1: Write failing fake-engine behavior tests**

Use an executable bounded fake engine and HTTP fixture to require build, rank-ordered starts, endpoint-owner readiness after both ranks, chat invocation, collective evidence, rank removal, endpoint withdrawal, worker-then-entrypoint recovery, restart health, and bounded cleanup. Include a user-authored recipe slug to prove there is no built-in slug dispatch.

- [x] **Step 2: Run tests to verify RED**

Run: `uv run --project control --frozen python -m pytest scripts/tests/test_qualify_recipe.py -q`

Expected: FAIL because the current qualifier stops after architecture detection.

- [x] **Step 3: Implement the minimal engine state machine**

Resolve context and validators from the v1 documents, execute Docker-compatible build/run/inspect/stop/rm calls with subprocess timeouts, issue bounded HTTP health/chat checks, execute declared distributed transitions, canonicalize step evidence, and clean up in `finally`. Return exit 3 only for genuine host/hardware limitations and exit 1 for qualification failure.

- [x] **Step 4: Run focused tests and the local real architecture gate**

Run: `uv run --project control --frozen python -m pytest scripts/tests/test_qualify_recipe.py -q`

Run: `scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --level container`

Run: `scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --level container`

Expected: fake-engine tests PASS; local x86_64 reports environment-limited without claiming physical acceptance.

### Task 4: Native development paths, licenses, and canonical digest cleanup

**Files:**
- Modify: `scripts/run-development-slices`
- Rewrite: `scripts/qualify-development-model`
- Modify: `scripts/tests/test_run_development_slices.py`
- Modify: `scripts/tests/test_qualify_development_model.py`
- Modify: `tests/scripts/test_qualify_development_model.py`
- Create: `adapters/deepseek/mia-vllm/licenses/MiaAI-Lab-LICENSE`
- Create: `adapters/deepseek/mia-vllm/licenses/vLLM-LICENSE`
- Create: `adapters/deepseek/mia-vllm/licenses/vLLM-NOTICE`
- Modify: `adapters/deepseek/mia-vllm/Dockerfile`
- Modify: `config/runtime-distributions/anemll-vllm-mia.json`
- Modify: `config/runtime-distributions/ds4-spark.json`
- Modify: `config/patch-bundles/mia-deepseek-v4-flash-0731.json`
- Modify: `config/recipes/deepseek-v4-flash-0731-ds4-single.json`
- Modify: `config/recipes/deepseek-v4-flash-0731-mia-dual.json`
- Modify: catalog/recipe fixtures whose exact content digests change

**Interfaces:**
- Consumes: native v1 recipe references and source bundles.
- Produces: development acceptance that no longer dereferences deleted catalogs, final-image license retention, and distribution identity solely through canonical entity content plus pinned OCI fields.

- [x] **Step 1: Write failing executable development/license/digest tests**

Run each development script far enough to load its defaults and assert it resolves native v1 documents, inspect generated source bundles for all three legal files, inspect the final Dockerfile copy/label contract, and require runtime-distribution validation without a free-standing `sha256` field.

- [x] **Step 2: Run tests to verify RED**

Run: `uv run --project control --frozen python -m pytest scripts/tests/test_run_development_slices.py scripts/tests/test_qualify_development_model.py tests/scripts/test_qualify_development_model.py tests/recipes/test_mia_deepseek_v4_flash.py control/tests/test_catalog_entities.py -q`

Expected: FAIL on deleted development paths, absent legal material, and the redundant digest field.

- [x] **Step 3: Implement native defaults and legal retention**

Resolve catalog graphs by exact v1 references, make development model qualification delegate to structural/container recipe qualification, vendor exact upstream legal files and copy them into `/opt/vonk/licenses`, use the SPDX expression `MIT AND Apache-2.0`, and remove the redundant top-level distribution digest from schema/documents/fixtures.

- [x] **Step 4: Rebuild source identities and cascade exact references**

Run: `scripts/recipe-source-bundle adapters/deepseek/ds4 --output-dir .artifacts/recipe-sources/ds4`

Run: `scripts/recipe-source-bundle adapters/deepseek/mia-vllm --output-dir .artifacts/recipe-sources/mia`

Update only source-bundle hashes/sizes and canonical catalog-reference digests; retain all external identities verbatim.

- [x] **Step 5: Run focused tests to verify GREEN**

Run: `uv run --project control --frozen python -m pytest scripts/tests/test_run_development_slices.py scripts/tests/test_qualify_development_model.py tests/scripts/test_qualify_development_model.py tests/recipes/test_mia_deepseek_v4_flash.py control/tests/test_catalog_entities.py -q`

Expected: PASS.

### Task 5: Full verification, report, and commit

**Files:**
- Modify: `.superpowers/sdd/2026-08-15-execution-harness-foundation/task-8-report.md`

**Interfaces:**
- Consumes: RED/GREEN logs and exact final git diff.
- Produces: complete fix-round evidence and one committed fix set.

- [x] **Step 1: Run the complete Task 8 scoped suite and touched broad suites**

Run focused Python, Rust, schema, source-bundle, and script suites; run the two local qualifier commands; record exact counts and environment limitations.

- [x] **Step 2: Self-review**

Verify immutable identities byte-for-byte, no floating tags, no runtime patching/network fetches, license retention, generic qualifier dispatch, canonical evidence, real lifecycle sequencing, and no physical acceptance claim.

- [x] **Step 3: Append the report**

Append fix-round RED commands/output, GREEN commands/results, container limitation, files changed, exact identity preservation, self-review, and concerns.

- [x] **Step 4: Commit**

Run: `git add <fix-round files> && git commit -m "fix: address task 8 review findings"`

Expected: one new commit; no push.
