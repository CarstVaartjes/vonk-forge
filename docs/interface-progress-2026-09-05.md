# Launch implementation and evidence

Updated 2026-09-06. This replaces the earlier status snapshot. Platform
integration is `codex/interface-integration` at
`cc0ab30c` (latest remote main `0ab88b4f` is included). Local checks, publication,
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
| P5 cache / Run / profiles | NAS caches, explicit profile scope/Idle, durable preparation/distribution phases and artifact receipts integrated locally. | Runtime/helper OCI path, current-Recipe receipt authorization and bounded retry are integrated. Actual composed helper-process execution and combined acceptance remain pending; direct OrbStack Docker import/start is not that proof. |
| P6 website | Canonical compact catalog and plain-language explanation deployed. | Recipe refresh propagation; website evidence does not establish Controller behavior. |
| P7 Controller web / API / CLI | Paired lists, Model NAS download, Recipe placement, profiles, artifact jobs and rich Fleet surfaces integrated. Retired routes and generated clients removed. | Final UI branch: 195 Vitest tests, 22 Playwright journeys and one optional skip. Root layout repair passed its desktop/mobile journey. Route cleanup: 49 API/client tests and web build passed. CLI records 97 focused and 23 connected parity tests. New retries require matching API/CLI/web coverage. |
| P8 upstream refresh | Reviewed first batch and reports published in v1.0.2. | GLM, LTX and adapter follow-ups are being composed for the next release; all-Recipe engine checks are active. |
| P9 acceptance / cleanup | Fresh catalog/PostgreSQL checks and independent UI review recorded. | Retired authoring modules removed. Controller packaging and signed helper policy integrated. Combined suite fixes, actual composed helper execution, compatible consumer publication/deployment, then physical Spark observations remain. |

Root UI repair: `f8ee3122`. Route cleanup: `d6e7e188`, integrated at
`a251a7b5`; generated clients: `56e7d955`. Artifact settings and finite-number
handling: `a33c3a56` plus `56caa0ae`, integrated at `e6e3268e`. Precise credential and ordinary engine-argument follow-ups are integrated at `ca326291`. The combined source lint is clean under Ruff 0.16.1.

## Fault tolerance

Unknown ordinary engine options pass unchanged. Credential isolation,
container policy, structural resource bounds and artifact integrity remain
enforced. Actual engine errors stay visible; missing telemetry or changelogs
do not block a run. Transient failures preserve verified progress for retry.

The partial catalog-sync bug is fixed by `4ca70fff`, integrated at `ee73608b`.
Only a same-commit result explicitly marked current is reused. A partial sync
retries missing items without refetching successful immutable imports. Seven
focused tests passed, including fail-once recovery; pinned Ruff 0.16.1 passed.

Bounded model-cache and Run/Switch retries are integrated from `49fe4c67`, including API/CLI routes and generated clients. They reuse the persisted plan/artifact digest, verified bytes and completed-node receipts. Authentication, integrity, permissions and exhausted storage are terminal. UI recovery is being wired to the new persisted retry endpoints; preview/apply is not a replacement for retry. Catalog withdrawal needs separate review: a partial remote
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

## Worktree and integration audit

The 6 September audit inspected 256 discovered paths, including 200 readable Git checkouts. Older intermediate branches are compared by patches and current behavior, not ancestry alone. No checkout or uncommitted source was deleted or reset. The original `/opt/vonk-forge` working tree is preserved.

Completed branches merged during the audit:

- Immutable image receipt/current Recipe authorization: `115244ed`, merged at `ce368184`.
- Spark compiled runtime and approved bridge/CDI projection: `fb2c081c`, merged at `61e07dcc`.
- Controller Skopeo and exact-source protocol packaging: `386152fa`, merged at `e8179f51`.
- Durable retries: `49fe4c67`, merged at `1950cc56`.
- SGLang model-root wrapper fix: `b635712f`, merged at `b80ffd14`.
- Retired local authoring cleanup: `af0b1a96`, merged at `9d4f45ce`.
- Current generated retry clients and verified supply-chain manifest: `cc0ab30c`.

Historical CI/publication/installer changes are present through current integration; old skip-based CI patches should not be restored. The dirty original UI is superseded by the canonical compact paired lists. One uncommitted current-contract UI test correction was found in an old review checkout and assigned to the current UI owner. The independently fetched public web `7eb783d` tree is identical to historical launch `47f856fe`; it is already published under different commits. Recipe GLM/LTX/SparkInfer/Qwen/DS4 changes are carried by the sole v1.0.3 candidate. An older connected package-to-Controller-to-Library/API/CLI acceptance test was also found absent and is being rebuilt against the current contract. The standalone historical serving-evidence module is under semantic review against the producer-owned qualification path; its filename alone does not prove missing behavior.

Combined checks at this snapshot: 169 receipt/compiler/API/direct tests passed; 92 focused tests passed with 13 skips; 64 development-slice tests passed with their real loopback fixture; 3 CLI parity tests passed. The source/wheel supply-chain verifier passed. The broader Controller/protocol run reached 1,205 passes and two skips, then stopped at 12 failures. Packaging, schema-1 development fixtures, stale compiler/admission fixtures, migration/progress expectations and portable privilege-drop checks have explicit owners. The approved obsolete installer jurisdiction input and documentation cleanup passed the bundle checks; the short-path installer/Tailscale rerun passed 37 tests. The initial Caddy published-socket refusal cleared on an isolated run; its test now waits a bounded interval for that host socket while preserving immediate TLS/authentication failures. This is not a green release gate yet.

Published v1.0.2 QA covers exactly 85 Recipes, with no missing or duplicate rows. All 85 pass public contract, Model/package closure and every-role compilation. Current wrapper evidence is 82 pass, one GLM failure, and two direct-engine rows unverified. Actual parser evidence is 15 pass, two DS4 failures, and 68 unverified. Container startup and physical Spark execution remain unverified for the corpus. The v1.0.3 candidate fixes the GLM mismatch, both DS4 options, three SparkInfer pass-through defects, and the LTX compiled-wire fixture; candidate wrapper and DS4 compiler-to-parser checks passed. Publication is pending final generation and validation.

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
