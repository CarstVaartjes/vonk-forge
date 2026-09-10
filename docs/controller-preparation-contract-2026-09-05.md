# Controller-owned rollout preparation

This document records the Controller/NAS preparation contract. The current
profile lifecycle is cache-first: the Controller/NAS cache is the trusted
preparation authority, and Spark-local copies are derived execution state.

## Ownership and workflow

The Controller resolves the exact model version, recipe revision, artifact set,
compatible Linux/ARM64 runtime image and target placement. NAS-backed storage
holds verified immutable model files and OCI content separately. The Controller
orchestrates published-image retrieval or a compatible build worker; Controller
ownership does not mean compiling CUDA workloads inside the API process or
requiring NAS CPU compatibility.

Preparation makes the exact model and runtime-image assets available in the
Controller/NAS cache. A profile may select only assets that are fully cached
and verified. If a selected profile is missing an asset, apply is blocked with
the missing asset and a concrete reason, and offers cache preparation. Cache
preparation is durable and reports per-object progress, bytes when known, and
actionable failures; it does not silently change the profile's identities.

Once the cache is ready, the Controller distributes the exact verified model
and image assets to all selected Sparks in parallel. Each Spark verifies
destination bytes and image identity, imports or reuses the image, and then the
Controller stops and replaces conflicting workloads with the pinned recipe.
Already verified local copies are skipped. Normal rollout does not compile
source or download from internet origins on Sparks. Spark copies never become
the preparation authority or a fallback source for another profile.

## States and review

Keep these facts separate: available upstream; preparing in the Controller/NAS
cache; ready in the Controller/NAS cache; distributing; ready on selected
Sparks; starting; running. Cache readiness is the gate for apply, while target
readiness is reported after exact assets are distributed and verified. Running
requires actual readiness and endpoint evidence. Historical hardware
qualification remains separate from these operational states.

Library shows model-cache and runtime-image status independently. Profile
choices are limited to exact cached assets. Apply shows the missing cache
reasons when blocked and offers cache preparation; after preparation, one apply
distributes exact assets in parallel, skips verified Spark copies, stops and
replaces workloads, and reports per-Spark progress and readiness.

## Distribution contract

Controller serves only verified immutable objects to authorized enrolled agents for approved operations. Grants bind object digest, size, target identity and operation; credentials are not exposed in UI, logs or copied commands. Support bounded streaming and safe resume/ranges with identity validation and final destination digest verification. No arbitrary filesystem path or caller-selected upstream URL. Models remain outside container layers. OCI manifests/layers or a verified archive retain canonical image identity through import. Reuse existing verified image delivery where possible rather than adding a competing transfer stack.

Network failure leaves a resumable checkpoint. Corrupt/incomplete files are not
promoted to ready. NAS garbage collection may remove local model objects that
are no longer referenced by a saved profile, active workload, or preparation
operation; it must not remove referenced cache objects. Spark-local cleanup
cannot change or replenish the NAS authority, and a Spark copy does not pin an
otherwise unused NAS object. Controller preparation and transfers are bounded
durable jobs, not long blocking API handlers. CLI exposes the same cache
preparation, apply, progress, retry and switching results with stable JSON and
request-key/digest semantics.

## Acceptance

1. Prepare an exact model+recipe once in the Controller/NAS cache, then
   distribute the verified assets to two Sparks in parallel; no Spark internet
   fetch.
2. Repeat preparation and apply: no redundant cache download, image build,
   target copy or import when verified identities still match; verified local
   copies are skipped.
3. A profile with a missing cache asset cannot be applied. The result names
   every blocker and offers cache preparation without changing the profile.
4. Profile A to B to A reuses still-referenced cached objects; NAS garbage
   collection may remove only unreferenced local models. Explicit idle targets
   remain idle.
5. Interrupted distribution resumes safely; modified bytes, mismatched digest,
   unauthorized agent and stale plan cannot become ready or run.
6. Web and CLI report identical identities, cache/distribution phases,
   per-Spark progress and readiness. Stop/start success is not inferred from
   enqueue success.

Sol owns cross-worker integration and contract ledger. This authorizes repository implementation and tests, not live NAS/Spark deployment, external image publication or hardware acceptance claims.

## Historical design review and deferred recipe work — 2026-09-05

The following section is retained as dated decision history. It is superseded
for active guidance by the cache-first contract above and the current workflow
below; its proposals are not an alternate implementation path.

User requested design refinement and discussion before updating recipes. Do not modify recipe definitions/catalogs or introduce a new recipe schema as a side effect of this platform work. Requirements below are design decisions/proposals and an implementation gap ledger, not claims that current recipes enforce them.

### Verified gap ledger

Existing contracts bind model-version, harness and runtime-distribution references; runtime distribution pins ARM64 and an image digest; model-version inventory describes exact per-file hashes/sizes; build/import evidence binds output image identity. Existing run admission also checks installation, mapping/capacity/topology and readiness evidence.

Remaining gaps identified in review: recipe-local artifact inventory is not proven equal to the exact model-version file inventory; reported artifact-set identity is not consistently compared to the expected set; generic build does not establish ahead-of-time compilation completeness; builder placement is still agent-oriented; recipe-specific preparation exceptions lack a validated compatibility/gating contract; Controller and target readiness are not yet enforced throughout install/run. Initial RolloutPreparation DTO was a standalone projection and only required a nonempty verified target identity. It must instead compare target identity to the exact expected model set or image identity. A validated DTO alone does not prove execution enforcement.

### Historical proposed refinements for discussion

1. **One immutable resolved preparation manifest.** Resolve existing recipe/model/distribution facts into one Controller-owned manifest containing exact image identity, complete model-file inventory, launch parameters, topology and any justified preparation requirements. Do not duplicate editable artifact inventories in recipes, profiles and cache entries. Profiles reference intent; the preview binds the resolved manifest and effects. A later recipe-contract update should remove ambiguity at the source rather than add parallel authorities.
2. **Separate expected identity from observed evidence.** Manifest facts are immutable; cache availability, per-Spark verification, driver compatibility and running health are timestamped observations. A target becomes staged only when observed identities equal the manifest, all required files are present and the image is imported. Recheck relevant facts at apply/start; avoid binding unrelated changing telemetry into the plan digest.
3. **Three useful user states.** Use 'Cached on Controller', 'Ready on selected Sparks', and 'Running'. Explain partial preparation with missing image/model bytes or a named blocker. Full details can expose intermediate progress. Do not label cached artifacts as executable readiness, or claim staged assets guarantee immediate runtime health.
4. **Compilation is an image-build responsibility by default.** Runtime binaries and known CUDA kernels belong in the image. Only demonstrated model/config/hardware-dependent work becomes a preparation exception. Its compatibility identity must include the actual dependencies that can invalidate reuse. Treat output as Controller-managed implementation detail, not a new top-level Library category or something the user must curate. Do not claim a boolean recipe declaration proves there is no runtime JIT: require build/preparation evidence and a representative launch test.
5. **Stage without interrupting inference.** Download, verify and import ahead of time where resources allow. GPU-dependent warm-up may conflict with the active workload; show that interruption separately and do not promise it is nondisruptive. Stop/start review must show unavoidable remaining work and endpoint downtime. Do not launch a hidden second model just to turn a readiness indicator green.
6. **Keep safe recovery explicit.** Partial fleet application reports per-Spark actual state and offers resume or a preview of restoration to a previous profile. Do not promise atomic fleet switching or automatic rollback: capacity, failed nodes and warm-up can prevent restoration. Retain previous immutable assets by default to make recovery practical.
7. **Freshness without surprise changes.** Updating a cached upstream version creates a new immutable version. It must not change a saved profile's pinned model or image silently. Offer 'Update available' and a reviewed profile revision. Readiness survives unrelated catalog changes but is invalidated by changed target identity, incompatible host state or missing/corrupt artifacts.

### Historical discussion choices

Recommended preparation behavior: 'Prepare profile' stages its exact model and image assets on all assigned Sparks; 'Cache on Controller' is available for exploration without occupying Spark storage. Neither action stops running workloads implicitly. If preparation requires disruptive GPU work, present it as a separate reviewed step.

Recommended switching behavior: deliberate whole-scope preview, retain prior assets, stop only conflicting runs, start desired runs, verify serving readiness, report partial success truthfully. Request routing changes and any rolling-switch guarantees require explicit recipe/topology support; do not imply universal zero downtime.

Recipe updates are deferred. Platform consumers should expose unresolved requirements rather than fabricate a compatible manifest. Before later recipe changes, agree the manifest authority and exception policy, then validate a representative ordinary runtime and one actual GPU-dependent exception end to end.

### Historical product-direction discussion: recipes should just work

User correction, 2026-09-05: readiness and mandatory review steps were
considered overcomplicated. This dated discussion superseded earlier proposals
at that time, but is itself superseded for active guidance by the cache-first
workflow below. Recipes in our repository remain our responsibility to test.

Normal workflow is select model/recipe and Sparks, then **Run**; or select a saved profile and **Switch profile**. That click authorizes the disclosed replacement of workloads on those selected targets. Show current and requested model placement inline at the action, not in a required extra review page. The Controller automatically resolves, fetches/builds if needed, copies, verifies, stops conflicting workloads, starts and checks service health. Show plain progress such as 'Downloading model', 'Copying to Atlas', 'Starting model', then actual running state. Do not require users to understand readiness levels, approve a digest, qualify a recipe or manually install dependencies.

Preview/digest/request-key, source verification, authorization, fit checks, exact target identity and concurrency controls are internal correctness mechanisms. The web and CLI can obtain and apply a plan within the requested action. A stale plan that changes targets, workload replacement or deletion must not silently broaden the user's action; replan automatically only within that intent, otherwise ask the concrete unresolved question. Routine predictable internal steps do not require confirmation. Failures explain what failed and offer retry/recovery without exposing contract terminology.

'Cache on Controller' remains a secondary Library action for advance downloads. 'Prepare profile' may remain a secondary optimization for staging without switching, never a prerequisite. Cache/target status can appear as quiet factual context or details, not a qualification system or a dashboard of readiness badges. Advanced users and agents retain optional dry-run/plan inspection and detailed JSON. Normal CLI Run/Switch performs the same orchestration as the web.

Repository maintainers own recipe testing and compatibility. The goal is every shipped recipe working on its declared supported hardware with sensible defaults. Unsupported configurations should be unavailable with a concrete explanation; do not ask users to certify our recipes. Existing coverage gaps remain engineering work, not claims that all present recipes have already passed physical tests. Recipe definition changes and recipe test campaigns remain deferred until the user resumes that work.

Acceptance adjustment: normal cached and uncached Run journeys and profile switching require no mandatory preview/approval screen. Automatic preparation must perform actual work and preserve the user's exact target/retention intent. Real missing credentials, insufficient resources, ambiguous placement or destructive cleanup outside that intent can require a specific actionable choice; never introduce a generic 'are you sure' ceremony. Existing reviewed-state UI fixtures/spec cases should become optional advanced inspection cases rather than the default path.


## Current cache-first profile workflow — confirmed 2026-09-10

The normal interface is simple, but cache readiness is an explicit lifecycle
gate. Cache preparation and profile apply are separate operations.

- **Prepare cache** downloads/builds the exact model and required runtime-image
  assets into Controller/NAS cache, verifies them, and reports what is ready.
  It does not silently revise a profile's identities.
- **Run** and **Switch profile** first validate that every selected profile
  choice is cached and verified. If not, apply is blocked with the missing
  model/image and the reason, and offers **Prepare cache**. Apply never fetches
  from upstream as a hidden prerequisite.
- Once ready, the Controller distributes the exact cached assets to selected
  Sparks in parallel, verifies them, skips already verified local copies, then
  stops and replaces conflicting workloads. It reports Copying, Starting and
  Running/readiness per Spark, with bytes when known and indeterminate progress
  otherwise.
- On failure, preserve completed work, identify the affected Spark and failed
  step, and offer a meaningful retry. A partially switched fleet must show what
  is actually running on each Spark. Spark copies remain derived state; NAS
  garbage collection handles unused local model objects according to the
  reference rules above.

Internal planning, verification and distribution grants remain automatic
implementation mechanisms. Advanced diagnostic APIs/CLI can expose them, but
the normal web and CLI journeys must make the cache gate and its reason clear.
Preparation and distribution speed are measured and reported; fast networking
does not justify fabricated instantaneous completion.
