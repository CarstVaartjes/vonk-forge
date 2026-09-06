# Launch implementation and evidence

Updated 2026-09-06. This replaces the earlier status snapshot. Platform
integration is `codex/interface-integration`
(latest checked remote main `0ab88b4f` is included). Local checks, publication,
Controller deployment and physical Spark execution remain separate results.
No local result below claims a deployed Controller or NVIDIA workload.

## Published results

| Component | Verified result |
|---|---|
| Recipes | [PR73](https://github.com/CarstVaartjes/vonk-forge-recipes/pull/73) merged at `48b00c1f5f1bbd46ea7141d491b63f2697271923`; [v1.0.3](https://github.com/CarstVaartjes/vonk-forge-recipes/releases/tag/v1.0.3) published with 92 Models, 85 Recipes and 85 archives. Thirteen packages changed; 72 retain their bytes. |
| Recipe checks | PR workflow `34025493486` and publication workflow `34025838414` succeeded. Producer, public contracts, catalog and independent platform validation passed. Local full producer suite: 420 passed, one skipped. Independent validator authority: `26a2dfa804d80a02a39cd42e6deae5f3b0ecc529`. |
| Canonical acceptance fixture | [PR74](https://github.com/CarstVaartjes/vonk-forge-recipes/pull/74) merged at `807957c9bae653f618d98fb27620f69bf736fe37` after workflow `34029444031` passed. It adds a test-only Model/Recipe/package outside the public catalog. Four focused tests, including actual HTTP serving and production source resolution, passed; a real public download verified the declared 51-byte SHA-256. |
| Public website | PR58 remains deployed. [PR59](https://github.com/CarstVaartjes/vonk-forge-web/pull/59) at `fd19368a` adds the frontier-AI story, Model/Recipe availability explanation and current product screens. Fourteen affected unit tests, build, three browser journeys and desktop/mobile review passed; publication is pending CI. |
| Controller / Spark packages | Current platform integration is not yet published or deployed. |

The v1.0.3 annotated tag binds the exact main commit above. GitHub reports
it as unsigned; the publication job name is not signature evidence. The
release fixes two DS4 profiles, three SparkInfer profiles, four GLM profiles
and four LTX profiles. The earlier v1.0.2 release includes the Mia Qwen3.8
Flash Next dual-Spark vLLM Recipe and the first upstream refresh batch.
Structural validation does not establish physical Spark inference.

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
| P1 producer | Model/Recipe definitions, author guide, schemas/examples, changed-only catalog and exact archives published; v1.0.3 contains the 13 follow-up package repairs. | Engine verification below; physical serving remains separate. |
| P2 independent validator | Published validator `26a2dfa8` reads the canonical contract; v1.0.3 passed its independent secret and package-identity checks. | Recheck final combined consumer revision. |
| P3 catalog / database | Canonical persistence, typed lists/details, all-page pagination and ordered multi-Model details integrated. | Fresh OrbStack PostgreSQL imported 92 Models / 85 Recipes and retained 13 unlinked Models; [exact evidence](evidence/fresh-launch-catalog-postgres-acceptance-2026-09-06.md). Recheck final database additions. |
| P4 downloads / secrets | Optional Controller/worker HF token, anonymous public downloads and [documentation](model-cache-huggingface-auth.md) integrated. | Deployed gated-download observation. |
| P5 cache / Run / profiles | NAS caches, explicit profile scope/Idle, durable preparation/distribution phases and artifact receipts integrated locally. | Runtime/helper OCI path, current-Recipe receipt authorization and bounded retry are integrated. Actual composed helper-process execution and combined acceptance remain pending; direct OrbStack Docker import/start is not that proof. |
| P6 website | Canonical compact catalog and plain-language explanation deployed. | Recipe refresh propagation; website evidence does not establish Controller behavior. |
| P7 Controller web / API / CLI | Paired lists, Model NAS download, Recipe placement, profiles, artifact jobs and rich Fleet surfaces integrated. Retired routes and generated clients removed. Retry UI passed 199 Vitest tests and 18 Library browser journeys; creator attribution passed seven focused tests and its browser journey. | CI at `8ced8a0a` passed Admin web behavior and generated clients. CLI records 97 focused and 23 connected parity tests. Deployed user/agent workflows remain to be observed. |
| P8 upstream refresh | Reviewed first batch published in v1.0.2; DS4, SparkInfer, GLM and LTX follow-up repairs published in v1.0.3. Every Recipe is represented in the exact QA ledger below. | Real pinned engine parsing remains unverified for 68 Recipes; container start and physical inference remain unverified for the corpus. |
| P9 acceptance / cleanup | Fresh catalog/PostgreSQL checks and independent UI review recorded. | Retired authoring modules removed. Controller packaging and signed helper policy integrated. Combined suite fixes, actual composed helper execution, compatible consumer publication/deployment, then physical Spark observations remain. |
| P10 availability / recovery | [Implementation brief](library-availability-design-2026-09-06.md) committed. Model transfer, image preparation, web/CLI and public explanation have separate owners under Sol. | Parallel nonblocking transfers/builds, fair scheduling, provider-aware backoff, durable progress, guided errors, Refresh and forced download/rebuild are in development. They are not yet integrated or deployed. |

The platform now consumes the exact PR74 main revision in its CI fixture
receipt and both public-contract dependency receipts. The contract source tree
and rebuilt wheel are byte-identical to the v1.0.3 source. Independent platform
validation at `73bcf75f` passed all 85 public Recipes, 92 Models, package identity
and secret checks. The replacement canonical lifecycle harness is integrated
at `e9994f50`; its 23 focused behavior tests passed. This is not a completed
composed lifecycle or physical inference result.

The approved supply-chain verifier update `c05fbf7` and its whole cleanup branch
are integrated. All 39 previously covered active inputs remain; only the two
deleted runners were removed, and 37 current inputs plus contract-wheel
integrity verification were added. Final generated evidence must be refreshed
after the remaining implementation merges. The pure serving evaluator now lives
in the shared `cluster_profiles` package, with its verifier coverage retained.
The subsequently discovered obsolete local importer and model-target ledger
are retired; the managed canonical catalog remains the only consumer path.

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

Bounded model-cache and Run/Switch retries are integrated from `49fe4c67`, including API/CLI routes and generated clients. They reuse the persisted plan/artifact digest, verified bytes and completed-node receipts. Authentication, integrity, permissions and exhausted storage are terminal. UI recovery now uses the persisted retry endpoints, adopts returned operations and polls their progress. Terminal failures do not offer an invalid retry. The reviewed UI follow-up passed 199 Vitest tests, 18 Library browser journeys and its build. Catalog withdrawal needs separate review: a partial remote
snapshot must never remove previously valid entries or immutable revisions.

## Every-Recipe engine verification

The complete 85-Recipe inventory was assigned to four groups: vLLM 34, SGLang/DS4
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

Historical CI/publication/installer changes are present through current integration; old skip-based CI patches should not be restored. The dirty original UI is superseded by the canonical compact paired lists. One uncommitted current-contract UI test correction was recovered in the current UI retry branch and merged. The independently fetched public web `7eb783d` tree is identical to historical launch `47f856fe`; it is already published under different commits. Recipe GLM/LTX/SparkInfer/Qwen/DS4 changes are carried by the sole v1.0.3 candidate. The recovered package-to-Controller-to-Library/API/CLI acceptance test is merged. Exact published-package, producer-freshness and credential-isolation checks pass; its connected Controller case remains environment-dependent. Historical serving execution and durable evidence were genuinely absent. Canonical serving execution and bounded response/evidence handling are now merged; the final assertion review is in progress. The old standalone authored contract was not restored.

Combined checks: the latest broad Controller/protocol run at `b05d13e0`
passed 2,378 tests, skipped 128 and found two failures; it excluded only the
obsolete MIA corpus file under active cleanup. OrbStack was stopped during
that run, so container-dependent skips are not acceptance. Both failures
were corrected: the Docker packaging lock now agrees with the latest recipe
source, and a mutation-ordering fixture advances its clock instead of relying
on random UUID order at an identical timestamp. The two complete focused
files then passed 57 tests with nine environment-dependent skips. The public
contract wheel rebuilt from `48b00c1f` is byte-identical to the recorded wheel
(`694a60b6…`); both lockfiles now bind that latest source.

Earlier integrated checks include 169 receipt/compiler/API/direct tests, 64
development-slice tests, 37 installer/Tailscale tests, actual PostgreSQL
contract checks and a real Caddy socket check. Those results retain their
original source/environment boundaries. The disk-exhausted second broad run
is invalid as a release gate; no SQLite/ENOSPC errors were concealed as passes.

The exact v1.0.3 [per-Recipe QA ledger](evidence/recipe-engine-qa-2026-09-06.json)
covers all 85 canonical publisher/slug identities. All 85 pass contract,
Model/package closure and every-role compilation; 83 pass wrapper routing
and two DS4 recipes invoke their engine directly. Actual engine parser
acceptance is proven for 17, with 68 unverified. No corpus-wide container
startup or physical Spark inference is claimed.

Canonical recipe variants remain independent for the same Model, creator or
Spark count. The sync lookup now uses publisher/slug and bounded query chunks,
including a 257-identity regression. Creator attribution is visible in compact
Recipe rows/details; a same-Model fixture preserves all four Recipe choices.

The duplicate DS4/MIA definitions, adapters, development entrypoints and local
model-target importer have been removed. Tests now consume the canonical
producer examples and production compiler directly. The full Controller run
reported 1,773 passed, 14 skipped and two missing required-input bindings in
the replacement test fixtures. Those bindings were corrected, and all 77 tests
in the affected modules then passed. The focused repository suite passed 120
tests; its two skipped validator cases were made portable and independently
passed as part of seven validator tests against all 85 Recipes and 92 Models.

Native ARM64 CI at `ad24d720` proved signed image import through the real helper.
Start correctly rejected the probe's undeclared executable; `47a02738` repairs
the test image and uses the declared runtime path without changing validation.
Its next native run is pending. Both repair and upgrade acceptance passed at
`abc1c240`. These are not live Controller-to-Spark distribution observations.

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
