# Launch implementation and evidence

Updated 2026-09-06. This replaces the earlier status snapshot. Platform
integration is `codex/interface-integration` at
`ee73608b8cc4f4df4e7c01c0fd49a29f61a48aad`. Local checks, publication,
Controller deployment and physical Spark execution remain separate results.
No local result below claims a deployed Controller or NVIDIA workload.

## Published results

| Component | Verified result |
|---|---|
| Recipes | [PR72](https://github.com/CarstVaartjes/vonk-forge-recipes/pull/72) merged at `32b4c094ba0bf6376d419cb06357fe76b160d944`; [v1.0.2](https://github.com/CarstVaartjes/vonk-forge-recipes/releases/tag/v1.0.2) published with 92 Models, 85 Recipes and 85 archives. |
| Recipe checks | PR workflow `34021339495` and publication workflow `34021548983` succeeded. Producer, public contracts, catalog and independent platform validation passed. Local full producer suite: 358 passed, including 307 subtests. |
| Public website | PR58 merged at `7eb783d63c5ea87d2efea8834467ddcda52decfd`; workflow `33985962484` deployed canonical compact lists and documentation to `https://abd3b57b.vonk-forge-web.pages.dev`. |
| Controller / Spark packages | Current platform integration is not yet published or deployed. |

The v1.0.2 annotated tag points to the exact main commit above. GitHub reports
it as unsigned; the publication job name is not signature evidence. The
release includes the Mia Qwen3.8 Flash Next dual-Spark vLLM Recipe and the
first source-refresh batch: six changed/new archives and 79 retained archives.
Audit reports record retained pins and follow-ups. Publication and structural
validation do not establish that every engine accepts its options or runs on
Spark hardware.

## Product and work packet

The [launch work packet](launch-work-packet-2026-09-05.md) remains the full
scope. The interface uses compact interacting Model/Recipe lists, one Run
action with automatic preparation and progress, complete fleet profiles with
explicit Idle Sparks, and observed Fleet state. CLI and web share Controller
actions. NAS model/image caching and Controller distribution retain reusable
artifacts. The recipe repository owns the two authored Pydantic contracts.
This is a fresh launch with one current contract and no legacy catalog path.

| Packet | Integrated or published work | Remaining evidence |
|---|---|---|
| P1 producer | Model/Recipe definitions, author guide, schemas/examples, changed-only catalog and exact archives published in v1.0.2. | Engine verification below; source-refresh follow-ups. |
| P2 independent validator | Published validator reads the canonical contract. v1.0.2 validation passed against platform `0ab88b4f`. | Recheck final combined consumer revision. |
| P3 catalog / database | Canonical persistence, typed lists/details, all-page pagination and ordered multi-Model details integrated. | Fresh OrbStack PostgreSQL imported 92 Models / 85 Recipes and retained 13 unlinked Models; [exact evidence](evidence/fresh-launch-catalog-postgres-acceptance-2026-09-06.md). Recheck final database additions. |
| P4 downloads / secrets | Optional Controller/worker HF token, anonymous public downloads and [documentation](model-cache-huggingface-auth.md) integrated. | Deployed gated-download observation. |
| P5 cache / Run / profiles | NAS caches, explicit profile scope/Idle, durable preparation/distribution phases and artifact receipts integrated locally. | Final Rust/helper OCI path, current-Recipe receipt authorization, real image import/start, reuse and failure/retry. |
| P6 website | Canonical compact catalog and plain-language explanation deployed. | Recipe refresh propagation; website evidence does not establish Controller behavior. |
| P7 Controller web / API / CLI | Paired lists, Model NAS download, Recipe placement, profiles, artifact jobs and rich Fleet surfaces integrated. Retired routes and generated clients removed. | Final UI branch: 195 Vitest tests, 22 Playwright journeys and one optional skip. Root layout repair passed its desktop/mobile journey. Route cleanup: 49 API/client tests and web build passed. CLI records 97 focused and 23 connected parity tests. New retries require matching API/CLI/web coverage. |
| P8 upstream refresh | Reviewed first batch and reports published in v1.0.2. | GLM, LTX and adapter follow-ups are being composed for the next release; all-Recipe engine checks are active. |
| P9 acceptance / cleanup | Fresh catalog/PostgreSQL checks and independent UI review recorded. | Residual retired configuration/modules, actual Controller image/helper checks, combined CI, compatible consumer publication/deployment, then physical Spark observations. |

Root UI repair: `f8ee3122`. Route cleanup: `d6e7e188`, integrated at
`a251a7b5`; generated clients: `56e7d955`. Artifact settings and finite-number
handling: `a33c3a56` plus `56caa0ae`, integrated at `e6e3268e`. Final precise
credential and ordinary engine-argument follow-ups are still being checked.

## Fault tolerance

Unknown ordinary engine options pass unchanged. Credential isolation,
container policy, structural resource bounds and artifact integrity remain
enforced. Actual engine errors stay visible; missing telemetry or changelogs
do not block a run. Transient failures preserve verified progress for retry.

The partial catalog-sync bug is fixed by `4ca70fff`, integrated at `ee73608b`.
Only a same-commit result explicitly marked current is reused. A partial sync
retries missing items without refetching successful immutable imports. Seven
focused tests passed, including fail-once recovery; pinned Ruff 0.16.1 passed.

Bounded model-cache and Run/Switch retries are under implementation. They must
reuse the persisted plan/artifact digest, verified bytes and completed-node
receipts. Authentication and integrity failures must not become retry loops
or false success. Catalog withdrawal needs separate review: a partial remote
snapshot must never remove previously valid entries or immutable revisions.

## Every-Recipe engine verification

The complete v1.0.2 inventory is assigned to four groups: vLLM 34, SGLang/DS4
7, ComfyUI/Diffusers 23, and PyTorch pipeline 21. Total: 85 Recipes.
Each per-Recipe result must bind the exact source and distinguish:

1. Pydantic, Model/file selection and package closure.
2. Actual Controller compilation of every role and default setting.
3. Real wrapper parsing and child argv, including order, repeated options,
   empty values, structured JSON and setting bindings.
4. Actual pinned engine parser acceptance where executable source/runtime is
   available.
5. Container start and physical Spark execution, when performed.

Intercepting a child process proves wrapper argument routing, not engine
acceptance or inference. Missing parser/container/hardware evidence remains
unverified. Proven defects feed the sole producer integration branch and are
rechecked before publication.

## Remaining runtime and deployment checks

The user explicitly approved post-executable engine arguments, bridge access
on up to two selected serving/rendezvous mappings, and only the NVIDIA CDI
selector `nvidia.com/gpu=all`. UID10001, read-only root, dropped capabilities,
no-new-privileges and declared mounts remain enforced; host networking is
excluded. Authorization is not evidence that the implementation works.

The composed check must carry one real image through Controller inspect/copy/
export, immutable storage, local distribution, import and helper start.
Registry index, platform manifest, local config ID, imported reference and
archive SHA/size must each be verified at their own boundary. Test nonempty
ENTRYPOINT, writable UID10001 HOME/temp/cache, retained cache, reset temporary
output, restart and tamper failures.

OrbStack was verified on 2026-09-06: context orbstack, server 29.4.0, OS
OrbStack, architecture aarch64. It is available for container-backed checks.
GPU/NCCL/fabric behavior, sensor accuracy and model quality require physical
Sparks. The [metrics specification](interface-metrics-spec-2026-09-04.md)
remains the coverage requirement; the recorded 25 Rust telemetry tests are
implementation evidence, not physical sensor acceptance.

Issues #593–#598 and #551 remain tracked. Local fixtures do not close deployed
cache, progress, provenance, preflight, recovery and sanitized-failure
journeys. After combined checks, publish/deploy compatible consumers through
the authorized Controller-managed Spark path, preserving secrets and volumes.
