# Task 8 report: Recreate DS4 and Mia as native v1 recipes

Date: 2026-08-15

Status: DONE_WITH_CONCERNS

Commit: `41619176339d40750dfeabc0cfe25a78abe56805`

## What was implemented

- Replaced the prototype DS4 and Mia adapter trees with deterministic native-v1 image build contexts.
  - DS4 is compiled from canonical `antirez/ds4` source into an immutable CUDA 13.0.1 ARM64 runtime image.
  - Mia starts from the immutable Anemll ARM64 image, vendors the official DeepSeek encoding and all 16 ordered Mia patch actions, verifies every input hash, applies them during image build, verifies the exact post-patch footprint, removes all build patch material, and runs as UID/GID `10001:10001`.
  - Source and tree identities are hard-coded, not Docker build arguments, so callers cannot override authoritative revisions or verification digests.
  - Both final images have empty image entrypoints and no runtime fetch or startup-mutation hook.
- Added native v1 entities for:
  - `deepseek-ai/deepseek-flash` ModelGroup;
  - `deepseek-ai/deepseek-v4-flash-0731` Model;
  - the exact antirez mixed-imatrix DS4 ModelVersion;
  - the exact official 74-file DeepSeek DSpark ModelVersion;
  - the DS4 and vLLM execution harness references;
  - the DS4 Spark and Anemll/Mia runtime distributions;
  - the exact ordered Mia patch bundle;
  - one single-Spark DS4 recipe and one two-Spark distributed Mia recipe.
- Deleted the complete old prototype trees under `config/catalog/development/**` and `config/recipes/development/**`; no compatibility reader, alias, or migration path was added.
- Expanded the strict v1 catalog schema so ModelVersion source, lineage, format, precision, quantization, parameters, limits, aggregate sizes, license, access, artifacts, dependencies, availability, and supersession are first-class validated fields. Artifact inventories now allow up to 256 entries, permitting the exact 74-file official inventory.
- Added strict runtime-distribution source/image/dependency/build/capability fields and strict patch-bundle source/order/tree/compatibility/removal/license/source-bundle fields.
- Added explicit recipe `world_size`, distributed topology mode, and lifecycle readiness/failure semantics.
- Added semantic contract checks for exact model inventory revisions and size totals, matching image-manifest digests, contiguous patch order, and mathematically exact distributed-vLLM capability dimensions bound to the vLLM harness.
- Corrected the DS4 compiler to current upstream CLI flags (`--mtp`, `--ctx`, `--batched-session`, `--dspark`, `--cuda`) and kept DS4 single-node only.
- Kept the generic built-in vLLM entity honestly advertising only `single`; the vLLM compiler accepts distributed mode only when an exact patch bundle and runtime distribution explicitly verify the requested vLLM-MP topology, TP/PP/DP dimensions, roles, rank-loss behavior, `mp` backend, and NCCL/RoCE fabric.
- Compiled Mia into native structured argv for rank 0/1, TP=2, PP=1, `--nnodes 2`, `--node-rank`, worker `--headless`, and placement-provided master address/port. No shell launcher is used.
- Preserved the recipe's verified `host_network: true` in the role-specific runtime spec so the agent can form the actual two-node fabric; unrelated or single-node distributions remain unable to authorize host networking.
- Added deterministic `scripts/recipe-source-bundle` and structural/container-gated `scripts/qualify-recipe` tools.
- Reworked development-fixture/catalog tests to assert that prototype paths are gone and exact native-v1 entities resolve in dependency order.

## TDD RED evidence

The brief's literal root-project command was attempted first:

```text
uv run --frozen python -m pytest tests/recipes/test_deepseek_v4_flash_ds4.py tests/recipes/test_mia_deepseek_v4_flash.py control/tests/test_development_recipe_fixture.py scripts/tests/test_recipe_source_bundle.py scripts/tests/test_qualify_recipe.py -q
```

Collection failed because the repository-root uv environment does not install the control project's `vonk-agent-protocol`/`vonk_control` dependencies (`ModuleNotFoundError: vonk_agent_protocol`). This is an invocation-environment mismatch, not the expected feature RED, so the same scope was run in the owning control project:

```text
uv run --project control --frozen python -m pytest tests/recipes/test_deepseek_v4_flash_ds4.py tests/recipes/test_mia_deepseek_v4_flash.py control/tests/test_development_recipe_fixture.py scripts/tests/test_recipe_source_bundle.py scripts/tests/test_qualify_recipe.py -q
```

Result before implementation:

```text
17 failed
```

The failures were expected: both native recipes/entities and both new scripts were absent, while the development fixture still required the old prototype trees.

Review-driven hardening also captured two focused RED states before their fixes:

```text
2 failed in 0.13s
```

Those assertions proved that DS4 and Mia exact identities were still overridable through Docker `ARG` values.

```text
2 failed in 0.37s
```

Those assertions proved that a wrong distributed fabric was not rejected at the concrete compiler boundary and that the compiled Mia runtime spec emitted `host_network: false` despite the verified distributed recipe.

## GREEN evidence

Complete Task 8 scope after all fixes:

```text
uv run --project control --frozen python -m pytest tests/recipes/test_deepseek_v4_flash_ds4.py tests/recipes/test_mia_deepseek_v4_flash.py control/tests/test_development_recipe_fixture.py scripts/tests/test_recipe_source_bundle.py scripts/tests/test_qualify_recipe.py -q
..................                                                       [100%]
18 passed in 1.21s
```

Broader touched-area regression suite:

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_catalog_contract.py \
  control/tests/test_catalog_entities.py \
  control/tests/test_catalog_seeds.py \
  control/tests/test_builtin_harnesses.py \
  control/tests/test_recipe_runtime_specs.py \
  control/tests/test_development_recipe_fixture.py \
  control/tests/test_development_catalog.py \
  control/tests/test_harness_registry.py \
  control/tests/test_harness_conformance.py \
  control/tests/test_topology.py \
  control/tests/test_cluster_mappings.py \
  control/tests/test_recipe_operations.py \
  control/tests/test_library_projection.py \
  control/tests/test_workload_run_importer.py \
  tests/recipes/test_deepseek_v4_flash_ds4.py \
  tests/recipes/test_mia_deepseek_v4_flash.py \
  scripts/tests/test_recipe_source_bundle.py \
  scripts/tests/test_qualify_recipe.py -q
417 passed in 25.22s
```

Additional verification:

```text
uvx --from ruff==0.16.1 ruff check <all changed Python files>
All checks passed!
```

- `jq empty` passed for both schemas and every new/modified authoritative JSON document.
- `bash -n` passed for every vendored Mia shell patch.
- `git diff --check` passed.
- A diagnostic control-wide run during iteration reached 2,356 passing tests; all Task 8 failures it exposed were fixed. The remaining migration-specific failures require mutable legacy database rows and conflict with the already-completed fresh-schema cutover, so they were not treated as Task 8 compatibility work.

## Deterministic source-bundle evidence

The exact brief commands were run, then repeated into a second temporary output directory. Both manifests and both tar archives compared byte-for-byte equal with `diff` and `cmp`.

```text
scripts/recipe-source-bundle adapters/deepseek/ds4 --output-dir .artifacts/recipe-sources/ds4
{"archive":"228fede9f501c71514aba8ced8058b05e73ad606c47a2ba32ff257d695177de6.tar","archive_bytes":10240,"file_count":1,"source_sha256":"228fede9f501c71514aba8ced8058b05e73ad606c47a2ba32ff257d695177de6","total_bytes":1534}

scripts/recipe-source-bundle adapters/deepseek/mia-vllm --output-dir .artifacts/recipe-sources/mia
{"archive":"f836e0bb5241e877321e607bcf057afee5610a1ad43421f03b0995210b534c7e.tar","archive_bytes":215040,"file_count":21,"source_sha256":"f836e0bb5241e877321e607bcf057afee5610a1ad43421f03b0995210b534c7e","total_bytes":194641}
```

The generated `.artifacts` directory was removed after comparison; it is reproducible and is not an implementation source.

## Container qualification evidence and environment limitation

Both requested commands were run. `scripts/qualify-recipe` first completed structural schema/reference/source-policy validation, then gated container work on native ARM64.

```text
scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --level container
{"detail": "container qualification requires a native linux/arm64 host", "detected_architecture": "x86_64", "passed": false, "required_architecture": "arm64", "status": "environment-limited"}
exit 3

scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --level container
{"detail": "container qualification requires a native linux/arm64 host", "detected_architecture": "x86_64", "passed": false, "required_architecture": "arm64", "status": "environment-limited"}
exit 3
```

Host evidence:

```text
docker_arch=x86_64 server=29.1.3
uname -m -> x86_64
```

Therefore no ARM64 image build, DGX Spark GPU startup, health/invoke/restart, two-rank collective, endpoint-owner readiness, rank-loss withdrawal, or recovery claim is made. Full container qualification requires a native ARM64 DGX Spark for DS4 and two networked DGX Sparks with the exact installed model inventory for Mia.

## Exact upstream, model, and image identities

### Canonical sources

- MiaAI-Lab recipe source:
  - repository: `https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark`
  - revision: `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`
  - codeload archive: 1,938,471 bytes
  - archive SHA-256: `1b5abd647bbead78b1863632424ac332b9e0044e521743813058ac07f3606e04`
  - license: MIT
- Canonical DS4 source:
  - repository: `https://github.com/antirez/ds4`
  - revision: `84cc882352757baf628a1776badf7cc54d584e28`
  - codeload archive: 8,379,876 bytes
  - archive SHA-256: `3ab2c4485bee87f36166b12ab59abbc293ad9fdfadb1c2920d1cbc7f617da165`
  - license: MIT
- Anemll distribution source:
  - repository: `https://github.com/Anemll/dspark-vllm-gx10`
  - revision: `47503f8e38dadd4dededca798150db2619594fce`
  - codeload archive: 172,756 bytes
  - archive SHA-256: `9b3e1de63857220506201c5416df29260691597e3ae7ed7cf18f532b642803ea`
  - license: MIT
- Exact vLLM base source implemented by the Anemll image:
  - repository: `https://github.com/vllm-project/vllm`
  - revision: `752a3a504485790a2e8491cacbb35c137339ad34`
  - version: `0.25.2.dev0+g752a3a504.d20260714`

### DS4 model artifacts

- repository: `https://huggingface.co/antirez/deepseek-v4-gguf`
- revision: `e7f04037032990db0346398d249baf9fb9df1ccc`
- target GGUF:
  - `DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf`
  - 86,720,111,488 bytes
  - SHA-256 `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0`
  - exact quantization: routed expert gate/up `IQ2_XXS`, routed expert down `Q2_K`, attention/shared-expert/output `Q8_0`; catalog quantization `iq2_xxs-q2_k-mixed`
- DSpark support GGUF:
  - `DeepSeek-V4-Flash-DSpark-support-0731.gguf`
  - 5,989,114,272 bytes
  - SHA-256 `7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360`
- exact aggregate: 92,709,225,760 bytes
- license source: official DeepSeek MIT LICENSE at exact official revision `62af8fffb2f7030cac4de2f0169f5b8d1101b646` (the antirez artifact repository has no LICENSE file at its selected revision).

### Official Mia model inventory

- repository: `https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-DSpark`
- revision: `62af8fffb2f7030cac4de2f0169f5b8d1101b646`
- access: public, ungated, no authentication
- license: MIT; LICENSE SHA-256 `f2c6c602815669d292889e5be8c802f2ed950653b77999b1584e8e6aed25d040`
- exact inventory: 74 files, 166,898,666,055 bytes
- weights: 48 safetensors shards, 166,886,535,336 bytes
- independently generated upstream inventory manifest SHA-256: `08a228ec1ba9111d8ebb0dc10405df63859eea1a4d64e685f3075131a147b8a7`
- official encoding: 27,908 bytes, SHA-256 `bdbd57c132a1b3725042323d02b98b9d1df28e5f388f134399555d041f5055e0`
- model facts: 284B total parameters, 13B active, 1,048,576-token context, MoE experts FP4 and remaining checkpoint parameters FP8.

### OCI images

- Anemll Mia base image:
  - `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`
  - platform: Linux ARM64
  - manifest size: 9,530
  - config digest: `sha256:3430d6614a8e2925f34d059af6caf05aff42387326db4d05639a60f10f2654d8`
  - 44 layers, 9,787,494,235 compressed bytes
- DS4 CUDA build image:
  - `nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:5c36750138dc1447a17dafbb397674f167d3b44ce18d9160d769df114577b35d`
- DS4 CUDA runtime image:
  - `nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04@sha256:36050649ad1acc5d3de2c26620191c25850fb12a5771b6c22996033003d952e4`
  - Linux ARM64 manifest size: 2,207
  - config digest: `sha256:55e3a0bc92ce6d14b77cd8f4a81414f2b75c6b3964b17dc452028b55e09ab168`
  - compressed layers: 1,765,183,632 bytes

### Mia patch verification

- 15 exact upstream hotfix files plus one deterministic extraction of Mia's inline reasoning-default patch are recorded in strict application order with SHA-256, purpose, upstream reference, and license.
- Three upstream Python patch files omit a final newline. The source bundle normalizes regular text files, so the build patcher removes exactly one normalized trailing newline from those three files before checking their exact upstream hashes.
- Exact pre-patch footprint SHA-256: `c95903b2148fee727e4e88ffa9b0e65d86239f36bcbec33e352c9a5b92580a03`.
- Exact post-patch footprint SHA-256: `d8995ee56a347544e794269479595768a4aac24b6fac447bf599dc04b73b3325`.
- An independent clean vLLM checkout plus exact Anemll overlay was compared with the patched tree. The difference was exactly the 21 paths in `verify-patched-tree.py`; both pre- and post-digests re-verified successfully.

## Exact catalog and recipe content identities

- ModelGroup: `c28a3ab4a7f3feedfa28d375b139ccf94ab9fad6d16adb62888a06cc27926997`
- Model: `379c5650925fcf84631a262f3112d1c1f1aa603c3885056192cebb25c614f145`
- DS4 ModelVersion: `a54f12dd8653ff220efed3d5b1efa667ab95f060e16211f1cdba7e0a2dcfeafb`
- Official ModelVersion: `d46fb40bf9d117ae39fe68fae3d5a3a7532325a5421637cad68f373a7b5ddd10`
- DS4 harness: `ac139f771cc97b27c1cf6fd97404b6a4db56d6d1725b4282cc5af0289a5421b3`
- vLLM harness: `c0d297318f223378fe573964291bc90fc950242e0d16d1d301c7d3cb4251487d`
- DS4 runtime distribution: `00c2a17548a549333dab8af8b31ec0b4d9af53ee80838f3a83c65faafebc1f22`
- Anemll runtime distribution: `52746e9df3ea563e55941f88f43e275fb2eee76dea0bee8e54e1b8d34474a7b6`
- Mia patch bundle: `23b84b8e50bd381f356715db76460934ade4199237fcd9ee5dac5d246feefb68`
- DS4 recipe: `ad18d1f43180a8422a434d617ff22272964da2a0a49d3c2388ae7edac72d5cde`
- Mia recipe: `c19631a125602e47575af012943d2e8c2db8ed83af67e96ca294da69fde38120`

## Files changed

- Rewritten adapter contexts:
  - `adapters/deepseek/ds4/Dockerfile`
  - `adapters/deepseek/mia-vllm/Dockerfile`
  - `adapters/deepseek/mia-vllm/apply-build-patches.py`
  - `adapters/deepseek/mia-vllm/verify-patched-tree.py`
  - `adapters/deepseek/mia-vllm/vllm-wrapper.py`
  - `adapters/deepseek/mia-vllm/encoding/encoding_dsv4.py`
  - `adapters/deepseek/mia-vllm/patches/**`
  - obsolete compose, wrapper, manifest, validation, and host-side mutation files under both old adapter trees were deleted.
- Native catalog and recipe documents:
  - `config/model-groups/deepseek-flash.json`
  - `config/models/deepseek-v4-flash-0731.json`
  - `config/model-versions/deepseek-v4-flash-0731-ds4.json`
  - `config/model-versions/deepseek-v4-flash-0731-official.json`
  - `config/runtime-distributions/ds4-spark.json`
  - `config/runtime-distributions/anemll-vllm-mia.json`
  - `config/patch-bundles/mia-deepseek-v4-flash-0731.json`
  - `config/recipes/deepseek-v4-flash-0731-ds4-single.json`
  - `config/recipes/deepseek-v4-flash-0731-mia-dual.json`
  - `config/execution-harnesses/ds4.json`
  - all files under `config/catalog/development/**` and `config/recipes/development/**` were deleted.
- Contracts and compilers:
  - `schemas/global/catalog-entity-v1.schema.json`
  - `schemas/global/recipe-v1.schema.json`
  - `control/src/vonk_control/catalog_contract.py`
  - `control/src/vonk_control/recipe_contract.py`
  - `control/src/vonk_control/recipe_runtime_specs.py`
  - `control/src/vonk_control/harnesses/common.py`
  - `control/src/vonk_control/harnesses/ds4.py`
  - `control/src/vonk_control/harnesses/registry.py`
  - `control/src/vonk_control/harnesses/vllm.py`
  - recipe constructors in `harness_conformance.py` and `workload_run_importer.py` were updated for explicit `world_size`.
- Tests and fixtures:
  - catalog/recipe minimal fixtures and all affected topology constructors;
  - `control/tests/test_catalog_contract.py`
  - `control/tests/test_catalog_entities.py`
  - `control/tests/test_builtin_harnesses.py`
  - `control/tests/test_development_catalog.py`
  - `control/tests/test_development_recipe_fixture.py`
  - affected topology/registry/projection/operation tests;
  - `tests/recipes/test_deepseek_v4_flash_ds4.py`
  - `tests/recipes/test_mia_deepseek_v4_flash.py`
  - `scripts/tests/test_recipe_source_bundle.py`
  - `scripts/tests/test_qualify_recipe.py`
- Tools/config:
  - `scripts/recipe-source-bundle`
  - `scripts/qualify-recipe`
  - removed a stale deleted-prototype Ruff exclusion from `pyproject.toml`.

## Self-review findings

- Exact identity: all source revisions are 40-hex commits; every OCI reference is digest-pinned; model artifacts have exact paths, SHA-256 values, byte counts, access, and license facts; all catalog references were recomputed and service-resolved after final edits.
- DS4 correctness: selected the current `ds4f-q2` mixed imatrix default and separate DSpark support file; no NVFP4 claim remains in the DS4 entity or recipe.
- Mia inventory: the strict catalog contains all 74 official files and both aggregate size sums are validated against the inventory.
- Build immutability: review found and removed overridable Docker `ARG` identities. Exact revisions and pre/post digests are now literal build instructions/labels.
- License review: the initially selected antirez LICENSE URL returned 404. It was replaced with the official DeepSeek MIT LICENSE at the exact official model revision.
- Patch/offline behavior: all patches and the encoding are local, hash-checked, applied at build time, post-verified, and removed. Recipe `pre_start` hooks are empty and both model clients are configured offline. DS4's only network action is the allowlisted, hash-verified source download during image build.
- Distributed semantics: generic vLLM remains single-node in its entity. Distributed compilation requires the exact verified distribution and patch; rank commands are structured argv; the worker is headless; placement rendezvous variables become native master CLI flags; host-network authority now survives runtime-spec compilation; rank-loss withdrawal/recovery is explicit in lifecycle metadata.
- Security: final images are numeric non-root; runtime mounts are read-only model plus isolated writable output; capabilities are empty; privileged mode is false; host networking is accepted only for a verified distributed-vLLM capability and connected multi-node topology.
- Review tooling: the required review skill was used, but no reviewer/subagent dispatch capability was exposed in this session. A structured independent diff/source-evidence review was performed locally instead.

## Concerns

1. Full container qualification could not run on this x86_64 host. No ARM64 build, GPU invocation, or physical two-node result is claimed; this is why the task status is `DONE_WITH_CONCERNS`.
2. The brief's literal root `uv run --frozen` pytest invocation cannot import control-only dependencies. The exact same test scope is GREEN with `uv run --project control --frozen`.
3. `scripts/qualify-recipe --level container` deliberately returns environment-limited until it is run on native ARM64 with the required GPU/model/fabric environment. Structural qualification is implemented and tested here; physical acceptance remains to be captured on that hardware.

---

## Fix round 1 — 2026-08-16

Status: DONE_WITH_CONCERNS

This section supersedes the original statement that container qualification
only implemented an architecture gate. The qualifier now has a real executable
container path; this x86_64 host still cannot execute the ARM64/GPU portion.

### What was implemented

All eight findings in `task-8-review.md` were addressed:

1. `scripts/qualify-recipe --level container` is now a generic, contract-driven
   Docker-compatible state machine. It resolves exact v1 references, verifies
   the source bundle and build policy, builds the selected context, starts ranks
   in declared order, checks all-rank/endpoint-owner health, invokes chat,
   performs bounded stop/restart, exercises rank-loss withdrawal and coordinated
   recovery for distributed recipes, cleans up, and emits canonical fail-closed
   evidence. One read-only artifact-root mount is projected to `/models`, so
   multi-artifact recipes do not create overlapping mounts.
2. The runtime-distribution schema now requires a closed distributed launch
   contract with placement-sourced local/master address and master port plus an
   exact rank profile for `NCCL_IB_HCA`, `NCCL_SOCKET_IFNAME`,
   `NCCL_IB_GID_INDEX`, `TP_SOCKET_IFNAME`, and `GLOO_SOCKET_IFNAME`. The catalog
   semantic validator, vLLM compiler, controller runtime-spec compiler, Mia
   wrapper, strict Rust `WorkloadSpec`, and OCI launcher all validate or consume
   that projection. Generic vLLM still advertises only honest single-node
   support; distributed compilation requires a verified distribution capability
   and exact patch bundle.
3. Added an executable distributed lifecycle consumer. The production worker
   observes failed exact ranks, atomically withdraws the real recipe route,
   queues bounded stop phases, starts worker ranks before the endpoint owner,
   and leaves publication pending until all ranks have fresh healthy evidence.
   Missing or malformed recovery authority now commits a withdrawn route and a
   failed run without queuing an unsafe restart. A dependency-free lifecycle
   state machine provides the same behavioral contract to the qualifier.
4. Replaced the deleted-prototype development runners with small native-v1
   entrypoints. `model-single` selects the DS4 recipe and `model-multinode`
   selects the Mia recipe; both delegate to the same structural/container
   qualifier. Obsolete prototype tests were removed and replaced with executable
   native-input and delegation tests. Live development runbooks and their
   contract tests were updated so they no longer advertise deleted catalogs or
   removed runner flags.
5. Retained the exact pinned Mia MIT license and pinned vLLM Apache-2.0 license,
   added a truthful Vonk Forge notice (the pinned vLLM tree has no upstream
   NOTICE file), copied all three into `/opt/vonk/licenses`, and labeled the
   resulting image `MIT AND Apache-2.0`.
6. Added behavioral tests for missing rendezvous/fabric projection, wrapper
   refusal, strict Rust placement binding, route withdrawal, bounded worker-first
   recovery, authority-failure withdrawal, real qualifier command sequencing,
   cleanup, invocation failure, and native development entrypoints.
7. Removed built-in recipe-slug dispatch from the qualifier. A user-authored v1
   recipe with a different slug traverses the same build/health/invoke/recovery
   evidence path while engine-specific checks remain selected by the declared
   harness/distribution capability.
8. Removed the redundant top-level runtime-distribution `sha256` from the strict
   schema, both authoritative distributions, fixtures, and conformance documents.
   Distribution identity is now only the defined canonical entity content digest;
   OCI manifest/config/layer identities remain separately digest-bound.

The review also exposed and fixed a pre-existing cross-language mismatch:
`compile_runtime_spec` now emits the exact strict Rust `WorkloadSpec` shape
(`runtime`, role artifacts, endpoint, security, and lifecycle) rather than
controller-only identity/topology/interface fields. Normal stop payloads now
strip controller-only role metadata before crossing the strict agent protocol.

### TDD RED evidence

The following RED states were captured before their corresponding
implementations. Failures were expected because the reviewed behavior was
absent or rejected by the next strict boundary.

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_distributed_lifecycle.py -q

ERROR collecting ...
ModuleNotFoundError: No module named 'vonk_control.distributed_lifecycle'
```

This established that lifecycle JSON had no executable consumer.

```text
uv run --project control --frozen python -m pytest \
  tests/recipes/test_mia_deepseek_v4_flash.py \
  control/tests/test_recipe_runtime_specs.py \
  control/tests/test_builtin_harnesses.py -q

11 failed, 5 passed
```

The failures covered absent launch/rank profiles, absent fabric projection,
missing placement bindings, permissive wrapper behavior, and the controller/Rust
runtime-spec shape mismatch.

```text
uv run --project control --frozen python -m pytest \
  scripts/tests/test_qualify_recipe.py -q

3 failed, 1 passed
```

The old qualifier stopped after architecture detection and could not execute a
fake ARM64 engine, a user-authored recipe, health/invocation, recovery, or
cleanup.

```text
uv run --project control --frozen python -m pytest \
  scripts/tests/test_native_development_entrypoints.py \
  tests/recipes/test_mia_deepseek_v4_flash.py \
  control/tests/test_catalog_entities.py -q

3 failed
```

The development scripts still loaded deleted prototype paths, legal material
was absent, and runtime distributions still accepted a self-asserted digest.

```text
cargo test -p vonk-agent --test workloads --no-run

error[E0432]: unresolved import `vonk_agent::workloads::PlacementEnvironmentSpec`
```

This proved the strict agent contract had no declared rendezvous producer.

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_recipe_operations.py::test_distributed_rank_loss_queues_bounded_worker_first_recovery -q

ERROR collecting ...
ImportError: cannot import name 'DistributedRecoveryCoordinator'
```

This established that no production recovery coordinator existed.

Documentation was also converted under TDD after live commands were found to
reference the removed CLI:

```text
uv run --project control --frozen python -m pytest tests/test_docs_contract.py -q

5 failed, 29 passed
```

The new assertions rejected old identities, deleted catalog paths, and removed
checkpoint flags.

Two self-review regressions were captured before the final corrections:

```text
uv run --project control --frozen python -m pytest \
  scripts/tests/test_qualify_recipe.py \
  control/tests/test_distributed_lifecycle.py -q

2 failed, 8 passed
```

Those failures caught two overlapping DS4 `/models` mounts and duplicate owner
stop behavior. The coordinated fix mounts the artifact root once and stops every
rank exactly once.

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_recipe_operations.py::test_distributed_rank_loss_withdraws_route_when_recovery_authority_is_missing -q

1 failed
```

The coordinator raised while assembling missing endpoint evidence. The fix now
commits fail-closed route withdrawal and failed state before returning.

### GREEN evidence

Complete focused Task 8/review scope:

```text
uv run --project control --frozen python -m pytest \
  tests/recipes/test_deepseek_v4_flash_ds4.py \
  tests/recipes/test_mia_deepseek_v4_flash.py \
  control/tests/test_development_recipe_fixture.py \
  control/tests/test_distributed_lifecycle.py \
  scripts/tests/test_recipe_source_bundle.py \
  scripts/tests/test_qualify_recipe.py \
  scripts/tests/test_native_development_entrypoints.py \
  tests/test_docs_contract.py -q

71 passed in 3.94s
```

Broad touched Python scope was split into two fresh processes after one combined
process encountered an interpreter-level SQLAlchemy-extension segmentation
fault in an unchanged library-projection fixture. The exact fixture passed alone
(`1 passed in 0.86s`), and every test in the split scope passed:

```text
# Catalog, compiler, conformance, topology, and mapping group
293 passed in 4.15s

# Operations, routes/projection, production worker, exact agent API,
# recipes, qualifier/development scripts, source bundle, and docs group
188 passed in 24.74s
```

Focused normal and fail-closed production recovery paths:

```text
uv run --project control --frozen python -m pytest \
  control/tests/test_recipe_operations.py::test_distributed_rank_loss_queues_bounded_worker_first_recovery \
  control/tests/test_recipe_operations.py::test_distributed_rank_loss_withdraws_route_when_recovery_authority_is_missing -q

2 passed in 1.02s
```

Full Rust agent suite:

```text
cargo test -p vonk-agent --all-targets

129 passed; 0 failed
```

Additional final checks:

```text
uvx --from ruff==0.16.1 ruff check <all changed Python files>
All checks passed!

cargo fmt --all --check
jq empty <both schemas and all changed authoritative JSON>
bash -n adapters/deepseek/mia-vllm/patches/*.sh
git diff --check

All exited 0.
```

### Container qualification evidence and environment limitation

Both real local commands execute strict structural resolution before the native
architecture gate. The available Docker server is x86_64, so both correctly
returned canonical environment-limited evidence and exit 3:

```text
scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --level container
{"detail":"container qualification requires a native linux/arm64 host","detected_architecture":"x86_64","passed":false,"required_architecture":"arm64","status":"environment-limited"}

scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --level container
{"detail":"container qualification requires a native linux/arm64 host","detected_architecture":"x86_64","passed":false,"required_architecture":"arm64","status":"environment-limited"}

ds4_exit=3 mia_exit=3
```

The bounded fake ARM64 engine plus HTTP fixture executes the actual qualifier
state machine, including six distributed rank starts across initial start,
restart, and recovery; collective health; endpoint invocation; rank-loss route
withdrawal; worker-before-owner recovery; recovered invocation; and zero
remaining containers. This is executable behavior evidence, not physical
ARM64/GPU acceptance.

No DS4 GPU run, Mia two-Spark collective, physical RoCE transfer, performance,
or hardware acceptance is claimed. Those remain Task 9.

### Deterministic source and legal evidence

Both source bundles were generated twice into independent temporary directories;
their manifests compared with `diff` and archives with `cmp`:

```text
DS4: sha256 228fede9f501c71514aba8ced8058b05e73ad606c47a2ba32ff257d695177de6
      10,240 archive bytes; 1 file; 1,534 source bytes
Mia: sha256 1db8274206e65ccf2f58b5e744c5b4e7f96c14f916bce2b4a0429630eda6256f
      235,520 archive bytes; 24 files; 208,498 source bytes
```

Retained legal file hashes:

```text
MiaAI-Lab-LICENSE e45e9dafd52503a5dfd6122985f692c27b51afcfe3b695ee8c41f9d18df4439f
vLLM-LICENSE      c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4
vLLM-NOTICE       8afe4ce4927dbf5cbe89e32aaa6d2dba7600aa469970be2a61f275f9a610525a
```

### Exact identities preserved

No external source, model, artifact, or image identity from the accepted Task 8
implementation changed:

- Mia source `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`.
- DS4 source `84cc882352757baf628a1776badf7cc54d584e28`.
- Anemll source `47503f8e38dadd4dededca798150db2619594fce`.
- vLLM source `752a3a504485790a2e8491cacbb35c137339ad34`.
- Antirez model revision `e7f04037032990db0346398d249baf9fb9df1ccc`;
  target GGUF 86,720,111,488 bytes / SHA-256
  `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0`;
  support GGUF 5,989,114,272 bytes / SHA-256
  `7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360`.
- Official model revision `62af8fffb2f7030cac4de2f0169f5b8d1101b646`;
  public/ungated 74-file inventory totaling 166,898,666,055 bytes.
- Anemll linux/arm64 image
  `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`
  with manifest size 9,530.
- DS4 build/runtime CUDA digests
  `5c36750138dc1447a17dafbb397674f167d3b44ce18d9160d769df114577b35d`
  and `36050649ad1acc5d3de2c26620191c25850fb12a5771b6c22996033003d952e4`.

Canonical internal identities changed only where the review changed canonical
content:

- DS4 runtime distribution:
  `25bb4fd479d179169bafbc82662bb8bd66e73b9f9dbd3012da915216601bd26a`.
- Anemll runtime distribution:
  `af3b10b9abe842bfcd0e952225ae8881a5286f858b7cb47c4850bc05608ef6ec`.
- Mia patch bundle:
  `6acbd45a8cb986a90e378d074f19722407315e30d3e3885b2f82342477241076`.
- DS4 recipe:
  `ccaadf135991930a728f99cb5914ba6fecf94c6d5fff990cc8fbea7fde8c9f64`.
- Mia recipe:
  `fb7e6314de7649871f080d9dc0c63dbb1ea827eaf5b47d24291c34f26b49ec35`.

There are no floating tags in authoritative entities, no startup patching, no
runtime package installation, no qualification-time model fetch, no old alias,
and no compatibility reader.

### Files changed in fix round 1

- Runtime/build/legal:
  - `adapters/deepseek/mia-vllm/Dockerfile`
  - `adapters/deepseek/mia-vllm/vllm-wrapper.py`
  - `adapters/deepseek/mia-vllm/licenses/{MiaAI-Lab-LICENSE,vLLM-LICENSE,vLLM-NOTICE}`
- Authoritative contracts/entities:
  - `schemas/global/catalog-entity-v1.schema.json`
  - `schemas/global/recipe-v1.schema.json`
  - `config/runtime-distributions/{anemll-vllm-mia,ds4-spark}.json`
  - `config/patch-bundles/mia-deepseek-v4-flash-0731.json`
  - `config/recipes/{deepseek-v4-flash-0731-ds4-single,deepseek-v4-flash-0731-mia-dual}.json`
- Controller/compiler/runtime:
  - `control/src/vonk_control/{catalog_contract,distributed_lifecycle,distributed_recovery,harness_conformance,recipe_operation_worker,recipe_operations,recipe_runtime_specs,worker}.py`
  - `control/src/vonk_control/harnesses/vllm.py`
  - `rust/crates/vonk-agent/src/{oci,workloads}.rs`
- Development and qualification tools:
  - `scripts/qualify-recipe`
  - `scripts/qualify-development-model`
  - `scripts/run-development-slices`
  - deleted obsolete prototype development-script test modules and added
    `scripts/tests/test_native_development_entrypoints.py`
- Tests:
  - changed catalog, harness, runtime-spec, recipe-operation, agent-API, Rust
    workload, DS4/Mia recipe, qualifier, lifecycle, and docs contract tests
- Live documentation:
  - `docs/audits/development-model-smoke.md`
  - `docs/runbooks/development-agent-workloads.md`
  - `docs/runbooks/fresh-development-install.md`
  - `docs/runbooks/mia-deepseek-v4-flash.md`
  - `docs/superpowers/plans/2026-08-15-task-8-review-fix-round-1.md`

### Self-review findings

- Exact identity and immutability: all accepted external commits, file sizes,
  SHA-256 values, image digests, and model access/license facts remain exact.
  Cascaded internal references were independently recomputed from canonical
  documents and validated by catalog resolution tests.
- Distributed semantics: rank-specific fabric is distribution-owned and
  compiler-validated; placement rendezvous is controller-produced and
  Rust-consumed; the worker is headless; rank 0 is the sole endpoint owner;
  route publication follows all-rank health; recovery stops the whole gang once,
  starts workers first, and starts the owner last.
- Fail-closed behavior: unsupported architecture returns exit 3 without a pass;
  build/runtime/HTTP errors return exit 1; cleanup runs on failure; missing
  recovery authority withdraws rather than leaving a stale route; unbound Rust
  distributed placement is rejected.
- Security/offline: image references are digest-pinned; runtime uses numeric
  non-root identity, read-only root, no added capabilities, no new privileges,
  and one read-only model root; Mia patch inputs are present only at build time;
  no startup or qualification network fetch is present after installation.
- Genericity: qualification behavior is selected from strict v1 references,
  topology, interfaces, validators, lifecycle, and declared harness capability,
  not from DS4/Mia slug conditionals.
- Legal: exact pinned MIT and Apache-2.0 texts survive into the final image and
  the combined SPDX expression is accurate.
- Review tooling: the requesting-code-review workflow was applied, but this
  session exposed no subagent dispatch capability. The authoritative review was
  addressed finding-by-finding and the final diff was manually audited against
  the brief and behavior tests.

### Concerns

1. Physical linux/arm64, GPU, two-DGX-Spark, RoCE collective, and performance
   acceptance could not run on this x86_64 host. No such result is claimed;
   Task 9 retains that work.
2. One combined 481-test Python process encountered a segmentation fault inside
   SQLAlchemy's compiled event extension at an unchanged fixture. The exact test
   immediately passed alone and the same complete scope passed in two isolated
   processes (293 + 188 tests). This appears to be process/runtime instability,
   not an assertion failure, but it is retained here as environment evidence.

---

## Fix round 2 — 2026-08-16

Status: DONE_WITH_CONCERNS

This section supersedes the fix-round-1 description of the simplified
development runner and the DS4 public build path. All seven round-2 findings
were addressed. Physical Spark acceptance remains Task 9 and is not claimed.

### What was implemented

1. The bridge-networked DS4 qualifier now publishes only the endpoint-owner
   port as the bounded mapping `<endpoint-host>:<recipe-port>:<recipe-port>`.
   Its behavioral test inspects the actual generated `docker run` argv,
   including the complete DS4 engine argv and the bounded publication, on both
   initial start and restart.
2. The retained distributed-recovery deadline is now validated before every
   stop and start phase advance, at terminal start completion, and immediately
   before route republication. Expiry creates no next-phase work, fails the job,
   keeps the route withdrawn, queues bounded cleanup when ranks may have
   started, and moves the run to a non-revivable failed/stopping state. Route
   publication marks a recovery as published only after the publication
   succeeds, so ordinary later route renewal does not incorrectly reuse an old
   deadline.
3. The DS4 source archive is now staged in the canonical source bundle and
   verified by exact SHA-256 before extraction. Qualification always executes
   Docker builds with `--network none --pull false`; the agent's rootless Podman
   builder also uses `--pull=never`. Dockerfile HTTPS URLs are checked against
   the exact declared host allowlist, and undeclared URLs fail source policy.
   No build maps `public` to unrestricted Docker networking.
4. Runtime compilation now projects the recipe's exact mount list. Python and
   Rust both require/read-only-map `model -> /models` and create/write-map only
   `outputs -> /outputs`; the invented writable `/state` mount and
   `VONK_STATE_ROOT` environment variable were removed. Agent metadata remains
   outside the workload-writable output tree.
5. The complete synthetic, `model-single`, and `model-multinode` public-API
   development lifecycle was restored, including checkpoint/restart/failure
   phases, secret-safe evidence, and `.outputs.run_id`. Native DS4/Mia phases
   recursively resolve current v1 entities instead of deleted prototype
   catalogs. The synthetic fixture resolves the existing canonical `vllm`
   built-in and a fixture-only digest-pinned Python distribution whose shim
   accepts the exact compiled vLLM argv. Production remains exactly the required
   eight built-in harnesses; an explicit literal-set regression enforces this.
6. `qualify-development-model` now traverses every output parent with
   descriptor-relative `O_DIRECTORY|O_NOFOLLOW`, checks the final path without
   following it, creates the temporary file with `O_EXCL|O_NOFOLLOW`, and uses
   descriptor-relative atomic replacement. Existing output symlinks and
   symlinked parents are refused without modifying their targets.
7. Deleted
   `docs/superpowers/plans/2026-08-15-task-8-review-fix-round-1.md`; the SDD
   review/report ledger is the sole fix-round record.

The controller follow-up caught an initially proposed ninth development HTTP
built-in. A literal eight-harness test was added RED, the unauthorized compiler,
schema enum, and entity were removed, and the synthetic fixture was rebound to
the existing vLLM contract before final verification.

### TDD RED evidence

The primary round-2 test batch was written and run before implementation:

```text
uv run --project control --frozen python -m pytest -q \
  scripts/tests/test_qualify_recipe.py::test_bridge_qualification_publishes_the_bounded_endpoint_and_builds_offline \
  control/tests/test_source_policy.py::test_public_build_refuses_a_url_outside_the_declared_host_allowlist \
  control/tests/test_recipe_runtime_specs.py::test_runtime_spec_is_compiled_from_the_trusted_builtin_projection \
  tests/recipes/test_deepseek_v4_flash_ds4.py::test_ds4_runtime_spec_preserves_exact_declared_mount_authority \
  tests/recipes/test_mia_deepseek_v4_flash.py::test_mia_runtime_spec_preserves_verified_host_fabric_authority \
  scripts/tests/test_native_development_entrypoints.py::test_development_model_qualifier_refuses_a_symlink_output_before_resolution \
  scripts/tests/test_native_development_entrypoints.py::test_development_model_qualifier_refuses_a_symlinked_output_parent \
  control/tests/test_recipe_operations.py::test_distributed_recovery_deadline_is_enforced_before_phase_advance \
  control/tests/test_recipe_operations.py::test_distributed_recovery_deadline_is_rechecked_before_route_publication \
  scripts/tests/test_run_development_slices.py::test_runner_help_exposes_restart_and_failure_checkpoints \
  scripts/tests/test_run_development_slices.py::test_runner_completes_exact_public_lifecycle_without_secret_leaks

11 failed in 3.15s
```

The failures demonstrated the reviewed defects: Docker `default` networking,
no host publication, ignored source host authority, `/state` replacing
`/outputs`, symlinks being followed, expired recovery advancing/publishing, and
the missing restored lifecycle CLI/output.

The strict Rust launch boundary was independently RED:

```text
cargo test -p vonk-agent --test workloads \
  container_arguments_are_typed_and_hardened -- --exact

FAILED: Workload(Invalid("security"))
```

The restored synthetic fixture initially used an unregistered custom harness;
the production compiler test captured that gap:

```text
uv run --project control --frozen python -m pytest -q \
  control/tests/test_development_recipe_fixture.py::test_synthetic_development_recipe_compiles_through_the_native_runtime_path

1 failed: RecipeRuntimeSpecError: unknown execution harness
```

The controller's exact built-in-set regression was also captured RED before
removing the proposed ninth harness:

```text
uv run --project control --frozen python -m pytest -q \
  control/tests/test_builtin_harnesses.py::test_production_builtin_harness_set_remains_exactly_the_required_eight

1 failed: Left contains one more item: 'development-http'
```

One full restored-runner iteration found its hard-coded pre-change fixture
digest, as expected after changing the native fixture identities:

```text
58 passed, 1 failed in 36.01s
```

The expected digest was updated to the independently recomputed canonical
recipe digest, after which the full suite passed.

### GREEN evidence

Final focused round-2 behavior, including the eight-built-in guard:

```text
uv run --project control --frozen python -m pytest -q <15 exact round-2 tests>
...............                                                          [100%]
15 passed in 3.39s
```

Complete restored development lifecycle suite:

```text
uv run --project control --frozen python -m pytest -q \
  scripts/tests/test_run_development_slices.py

59 passed in 35.84s
```

Complete Task 8 scoped Python suite, split into fresh processes to retain the
round-1 SQLAlchemy-extension stability workaround:

```text
# Catalog/schema/compiler/conformance/topology/mapping/source-policy group
317 passed in 3.84s

# Operations/routes/builds/agent API/recipes/qualifiers/docs/runbooks group
358 passed in 57.71s
```

Retained development NAS/runbook tests were also run directly:

```text
uv run --project control --frozen python -m pytest -q \
  tests/runbooks/test_development_nas_installation.py \
  tests/test_docs_contract.py

53 passed in 0.04s
```

Full Rust agent suite after the mount correction:

```text
cargo test -p vonk-agent --all-targets

129 passed; 0 failed
```

Additional final verification:

```text
uvx --from ruff==0.16.1 ruff check <all changed Python files>
All checks passed!

cargo fmt --all --check
git diff --check
jq empty <changed strict JSON/schema documents>

All exited 0.
```

### Container qualification evidence and environment limitation

The executable qualifier path is behaviorally covered with a bounded fake
ARM64 engine and HTTP endpoint. The final focused test verifies real generated
Docker build/run argv, offline build networking, bounded bridge publication,
DS4 engine flags, health/invocation, restart, and cleanup. The distributed
fixture retains collective health, endpoint-owner readiness, invocation,
rank-loss withdrawal, worker-first recovery, recovered invocation, and cleanup.

Both real local qualifications performed strict structural resolution, then
correctly stopped at the physical architecture gate on this x86_64 host:

```text
scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --level container
{"detail":"container qualification requires a native linux/arm64 host","detected_architecture":"x86_64","passed":false,"required_architecture":"arm64","status":"environment-limited"}

scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --level container
{"detail":"container qualification requires a native linux/arm64 host","detected_architecture":"x86_64","passed":false,"required_architecture":"arm64","status":"environment-limited"}

ds4_exit=3 mia_exit=3
```

No physical ARM64 image build, GPU inference, two-Spark collective, RoCE,
performance, or acceptance result is claimed. Those remain Task 9.

### Deterministic source evidence

DS4 and Mia source bundles were each generated twice into independent temporary
directories. Both manifest pairs passed `diff` and both archive pairs passed
`cmp` byte-for-byte:

```text
DS4: aff78f2e9bc43bd426c951f342ab3c162c748cab245edc69512389359aded750
      8,386,560 archive bytes; 2 files; 8,381,301 source bytes
Mia: 1db8274206e65ccf2f58b5e744c5b4e7f96c14f916bce2b4a0429630eda6256f
      235,520 archive bytes; 24 files; 208,498 source bytes
```

The staged DS4 archive is 8,379,876 bytes with SHA-256
`3ab2c4485bee87f36166b12ab59abbc293ad9fdfadb1c2920d1cbc7f617da165`,
matching the accepted canonical source identity.

### Exact identities preserved

All accepted external identities remain unchanged:

- Mia source `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`.
- DS4 source `84cc882352757baf628a1776badf7cc54d584e28`.
- Anemll source `47503f8e38dadd4dededca798150db2619594fce`.
- vLLM source `752a3a504485790a2e8491cacbb35c137339ad34`.
- Antirez model revision `e7f04037032990db0346398d249baf9fb9df1ccc`;
  target 86,720,111,488 bytes / SHA-256
  `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0`;
  support 5,989,114,272 bytes / SHA-256
  `7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360`.
- Official model revision `62af8fffb2f7030cac4de2f0169f5b8d1101b646`;
  public/ungated, 74 files totaling 166,898,666,055 bytes.
- Anemll linux/arm64 image
  `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`,
  manifest size 9,530.
- DS4 CUDA build/runtime digests
  `5c36750138dc1447a17dafbb397674f167d3b44ce18d9160d769df114577b35d`
  and `36050649ad1acc5d3de2c26620191c25850fb12a5771b6c22996033003d952e4`.

Round-2 canonical internal identities changed only where canonical DS4 build
content changed:

- DS4 runtime distribution:
  `337c9d850a70b6a8907e588d4fee1d447f770bc004cb15bbc45283d017dca389`.
- DS4 recipe:
  `373169b0ef24f8d21b0aa40e918e13554bb4d788b4bd426df9f14b64b47d184a`.
- Mia recipe remains
  `fb7e6314de7649871f080d9dc0c63dbb1ea827eaf5b47d24291c34f26b49ec35`.

The test-only synthetic recipe has canonical digest
`90396dc5d736ad8083ddfa23f90b2ecef5c05ea1c3129da5375455ddd684413a`,
source-bundle digest
`61086ce766236b70045c7c45dbc7615a24e4cef96e0cad424de808d5f0861f94`,
and fixture-only runtime-distribution digest
`40a2e2be4069930f3e903afff0fb1efcb23fae75aedf4a57380cedbb3b96c68b`.
It references the unchanged canonical vLLM harness digest
`c0d297318f223378fe573964291bc90fc950242e0d16d1d301c7d3cb4251487d`.

### Files changed in fix round 2

- Build/network/runtime:
  - `adapters/deepseek/ds4/Dockerfile`
  - `adapters/deepseek/ds4/vendor/ds4-84cc882352757baf628a1776badf7cc54d584e28.tar.gz`
  - `config/recipes/deepseek-v4-flash-0731-ds4-single.json`
  - `config/runtime-distributions/ds4-spark.json`
  - `control/src/vonk_control/source_policy.py`
  - `control/src/vonk_control/recipe_runtime_specs.py`
  - `rust/crates/vonk-agent/src/{oci,recipe_builder,workloads}.rs`
- Recovery/publication:
  - `control/src/vonk_control/distributed_recovery.py`
  - `control/src/vonk_control/recipe_operations.py`
  - `control/src/vonk_control/recipe_routes.py`
- Qualification/development:
  - `scripts/qualify-recipe`
  - `scripts/qualify-development-model`
  - `scripts/run-development-slices`
  - `control/tests/fixtures/recipes/dev-http-smoke/{recipe.json,context/**,entities/**}`
  - `docs/runbooks/{development-agent-workloads,fresh-development-install,mia-deepseek-v4-flash}.md`
  - deleted `docs/superpowers/plans/2026-08-15-task-8-review-fix-round-1.md`
- Tests:
  - changed controller source-policy/runtime-spec/operation/built-in/development
    tests, DS4/Mia recipe tests, Rust builder/workload tests, qualifier/native
    entrypoint/docs tests
  - restored `scripts/tests/test_run_development_slices.py`

### Self-review findings

- Exact identity/immutability: an independent final scan found every accepted
  source commit, model revision, artifact size/hash, and OCI digest unchanged.
  The accepted Mia/model authoritative documents are byte-unchanged from fix
  round 1. DS4's new internal references were recomputed and strict-resolved.
- Built-in scope: production remains the literal required eight harnesses;
  there is no development compiler/schema enum/global entity. The synthetic
  test fixture uses canonical vLLM and does not widen production registration.
- Security/offline: DS4's source is local, hash-verified, and built with no
  network; Podman refuses pulls; Docker qualification has no build network;
  undeclared Dockerfile URL hosts fail. No startup patching/fetch was added.
- Mount authority: controller and agent agree on exactly one read-only model
  mount and one isolated writable output mount. No writable state mount or
  agent metadata enters the workload-authorized tree.
- Recovery: deadline checks cover both stop/start phase transitions, final
  restart completion, and the last route-publication boundary. Expired routes
  stay withdrawn and failed, so later health observations cannot republish.
- Development behavior: native entity dependency order, source/build/mapping/
  install/run/route/inference/recovery/stop/uninstall evidence, resume
  checkpoints, and secret rejection are executable tests rather than document
  restatements. `.outputs.run_id` is retained.
- Symlink safety: output traversal and replacement remain descriptor-relative
  and do not resolve/follow attacker-controlled symlinks.
- Review tooling: the requesting-code-review workflow was applied. No subagent
  dispatch tool was available, so the seven-item review, immutable identity
  scan, and complete final diff were audited locally.

### Concerns

1. Physical linux/arm64, GPU, two-DGX-Spark, RoCE, and performance acceptance
   cannot run on this x86_64 host. No physical acceptance is claimed; Task 9
   retains it.

## Fix round 3 — 2026-08-16

Fix round 3 addressed exactly the four residuals in the appended authoritative
review. The previously corrected recipe, harness, model, distribution, patch,
source-bundle, lifecycle, and built-in-harness behavior remains intact.

### What was implemented

1. Recovery phase projection now re-samples the clock only after the recovery
   job row lock has been acquired. Deadline expiry is therefore evaluated with
   lock-held time before any phase or terminal transition. Route publication
   also rechecks the deadline after the external activation call; a publication
   that crosses the deadline is immediately replaced by the withdrawn/empty
   generation, commits the failed/withdrawn run state, and never records
   `recovery_route_published`.
2. The privileged helper now accepts exactly the writable host projection
   `agent-data/runs/<run-id>/outputs -> /outputs`. It derives and verifies the
   run ID from that parent, applies write ACLs only to the output directory,
   retains read-only model and runtime-contract mounts, and rejects `/state`,
   `/scratch`, or every other writable target before invoking Docker.
3. The executable Docker qualifier now emits the valid single argument
   `--pull=false`. Its behavioral engine parses build argv and exits 96 for the
   old split `--pull false` form, a missing no-pull declaration, or duplicate
   declarations, so this is an executable build-engine regression rather than
   a string-only assertion.
4. Recipe build authority now includes the ordered unique exact `FROM` image
   references, matching manifest digests, and a bounded base-image store byte
   envelope in the controller's build identity and signed agent claim. The
   Python and Rust protocol boundaries validate the closed shape and reject
   floating or mismatched references. The agent re-derives `FROM` authority
   from the verified source bundle, requires each immutable archive at
   `base-images/sha256/<manifest-hex>/image.oci.tar`, rejects absent, empty,
   symlinked, escaped, oversized, or substituted input, and loads each archive
   into the same fresh operation-private Podman graphroot used by the build.
   It then inspects exact digest, OS, and architecture before executing the
   still-networkless `--pull=never` build. Controller admission/reservation and
   live agent directory accounting both include the declared base-image store.

The production Python protocol wheel and its dependent lock/SBOM evidence were
regenerated after adding the typed base-image claim fields. The wheel SHA-256 is
`17f8de6fd41b35572343d48d82fb28d329862af4baa5063a9cb96168ef11ef23`;
the regenerated supply manifest SHA-256 is
`b407c1f7dbeb629ec0936c8baa20c0b7469a7a6b869fc04e7d62b5cf61028558`.

### TDD RED evidence

The publication-window and real qualifier regressions were first captured
together:

```text
control/.venv/bin/pytest -q \
  control/tests/test_recipe_operations.py::test_recovery_publication_crossing_deadline_is_immediately_withdrawn \
  scripts/tests/test_qualify_recipe.py::test_bridge_qualification_publishes_the_bounded_endpoint_and_builds_offline

2 failed
- publication did not raise after its external call crossed the deadline
- the behavioral engine exited 96 because build argv contained `--pull false`
```

The helper ABI was independently RED against the required exact output mount:

```text
cargo test -p vonk-agent-helper --test authority \
  accepted_runtime_is_compiled_to_hardened_docker_without_socket_authority -- --exact

FAILED: InvalidOperation (the helper still permitted `/state`, not `/outputs`)
```

The signed build authority was RED at both controller and Rust protocol
boundaries:

```text
cargo test -p vonk-agent-protocol --test recipe_builds \
  build_payload_is_closed_and_declarative -- --exact

FAILED: unknown field `base_images`

control/.venv/bin/pytest -q \
  control/tests/test_recipe_builds.py::test_build_plan_is_typed_sandboxed_and_durable \
  control/tests/test_recipe_builds.py::test_starting_build_atomically_reserves_temporary_disk_and_memory

2 failed: missing `base_images` / `base_image_storage_bytes`
```

The faithful private-OCI-store test proved the prior production builder ignored
an absent declared base:

```text
cargo test -p vonk-agent --test recipe_builder \
  build_fails_closed_when_declared_base_archive_is_absent -- --exact

FAILED: expected an error, received successful build evidence
```

Finally, the cross-language signed claim test exposed that the Python protocol
boundary had no typed slash-bearing base-image reference:

```text
uv run --project agent_protocol --frozen python -m pytest -q \
  agent_protocol/tests/test_contracts.py::test_recipe_build_claim_accepts_only_typed_slash_bearing_fields

1 failed: AgentProtocolError: filesystem path values are not allowed
```

All failures were expected because they directly exercised missing round-3
behavior before implementation.

### GREEN evidence

The final focused controller/recipe/qualifier batch includes the real
PostgreSQL row-lock wait, publication-crosses-deadline withdrawal, source and
signed build authority, exact native base identities, strict eight-built-in
guard, qualifier argv parser, source bundling, and DS4/Mia conformance:

```text
uv run --project control --frozen python -m pytest -q \
  control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py \
  control/tests/test_recipe_builds.py control/tests/test_source_policy.py \
  control/tests/test_development_recipe_fixture.py \
  control/tests/test_builtin_harnesses.py \
  control/tests/test_recipe_runtime_specs.py \
  control/tests/test_distributed_lifecycle.py \
  control/tests/test_development_catalog.py \
  scripts/tests/test_qualify_recipe.py \
  scripts/tests/test_recipe_source_bundle.py \
  scripts/tests/test_native_development_entrypoints.py \
  tests/recipes/test_deepseek_v4_flash_ds4.py \
  tests/recipes/test_mia_deepseek_v4_flash.py

292 passed in 32.64s
```

Complete Rust agent/helper/protocol targets exercised the exact helper ABI and
the faithful private-store sequence `load -> exact inspect -> offline build`,
including absent, substituted, and live-over-budget base failures:

```text
cargo test -p vonk-agent-protocol -p vonk-agent \
  -p vonk-agent-helper --all-targets

155 passed; 0 failed
```

Additional complete retained suites and security evidence:

```text
uv run --project agent_protocol --frozen python -m pytest -q agent_protocol/tests
450 passed in 0.44s

uv run --project control --frozen python -m pytest -q \
  scripts/tests/test_run_development_slices.py
59 passed in 35.75s

uv run --project control --frozen python -m pytest -q \
  tests/runbooks/test_development_nas_installation.py tests/test_docs_contract.py
53 passed in 0.04s

uv run --project control --frozen python -m pytest -q \
  control/tests/security/test_agent_protocol.py tests/scripts/test_verify_supply_chain.py
61 passed in 23.19s
```

Final non-test gates:

```text
uvx --from ruff==0.16.1 ruff check <all changed Python files>
All checks passed!

cargo clippy -p vonk-agent-protocol -p vonk-agent \
  -p vonk-agent-helper --tests -- -D warnings
cargo fmt --all -- --check
git diff --check
jq empty <all changed SBOM JSON documents>

All exited 0.

scripts/verify-supply-chain --json
{"errors":[],"images":7,"manifest_sha256":"b407c1f7dbeb629ec0936c8baa20c0b7469a7a6b869fc04e7d62b5cf61028558","ok":true,...}
```

One verification iteration found only formatter layout in the strengthened
helper test. The formatter's exact layout was applied, after which
`cargo test -p vonk-agent-helper --all-targets` passed all 12 tests and both
`cargo fmt --all -- --check` and `git diff --check` exited 0.

### Container qualification evidence and environment limitation

The qualifier's real executable path performs strict structural resolution,
then correctly stops at the physical architecture gate on this x86_64 host:

```text
scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --level container
{"detail":"container qualification requires a native linux/arm64 host","detected_architecture":"x86_64","passed":false,"required_architecture":"arm64","status":"environment-limited"}

scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --level container
{"detail":"container qualification requires a native linux/arm64 host","detected_architecture":"x86_64","passed":false,"required_architecture":"arm64","status":"environment-limited"}

ds4_exit=3 mia_exit=3
```

No physical ARM64 image import/build, GPU inference, two-Spark collective,
RoCE, performance, or acceptance result is claimed. Physical Spark acceptance
remains Task 9.

### Exact identities preserved

All accepted external identities are unchanged:

- Mia source `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`.
- DS4 source `84cc882352757baf628a1776badf7cc54d584e28`.
- Anemll source `47503f8e38dadd4dededca798150db2619594fce`.
- vLLM source `752a3a504485790a2e8491cacbb35c137339ad34`.
- Antirez model revision `e7f04037032990db0346398d249baf9fb9df1ccc`;
  target 86,720,111,488 bytes / SHA-256
  `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0`;
  support 5,989,114,272 bytes / SHA-256
  `7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360`.
- Official model revision `62af8fffb2f7030cac4de2f0169f5b8d1101b646`;
  public/ungated, 74 files totaling 166,898,666,055 bytes.
- Mia base image
  `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`,
  manifest size 9,530.
- DS4 build base
  `nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:5c36750138dc1447a17dafbb397674f167d3b44ce18d9160d769df114577b35d`.
- DS4 runtime base
  `nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04@sha256:36050649ad1acc5d3de2c26620191c25850fb12a5771b6c22996033003d952e4`.

No `config/` or `adapters/deepseek/` authoritative identity file changed in
round 3. The canonical DS4 runtime-distribution, DS4 recipe, and Mia recipe
digests therefore remain respectively
`337c9d850a70b6a8907e588d4fee1d447f770bc004cb15bbc45283d017dca389`,
`373169b0ef24f8d21b0aa40e918e13554bb4d788b4bd426df9f14b64b47d184a`,
and `fb7e6314de7649871f080d9dc0c63dbb1ea827eaf5b47d24291c34f26b49ec35`.

### Files changed in fix round 3

- Recovery/publication:
  `control/src/vonk_control/{recipe_operations,recipe_routes}.py` and
  `control/tests/test_recipe_operations.py`.
- Base-image authority and admission:
  `control/src/vonk_control/{recipe_builds,source_policy}.py`,
  `control/tests/{test_recipe_builds,test_development_recipe_fixture}.py`,
  `agent_protocol/src/vonk_agent_protocol/contracts.py`, and
  `agent_protocol/tests/test_contracts.py`.
- Agent protocol/store behavior:
  `rust/crates/vonk-agent-protocol/{src/lib.rs,tests/recipe_builds.rs}` and
  `rust/crates/vonk-agent/{src/recipe_builder.rs,src/source_policy.rs,tests/recipe_builder.rs}`.
- Privileged helper ABI:
  `rust/crates/vonk-agent-helper/{src/operations.rs,tests/authority.rs}`.
- Qualifier:
  `scripts/qualify-recipe` and `scripts/tests/test_qualify_recipe.py`.
- Regenerated supply evidence:
  `agent/uv.lock`, `control/uv.lock`,
  `inventory/wheels/vonk_agent_protocol-2.1.0-py3-none-any.whl`, and the changed
  agent-protocol/agent-Python/control-Python SBOMs plus
  `inventory/sbom/manifest.json`.

### Self-review findings

- Lock/deadline semantics: the regression uses a real PostgreSQL `FOR UPDATE`
  blocker and advances the injected clock only while the worker is blocked.
  No next recovery phase is created after release. The separate publisher test
  advances time inside the external publish call and verifies published then
  empty generations, durable failure/withdrawal, and absence of the recovery
  publication marker.
- Helper authority: host source shape and container target are both exact;
  forbidden-target tests keep the valid output source and vary only `/state`
  or `/scratch`. Validation fails before any additional Docker run.
- Build/offline security: controller and agent independently derive the same
  ordered immutable `FROM` set. The signed claim binds the set and storage
  envelope. The agent accepts no ambient graphroot, loads only fixed local
  archive paths into its operation-private graphroot, verifies exact imported
  digest/platform, retains `--network=none --pull=never`, and has executable
  absent/substitution/live-storage failure tests.
- Identity/immutability: no accepted external or canonical recipe entity was
  changed. No floating image tag, compatibility reader, alias, migration, or
  startup/network patch behavior was introduced.
- Built-in scope: the stale round-1 plan remains absent, `development-http`
  remains absent from production harness config/compiler paths, and the exact
  required-eight regression remains GREEN.
- Review tooling: no independent subagent dispatcher was exposed in this
  session. The authoritative four-item review, complete diff, generated binary
  evidence, immutable identities, and security/offline boundaries were audited
  locally; no unresolved critical or important finding remained.

### Concerns

1. This x86_64 environment cannot provide physical linux/arm64, GPU,
   two-DGX-Spark, RoCE, or performance acceptance. No such acceptance is
   claimed; Task 9 retains it.

## Fix round 4 — 2026-08-16

Round 4 started from immutable base commit
`003b841249b7ed257a253d4834a8753602d92a09` and implements only the four
residuals in `# Task 8 re-review — fix round 4`. The catalog remains an exact
clean-schema fence; no compatibility path, data migration, or accepted external
identity changed.

### Implementation

1. Recovery publication is now fail-closed across the external activation
   window. A recovery route generation is issued with an expiry no later than
   the signed recovery deadline, so the routing authority rejects it once that
   deadline passes even if compensating withdrawal fails. After a crossing,
   the run and recovery job are durably failed/withdrawn, the expired
   generation is projected as `withdrawal-pending`, the original deadline error
   survives an arbitrary ordinary cleanup exception, and maintenance retries
   the withdrawal from durable publication state. A successful immediate
   withdrawal records its actual withdrawal generation.
2. The Rust agent now has a complete exact-digest base-image producer/consumer
   path. On a fresh node it resolves only the declared untagged
   `repository@sha256:<digest>` through HTTPS with explicit ORAS auth and a
   DNS-pinned `--resolve`, fetches the exact manifest and each declared
   config/layer blob, validates all digest/size/platform relationships, and
   constructs a deterministic, structurally valid OCI layout archive. Declared
   archive size is checked before blob fetch, each transfer is streamed into a
   bounded pre-opened file, and the verified archive is atomically installed.
   The consumer passes the held verified descriptor to an operation-private
   Podman graphroot through stdin, verifies the imported digest and
   `linux/arm64` platform, and retains `--pull=never --network=none` for the
   build. No mutable tag, ambient image store, or Podman build pull is used.
3. Base-image storage is rooted in held descriptors below the canonical
   `data_root`. `openat2` uses `RESOLVE_BENEATH`, `NO_MAGICLINKS`, and
   `NO_SYMLINKS`; every created/opened authority directory is private and
   owner-controlled. Existing archives must be regular, single-link,
   owner-owned, non-group/world-writable files. Tests reject a symlinked data
   root, supply root, digest directory, archive, digest path escape, blob
   substitution, and path replacement; the consumer continues from the held
   verified inode across replacement.
4. Python now validates the complete closed `recipe.build.v1` claim shape with
   the same exact identities, platform, uniqueness, canonicality, scalar, byte,
   storage, network, and privilege bounds as Rust. Both implementations execute
   `vonk_agent_protocol/vectors/recipe-build-claim-v1.json`, including positive
   no-base/public-network cases and 25 negative mutations. The vector is
   packaged in the protocol wheel, and the wheel, both consuming locks, three
   affected SBOMs, and supply manifest were regenerated.

The controller's Task 9 Step 7 clean-start correction is preserved in
`docs/superpowers/plans/2026-08-15-execution-harness-foundation.md`: the reset
removes users, sessions, and enrollments, after which the administrator is
recreated and both Sparks are re-enrolled.

### RED evidence

The fail-closed route reproduction was added before the production change:

```text
uv run --project control --frozen python -m pytest -q \
  control/tests/test_recipe_operations.py::test_expired_recovery_route_is_unusable_when_compensating_withdrawal_fails

FAILED: RuntimeError: synthetic route withdrawal failure
```

The cleanup exception masked the recovery deadline and caused the surrounding
transaction to roll back. Strengthening the existing crossing regression also
produced two expected audit REDs before the final projection fix:

```text
test_recovery_publication_crossing_deadline_is_immediately_withdrawn
FAILED: route_generation was 1, expected withdrawal generation 2

test_expired_recovery_route_is_unusable_when_compensating_withdrawal_fails
FAILED: recovery job state was "succeeded", expected "failed"
```

The fresh-node OCI and descriptor authority tests failed against the old
consumer-only implementation:

```text
cargo test -p vonk-agent --test recipe_builder \
  fresh_node_produces_verified_exact_digest_oci_archive_before_offline_build -- --exact

FAILED: Io(NotFound)

cargo test -p vonk-agent --test recipe_builder \
  base_image_storage_rejects_symlinked_data_and_supply_roots -- --exact

FAILED: build returned Ok for a symlinked data root
```

The bounded producer audit was added before prefetch accounting and proved that
all blobs were fetched before the signed final-archive bound was enforced:

```text
cargo test -p vonk-agent --test recipe_builder \
  base_image_producer_rejects_declared_archive_above_bound_before_blob_fetch -- --exact

FAILED: observed 3 ORAS calls, expected only the manifest fetch
```

Shared claim vectors were then run against each language before parity was
implemented:

```text
uv run --project agent_protocol --frozen python -m pytest -q \
  agent_protocol/tests/test_contracts.py::test_recipe_build_claim_matches_shared_python_rust_vectors

FAILED: an invalid vector did not raise AgentProtocolError

cargo test -p vonk-agent-protocol --test recipe_builds \
  build_claim_matches_shared_python_rust_vectors -- --exact

FAILED: Rust accepted "noncanonical base reference"
```

These were requirement-focused REDs. No production implementation was written
before its corresponding failing regression.

### GREEN evidence

The final retained controller batch covers route recovery/publication,
build/source authority, the strict built-in catalog, development fixtures,
qualifier/source tools, native entrypoints, distributed lifecycle, and both
DS4/Mia recipe contracts:

```text
uv run --project control --frozen python -m pytest -q \
  control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py \
  control/tests/test_recipe_builds.py control/tests/test_source_policy.py \
  control/tests/test_development_recipe_fixture.py \
  control/tests/test_builtin_harnesses.py \
  control/tests/test_recipe_runtime_specs.py \
  control/tests/test_distributed_lifecycle.py \
  control/tests/test_development_catalog.py \
  scripts/tests/test_qualify_recipe.py \
  scripts/tests/test_recipe_source_bundle.py \
  scripts/tests/test_native_development_entrypoints.py \
  tests/recipes/test_deepseek_v4_flash_ds4.py \
  tests/recipes/test_mia_deepseek_v4_flash.py

293 passed in 32.40s
```

The final all-target Rust run includes the faithful registry/OCI producer,
manifest/config/layer digest validation, exact private-store load, offline
build, all descriptor/symlink/race failures, the shared protocol vectors, and
all retained helper/protocol/agent tests:

```text
cargo test -p vonk-agent-protocol -p vonk-agent \
  -p vonk-agent-helper --all-targets

164 passed; 0 failed
```

Additional retained and supply-chain suites:

```text
uv run --project agent_protocol --frozen python -m pytest -q agent_protocol/tests
451 passed in 0.44s

uv run --project control --frozen python -m pytest -q \
  scripts/tests/test_run_development_slices.py
59 passed in 35.66s

uv run --project control --frozen python -m pytest -q \
  tests/runbooks/test_development_nas_installation.py tests/test_docs_contract.py
53 passed in 0.04s

uv run --project control --frozen python -m pytest -q \
  control/tests/security/test_agent_protocol.py \
  tests/scripts/test_verify_supply_chain.py
61 passed in 22.68s
```

One parallel verification attempt made the Docker wheel-install test contend
with the development Docker suite and failed inside the pinned Python base
image with `ValueError: bad marshal data (invalid reference)`. The isolated
test immediately passed (`1 passed in 13.88s`), and the complete isolated
security/supply suite then produced the 61/61 result above. No source or
generated artifact changed between those runs.

Final formatting, lint, lock, JSON, and supply gates:

```text
uvx --from ruff==0.16.1 ruff check <4 changed Python files>
uvx --from ruff==0.16.1 ruff format --check <4 changed Python files>
cargo clippy -p vonk-agent-protocol -p vonk-agent \
  -p vonk-agent-helper --tests -- -D warnings
cargo fmt --all -- --check
uv lock --project agent --check
uv lock --project control --check
jq empty <shared vector and all changed SBOM JSON documents>
git diff --check

All exited 0; Ruff reported all checks passed and all 4 files formatted.

scripts/verify-supply-chain --json
{"errors":[],"images":7,"manifest_sha256":"1e81fd31110e10b130f23a35bf9ca7dae3a2f823dad39635cfd8e824bab5e9b8","ok":true,...}
```

Generated artifact identities:

- protocol wheel SHA-256:
  `cef491fb57d9df773664607bdb940e1da8de9cb1d5474d544f42216693daeb83`;
  both consuming lockfiles bind this exact wheel;
- shared vector SHA-256:
  `87ecc469559f466bc14aa9856ef906ea062b95ebdefa018f3ae4ce0b819fb043`;
- supply manifest SHA-256:
  `1e81fd31110e10b130f23a35bf9ca7dae3a2f823dad39635cfd8e824bab5e9b8`;
- wheel inspection contains both
  `vonk_agent_protocol/contracts.py` and
  `vonk_agent_protocol/vectors/recipe-build-claim-v1.json`.

### Physical qualification boundary

Both real qualifier commands still stop at the intended native architecture
gate on this host:

```text
scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --level container
scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --level container

{"detail":"container qualification requires a native linux/arm64 host","detected_architecture":"x86_64","passed":false,"required_architecture":"arm64","status":"environment-limited"}
ds4_exit=3; mia_exit=3
```

No physical ARM64 base import/build, GPU inference, two-Spark collective, RoCE,
performance, or physical acceptance result is claimed. Those remain Task 9.

### Files changed in fix round 4

- Route protocol and regression:
  `control/src/vonk_control/recipe_routes.py` and
  `control/tests/test_recipe_operations.py`.
- Exact OCI producer, descriptor authority, and offline consumer:
  `rust/crates/vonk-agent/src/{base_images,lib,process,recipe_builder}.rs` and
  `rust/crates/vonk-agent/tests/recipe_builder.rs`.
- Shared signed-claim parity:
  `agent_protocol/src/vonk_agent_protocol/contracts.py`,
  `agent_protocol/src/vonk_agent_protocol/vectors/recipe-build-claim-v1.json`,
  `agent_protocol/{pyproject.toml,tests/test_contracts.py}`, and
  `rust/crates/vonk-agent-protocol/{src/lib.rs,tests/recipe_builds.rs}`.
- Regenerated supply evidence:
  `agent/uv.lock`, `control/uv.lock`, the protocol wheel, the affected
  agent-protocol/agent-Python/control-Python SBOMs, and
  `inventory/sbom/manifest.json`.
- Preserved controller correction:
  `docs/superpowers/plans/2026-08-15-execution-harness-foundation.md`.

### Final self-review and concerns

- The published recovery lease is the external fail-closed boundary; durable
  retry state does not depend on successful compensating cleanup.
- The OCI path resolves and verifies actual manifest/config/layer content,
  predicts the complete archive bound before blob fetch, atomically installs
  only a verified layout, and loads from the same held inode it verified.
- Python and Rust consume the same packaged vector and agree on every valid and
  invalid case. Closed nested shapes and canonical wire identities are checked
  independently at each language boundary.
- Accepted recipe/source/model/container identities and the required-eight
  production catalog are unchanged. No legacy reader, alias, migration,
  mutable image tag, ambient image store, or build-network escape was added.
- No independent subagent dispatcher was exposed in this session. The full
  diff, generated binary contents, immutable identities, route failure window,
  filesystem descriptors, exact OCI graph, and language parity were audited
  locally; no unresolved critical or important finding remains.

Concern: this x86_64 environment cannot provide the physical linux/arm64, GPU,
two-DGX-Spark, RoCE, or performance evidence reserved for Task 9. No such
acceptance is claimed here.

## Fix round 5 — final breaker round (2026-08-16)

Round 5 started from the required clean base
`c9f848148be1a6cc19ebb83afb25efaed9c2d56f`. It is limited to the three
residuals in `# Task 8 re-review — fix round 5`; the descriptor-relative trust
root from round 4, immutable identities, clean-schema fence, offline build
authority, and the committed Task 9 reset-plan correction are preserved.

### RED evidence

Serving-boundary tests were added before the implementation:

```text
uv run --project control --frozen python -m pytest -q \
  control/tests/test_recipe_operations.py::test_recovery_expiry_inside_real_supervisor_ack_commits_cleanup_retry

FAILED: the real FileSupervisorAcknowledger reported
"live LiteLLM supervisor acknowledgement timed out" instead of making the
lease deadline authoritative, and the recovery publication had no committed
acknowledgement-failure cleanup path.

uv run --project control --frozen python -m pytest -q \
  deploy/compose/tests/test_litellm_supervisor.py::test_expiry_guard_stops_a_real_serving_process_at_the_exact_deadline

FAILED AttributeError: module had no _ServingLeaseGuard; the only expiry
enforcement was the two-second supervisor poll.
```

The first real OCI import exposed the synthetic fixture rather than accepting
it:

```text
cargo test -p vonk-agent --test recipe_builder \
  faithful_oci_layout_imports_into_a_real_private_content_store -- --exact

FAILED: ctr: mismatched image rootfs and manifest layers
```

After making `rootfs.diff_ids` consistent with the actual uncompressed layer,
a real unpack by the unprivileged private daemon reached the host mount
permission boundary. The final executable fixture therefore separates the two
authorities: private containerd performs real OCI archive import and exact
descriptor resolution with `--no-unpack`, while the real Docker loader creates
and exports the equivalent graph to prove the layer is consumable. The
substituted-content RED also demonstrated that `ctr images import` may return
success while exact content resolution fails, so the regression checks
descriptor resolution and bytes, not command status alone.

The shared Unicode vector was then added before changing Python:

```text
uv run --project agent_protocol --frozen python -m pytest -q \
  agent_protocol/tests/test_contracts.py::test_recipe_build_claim_matches_shared_python_rust_vectors

FAILED: DID NOT RAISE for "dockerfile above 512 UTF-8 bytes"

cargo test -p vonk-agent-protocol --test recipe_builds \
  build_claim_matches_shared_python_rust_vectors -- --exact

PASSED (Rust already applied the bound to UTF-8 bytes)
```

### GREEN implementation

- `FileSupervisorAcknowledger` now samples the lease clock before accepting
  any acknowledgement and rejects `now >= expires_at`, including expiry while
  `_publish` is blocked in the real file acknowledgement loop.
- The atomic publisher preserves the exact activated generation when
  supervisor acknowledgement raises. Recovery activation, publication,
  acknowledgement, and post-publication deadline errors all enter one durable
  path: project `withdrawal-pending` when activation occurred, mark the run
  failed/withdrawn, mark the recovery job failed without a publication-success
  marker, attempt compensation, and retain maintenance retry intent even when
  compensation raises. The transaction commits that state before the error is
  returned.
- Both production and embedded-development LiteLLM supervisors arm an
  independent deadline timer for the exact loaded process. At lease expiry the
  timer kills that process and clears its acknowledgement without waiting for
  the two-second reconciliation poll. A real local HTTP server is reachable
  before expiry, is stopped within 500 ms of the deadline, and cannot accept a
  post-deadline request.
- The OCI fixture now has an internally consistent manifest/config/layer graph
  whose config binds the actual uncompressed layer digest. A uniquely rooted
  real local containerd imports the OCI archive into a private content store
  and returns each manifest, config, and layer by exact digest. The real Docker
  archive loader then loads the same config/layer graph, reports the exact
  `RootFS.Layers`, creates the arm64 container, exports its rootfs, and exposes
  the declared layer file. Missing archives and bytes substituted under the
  old layer descriptor fail complete private-store resolution.
- Python now applies the 512-byte dockerfile wire limit to
  `value.encode("utf-8")`, matching Rust. The shared invalid vector contains
  182 Unicode characters but 524 UTF-8 bytes and is rejected by both readers.
- The protocol wheel, both consuming lockfiles, the affected Python SBOMs, and
  the supply manifest were regenerated. No package version, external route
  identity, image identity, or schema/migration identity changed.

Focused GREEN evidence:

```text
uv run --project control --frozen python -m pytest -q \
  control/tests/test_recipe_operations.py::test_recovery_expiry_inside_real_supervisor_ack_commits_cleanup_retry \
  control/tests/test_recipe_operations.py::test_recovery_publication_crossing_deadline_is_immediately_withdrawn \
  control/tests/test_recipe_operations.py::test_expired_recovery_route_is_unusable_when_compensating_withdrawal_fails
3 passed

uv run --project control --frozen python -m pytest -q \
  control/tests/test_dev_runtime_assets.py deploy/compose/tests/test_litellm_supervisor.py
33 passed in 9.21s

cargo test -p vonk-agent --test recipe_builder \
  faithful_oci_layout_imports_into_a_real_private_content_store -- --exact
cargo test -p vonk-agent --test recipe_builder \
  real_private_content_store_rejects_absent_and_substituted_oci_content -- --exact
2 passed; 0 failed

uv run --project agent_protocol --frozen python -m pytest -q \
  agent_protocol/tests/test_contracts.py::test_recipe_build_claim_matches_shared_python_rust_vectors
cargo test -p vonk-agent-protocol --test recipe_builds \
  build_claim_matches_shared_python_rust_vectors -- --exact
2 passed; 0 failed
```

### Complete retained verification

The final controller selection covers recovery publication, atomic route
runtime, source/build policy, development catalog/runtime, the live supervisor,
qualifier/source-bundle scripts, distributed lifecycle, and both recipe
contracts:

```text
uv run --project control --frozen python -m pytest -q \
  control/tests/test_recipe_operations.py control/tests/test_recipe_routes.py \
  control/tests/test_route_runtime.py control/tests/test_recipe_builds.py \
  control/tests/test_source_policy.py \
  control/tests/test_development_recipe_fixture.py \
  control/tests/test_builtin_harnesses.py \
  control/tests/test_recipe_runtime_specs.py \
  control/tests/test_distributed_lifecycle.py \
  control/tests/test_development_catalog.py \
  deploy/compose/tests/test_litellm_supervisor.py \
  scripts/tests/test_qualify_recipe.py \
  scripts/tests/test_recipe_source_bundle.py \
  scripts/tests/test_native_development_entrypoints.py \
  tests/recipes/test_deepseek_v4_flash_ds4.py \
  tests/recipes/test_mia_deepseek_v4_flash.py

328 passed in 35.48s
```

Full Rust and protocol verification, including the real OCI integrations:

```text
cargo test -p vonk-agent-protocol -p vonk-agent \
  -p vonk-agent-helper --all-targets
166 passed; 0 failed

uv run --project agent_protocol --frozen python -m pytest -q agent_protocol/tests
451 passed in 0.47s
```

Additional retained suites:

```text
uv run --project control --frozen python -m pytest -q \
  scripts/tests/test_run_development_slices.py
59 passed in 35.75s

uv run --project control --frozen python -m pytest -q \
  tests/runbooks/test_development_nas_installation.py tests/test_docs_contract.py
53 passed in 0.06s

uv run --project control --frozen python -m pytest -q \
  control/tests/security/test_agent_protocol.py \
  tests/scripts/test_verify_supply_chain.py
61 passed in 23.20s
```

Final static, lock, and generated-evidence gates:

```text
uvx --from ruff==0.16.1 ruff check <all changed Python source/tests>
uvx --from ruff==0.16.1 ruff format --check <changed formatted Python files>
cargo fmt --all -- --check
cargo clippy -p vonk-agent-protocol -p vonk-agent \
  -p vonk-agent-helper --tests -- -D warnings
uv lock --project agent --check
uv lock --project control --check
jq empty <shared vector and all changed SBOM JSON documents>
git diff --check

All exited 0. Ruff reported all checks passed; Clippy emitted no warnings.

scripts/verify-supply-chain --json
{"errors":[],"images":7,"manifest_sha256":"bba10c8eb56fd21acd4731e3e531ab164bc19b395d6cf1cc5e780b23c1915155","ok":true,...}
```

Generated artifact identities:

- protocol wheel SHA-256:
  `97c9deb581aae78be5ee0eeba69b7e729d83790f4075d6e09db560e13139be77`;
  both consuming lockfiles bind this exact local wheel;
- shared vector SHA-256:
  `60490179ed85ce893af45e5d3b0bbfd9ce5c0e83add49dc109d407cc92d4b295`;
- supply manifest SHA-256:
  `bba10c8eb56fd21acd4731e3e531ab164bc19b395d6cf1cc5e780b23c1915155`;
- wheel inspection contains the changed `contracts.py` and shared
  `recipe-build-claim-v1.json` vector.

### Physical qualification boundary and concerns

Both retained real qualifier commands returned the intended environment gate
on this x86_64 host:

```text
scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json --level container
scripts/qualify-recipe --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json --level container

{"detail":"container qualification requires a native linux/arm64 host","detected_architecture":"x86_64","passed":false,"required_architecture":"arm64","status":"environment-limited"}
ds4_exit=3; mia_exit=3
```

No unresolved round-5 correctness concern remains. Physical ARM64 base
import/build, GPU inference, two-Spark collective, RoCE, performance, and
physical acceptance remain explicitly assigned to Task 9 and are not claimed
by Task 8.
