# Launch implementation and evidence

Updated 2026-09-07. The current integration branch is
`codex/launch-run-operation-repair`, based on remote main `cb6edee0`.
[PR614](https://github.com/CarstVaartjes/vonk-forge/pull/614) carries the combined
Controller/Spark contract and trusted-cache corrections. Local checks,
publication, Controller deployment and physical Spark execution remain
separate results.

## Current checkpoint

- Platform work is on the integration branch, not yet published or deployed.
  Earlier CI runs do not cover its subsequent commits.
- Public contracts v1.0.5 are published at recipes commit
  `d55389b5cc144d0483cf89b95770198c6125cb15` (PR76). Model, Recipe, and nested
  capabilities require exact integer schema tags. All 92 Models, 85 Recipes,
  and 109 compiled role projections pass, including the 751-file Model.
- Canonical Model content references now connect cache persistence, all selected
  and companion Model ownership, Run/Switch, deletion, the API, CLI, and web.
  Generated clients use those same fields. Families, versions, and capabilities
  remain data in ModelDefinition. Private cache manifests use strict Pydantic
  structure and the public ModelReference type.
- Spark downloads use one shared object path across artifact sets. Installation
  materializes selected files from that cache, retries clean up their own partial
  files, and repeated selections retain their distinct mounts. Removing an
  installation keeps reusable downloads; API and UI previews describe that
  retention and report installation-copy bytes.
- The old recipe parser and package assets, flat recovery conversion, duplicate
  enrollment DTO, unused artifact-size resolver, and obsolete model mount layout
  are removed. Current Library acceptance uses ordinary published catalog inputs
  and fresh PostgreSQL.
- CLI qualification uses the current Library API. Selected file IDs are exact;
  download totals deduplicate shared object bytes across Models.
- Run/Switch consumes the typed cache manifest and the current preview response
  contract. Connected tests prove companion Model references survive, empty
  support files remain selectable, and shared object bytes count once.
- Concurrent Model and image workers claim only the row they will process;
  PostgreSQL no longer over-locks queued work and starves another worker.
- Verification at `e9a2b5b2`, before the ongoing wire consolidation: 1,943 Controller tests pass (three documented platform
  or opt-in skips); all four Controller-to-Rust install/start/uninstall/cleanup
  bridge tests pass. The 432 Rust tests across 40 executables passed in
  unprivileged ARM64 OrbStack before the final response-builder extraction;
  its focused protocol and connected bridge tests also pass. Web: 216 unit
  tests, production build, and 25 browser journeys pass (one capture-only skip).
  CLI/generated/supply-chain checks previously passed 328 tests plus 45 subtests;
  the final generated/supply-chain rerun passed all 107 tests.
- Distribution now uses the same shared Pydantic model in the Controller's
  actual response and compiled plan. At `40419e9d`, 562 connected Python tests
  and 32 Linux Rust protocol tests pass, including the actual HTTP manifest
  through Rust with safe Unicode, space, and underscore filenames.
- Removed progress-field aliases and the Rust heartbeat response fallback.
  Enrollment/renewal use shared Pydantic requests and issued-certificate output;
  the retired Rust pending-approval response and polling path are removed.
  Focused validation: 98 Controller enrollment/rotation tests and 12 Linux
  pairing tests pass. The combined protocol/wire run passed 539 tests, including
  enrollment and renewal through Rust, and exposed one heartbeat 409 failure
  following progress canonicalization. The host-helper integration fixes that
  mapping handoff; the current full Controller run passes the heartbeat checks.
- Bootstrap no longer selects an older response by setup-schema query. Both
  Controller and Spark setup require the complete current document. Seven
  connected bootstrap/enrollment/renewal checks and 47 unprivileged Linux setup
  tests pass; three focused Controller bootstrap checks pass.
- The current build/import and inventory boundary passes 16 connected checks,
  including artifact jobs and lifecycle execution. Job placement has an explicit
  nullable host port; serving placement may publish a different host port from
  the container endpoint. The full catalog gate uses production placement and
  passes all 109 roles across 85 Recipes and 92 Models, including 44 job Recipes.
- Package-helper authorization and release documents now use a complete
  Pydantic graph, with schemas derived from it rather than separately maintained
  field lists. All 74 focused package/helper and API checks pass.
- Telemetry uses one shared graph in the actual Controller endpoint and Rust
  sender. There is no empty legacy metric default. Its generated schema exposes
  scalar bounds and is checked for drift. All 237 focused Python checks and 19
  actual Rust/API/PostgreSQL bridge checks pass. Historical database migrations
  remain inert history; they are not runtime fallbacks.
- Model identity and safe Unicode/space filenames retain the same structure
  across Python, Rust, and helper materialization, including a nested path at
  the declared 512-character limit. The 122 affected checks and two focused
  Linux helper path tests pass at integration commit `006fd39a`.
- Host-helper grants and receipts use shared typed documents in Python and
  Rust. The actual grant API, Rust verifier/signer, Python signature consumer,
  authority service, and recipe operation checks pass all 101 focused tests.
  The unused generic grant response is removed.
- The composed Rust workspace compiles and its 38 test executables pass after
  the affected fixture/environment reruns (439 tests). Updated fixtures include
  required telemetry metrics and the explicit null port for artifact jobs.
  The Linux process boundary tests use executable temporary directories; the
  builder test accommodates the existing reserve scaling on small filesystems.
- The broader Controller run at `9d295cd8` passed 1,944 tests with three
  expected platform/opt-in skips and exposed three stale fixture/schema
  assumptions. Their 132 affected checks now pass. Current schema head
  `0022_current_telemetry_defaults` leaves historical 0016 unchanged and preserves
  existing row values. It removes the empty new-sample metrics default and uses
  current Controller provenance for new rollups. A populated PostgreSQL test
  verifies row preservation and rejection of omitted metrics.
- Core job envelopes now use shared Pydantic models on the actual claim,
  heartbeat and result routes. Result validation also resolves the stored
  operation kind before accepting successful evidence. The current claim
  request requires protocol and identity fields instead of filling them from
  defaults; 78 focused API checks and eight connected Rust enrollment/claim
  checks pass. Enrollment OpenAPI now exposes its canonical request model.
- The final operation audit found seven retired Python-agent commands that
  the current Rust agent rejects. Their old Controller orchestration is
  superseded by Run/Switch and is being removed together with its definitions
  and consumers. Current queue/fencing, compiled route authority, inventory,
  and signed readiness behavior remain owned by their current implementations.
- Signed run observations now use one current envelope and shared Python/Rust
  structures. Singleton observations carry explicit null rendezvous fields;
  omissions are rejected by both parsers. The complete composed wire suite
  passes 647 checks using newly built Rust probes in unprivileged ARM64
  OrbStack. The focused Python/API/lifecycle composition passes 583 tests
  with seven opt-in skips. Connected readiness checks pass actual start results
  through Controller persistence, grant issuance, helper signing, Rust
  serialization and Controller consumption. They do not execute a model.
- Old orchestration removal remains in progress. Its complete retirement is
  explicitly authorized. Automatic review rejected deleting still-referenced
  modules, so the current route authority and API/worker wiring are being
  made independent first; old modules are not being preserved as a supported
  execution path.
  The bundled protocol wheel and generated API clients must be refreshed after
  those merges, followed by the combined suite. These focused checks do not
  constitute a release-wide pass.
- Image preparation, cache persistence, availability, and launch compilation
  now use one Pydantic receipt. The duplicate class, field-alias property and
  dictionary fallbacks are removed. All declared receipt fields are required,
  with explicit nulls where applicable. The five affected Controller suites
  pass 125 tests with one optional container check skipped. A current full
  Rust workspace run passes all 435 tests across 38 executables in offline,
  non-root, read-only ARM64 OrbStack. It also caught and fixed a duplicate
  standard test annotation on an async Tokio test introduced during composition.
- The unavailable upstream Skopeo pin is replaced with a verified current
  official image. All six packaging checks pass; the worker image builds and
  downloads an OCI archive as UID10001 with a read-only root.
- The development lifecycle canary is blocked before lifecycle execution:
  nested Docker on the fresh OrbStack VM cannot unpack the baseline LiteLLM
  layer containing `/dev/console`. Source-built Controller images and ARM64
  binaries exist, but this is neither signed release nor physical acceptance.
- The user approved deleting all remaining legacy code. Removal of old catalog
  ModelGroup/ModelVersion schema branches, validators, and seed/test documents
  has a validated canonical replacement in an isolated worker, but automatic
  approval review requires explicit confirmation of the exact validator,
  schema, sidecar and supply-chain input removals. That request is pending;
  the blocked removals are paused. Active platform harness/runtime wire
  contracts remain supported. Separate exact approvals remain pending for
  removing repeated full-file hashes at trusted internal handoffs and the
  qualification CLI's duplicate territorial metadata check. Those changes have
  not been performed.
- The coordinated archive checksum field/header rename from OCI layout to OCI
  archive terminology is paused for exact approval. Checksum behavior is
  unchanged in the proposal; mixed Controller/Spark versions would reject the
  renamed message. No partial rename or old-name fallback has been introduced.
- The NAS Controller is unchanged. Backups and a fresh database are prepared;
  device identities have not been copied and no selected Model download has
  started. Deployment follows successful publication and acceptance.

The detailed evidence below records earlier implementation checkpoints; this
current checkpoint takes precedence for release and deployment status.

## Published results

| Component | Verified result |
|---|---|
| Recipes | [PR76](https://github.com/CarstVaartjes/vonk-forge-recipes/pull/76) merged at `d55389b5cc144d0483cf89b95770198c6125cb15`; [v1.0.5 publication](https://github.com/CarstVaartjes/vonk-forge-recipes/actions/runs/34109170382) succeeded with strict schema tags and unchanged 92 Models and 85 Recipes. |
| Recipe checks | PR workflow `34025493486` and publication workflow `34025838414` succeeded. Producer, public contracts, catalog and independent platform validation passed. Local full producer suite: 420 passed, one skipped. Independent validator authority: `26a2dfa804d80a02a39cd42e6deae5f3b0ecc529`. |
| Canonical acceptance fixture | [PR74](https://github.com/CarstVaartjes/vonk-forge-recipes/pull/74) merged at `807957c9bae653f618d98fb27620f69bf736fe37` after workflow `34029444031` passed. It adds a test-only Model/Recipe/package outside the public catalog. Four focused tests, including actual HTTP serving and production source resolution, passed; a real public download verified the declared 51-byte SHA-256. |
| Public website | [PR59](https://github.com/CarstVaartjes/vonk-forge-web/pull/59) merged at `5cb2008c` and deployed in workflow `34032392542`; main CI `34032392537` passed. The live `vonkforge.ai` bundle contains the frontier-AI story and both global-to-local explanations. Both live product screenshots match the reviewed bytes. Fourteen affected unit tests, build, three browser journeys and desktop/mobile review passed before publication. |
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
| P5 cache / Run / profiles | NAS caches, explicit profile scope/Idle, durable preparation/distribution phases and artifact receipts integrated locally. Signed native helper import/start/restart passed at `d2f3e24d`. | Live Controller-to-Spark distribution and physical inference remain unobserved. |
| P6 website | Canonical compact catalog and plain-language explanation deployed. | Recipe refresh propagation; website evidence does not establish Controller behavior. |
| P7 Controller web / API / CLI | Paired lists, Model NAS download, Recipe placement, profiles, artifact jobs and rich Fleet surfaces integrated. Retired routes and generated clients removed. Combined web passes 216 Vitest tests, build, regenerated clients and the Recipe availability Chromium journey. | Combined Controller/CLI run passed 1,940 tests with 14 skips and four failures; all four are fixed and the affected checks pass as described below. Deployed user/agent workflows remain to be observed. |
| P8 upstream refresh | Reviewed first batch published in v1.0.2; DS4, SparkInfer, GLM and LTX follow-up repairs published in v1.0.3. Every Recipe is represented in the exact QA ledger below. | Real pinned engine parsing remains unverified for 68 Recipes; container start and physical inference remain unverified for the corpus. |
| P9 acceptance / cleanup | Fresh catalog/PostgreSQL checks, independent UI review, packaged image transport and signed native helper execution recorded. Retired authoring modules removed. | Combined checks, compatible consumer publication/deployment, then physical Spark observations remain. |
| P10 availability / recovery | [Implementation brief](library-availability-design-2026-09-06.md) implemented and whole-merged under Sol review. Parallel transfers/builds, fair scheduling, provider-aware backoff, durable independent progress, guided errors, Refresh and forced download/rebuild are integrated. | Publication and deployed NAS observations remain. |

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

CI at `6cafc480` passed all repository shards, Rust, generated clients, Admin
web, Compose, and repair/upgrade acceptance. Two Controller image tests failed
because modifying the pinned Skopeo ELF caused an AMD64 startup fault. The
integrated fix `55cceafe` preserves the original binary and invokes its private
loader. Actual ARM64 and AMD64 images passed non-root version, TLS inspect,
archive copy/inspect, and security checks in OrbStack; root independently
rechecked both images as UID10001. Updated supply-chain verification passed.

Native ARM64 proof now passes at root `d2f3e24d`: signed image import, first
helper start, container removal and second start preserve UID10001 and cache
contents while resetting temporary files. The actual helper and production
Docker runner enforce the hardened argv and custody paths. The proof records
the registry index, platform manifest, actual OCI config and archive identities
separately. Docker's local image ID may be the manifest digest on containerd
stores. Archive parsing selects the exact config named by `manifest.json`,
handles classic root/config-blob layouts and seeks past layer payloads.

This fixes the installation-path, exact writable-ACL and legitimate uppercase/
long-filename failures found during review. All 28 helper library tests and
Clippy pass. The canonical corpus path audit remains lexical evidence only;
the native proof uses a tiny GPU-free image. No live Controller-to-Spark
distribution or physical inference is claimed.

The availability backend at `f57908cc` is integrated: immutable requests queue
while builders are unavailable; Model transfers and image preparations proceed
independently; PostgreSQL locks reserve builders before dispatch. Independent
PostgreSQL checks cover claims, restart fencing and the canonical compiler plus
real ModelCache child. Root `3a964a43` additionally preserves completed image
state/bytes while a Model is pending or failed, maps actual ModelCache byte
counters, retains unknown totals and avoids unsupported aggregate ETA claims.
The final UI/CLI branch `75d20485` and terminal integrity repair `ac342b23`
are whole-merged and independently reviewed. Integrity recovery obtains a
fresh canonical repair plan, relinks the new Model child and retains a completed
image. The connected regression proves repair through parent completion with
one image transport call. Terminal access errors offer Check access and resume;
the browser journey covers reload, parallel children and image-only force.
Combined web validation passes 216 tests and build. Generated clients reproduce
without a diff. Twelve additional availability implementation inputs extend
the supply-chain verifier; all 82 verifier tests and final regeneration/verification
pass. Combined Controller/CLI preflight passed 1,940 tests with 14 skips and
four failures. The root Activity fix `bb1793f6` translates only a missing cache
operation into the global provider's `KeyError` contract, allowing profile and
Run operations to be found. Current CLI-follow assertions and the missing
uncertain-response fixture provider were corrected; all 51 affected
CLI/cache/Activity checks pass. The full CLI module also passed all 93 tests.
The fourth failure used an old hardcoded Model digest. The fixture correction
`41fe9501` computes the exact canonical Model hash and checks its Recipe binding;
the full production API file passes with current producer contracts and actual
OrbStack PostgreSQL. Final CI remains the combined publication gate.

## Remaining runtime and deployment checks

The user explicitly approved post-executable engine arguments, bridge access
on up to two selected serving/rendezvous mappings, and only the NVIDIA CDI
selector `nvidia.com/gpu=all`. UID10001, read-only root, dropped capabilities,
no-new-privileges and declared mounts remain enforced; host networking is
excluded. Authorization is not evidence that the implementation works.

The composed check must carry one real image through Controller inspect/copy/
export, immutable storage, local distribution, import and helper start.
Registry index, platform manifest, actual OCI config, local Docker ID, imported reference and
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

## Selected NAS downloads

On 6 September the user narrowed the initial download scope to the current
MiaAI-Lab Qwen3.8 Flash Next, DeepSeek V4 Flash and GLM5.3 Flash recipes, plus
all 3D recipes. At published producer main `807957c9`, this selects 15 Recipes
and 15 Model definitions. Their selected files total 1,044,798,538,422 bytes
after SHA-256 deduplication. Qwen's SGLang and vLLM recipes share files. The
selection includes the current DeepSeek 0731, SparkInfer and Vision variants,
GLM EXL3/DFlash2 and NVFP4 variants, and eight 3D recipes. Downloading these
Model files does not start workloads or build runtime images.

Live preflight confirmed both Sparks online with no loaded workloads, but the
deployed Controller has no ModelCache API yet. The mounted Docker share has
approximately 2.35 TB free; this is not proof of the named cache volume's free
space. Authenticated UGREEN Docker UI access is available for the supported
project redeploy. No Model downloads have started. After publication and
deployment, inspect the official cache inventory/storage endpoint, generate
fresh per-Model download previews, deduplicate by Model content digest, submit
the returned plan digests and retain the
durable operation IDs. Preserve existing secrets, named volumes and valid
cached files; do not substitute direct NAS downloads for the Controller API.
