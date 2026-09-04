# Controller-owned rollout preparation

Explicit user decision, 2026-09-05: the Controller prepares quick rollouts; Spark agents download models and containers directly from it and run them. Required refinement of the interface implementation plan, NAS cache, profile preparation and run/switch contracts.

## Ownership and workflow

The Controller resolves exact model version, recipe revision, artifact set, compatible Linux/ARM64 runtime image and target placement. NAS-backed storage holds verified immutable model files and OCI content separately. The Controller orchestrates published-image retrieval or a compatible build worker; Controller ownership does not mean compiling CUDA workloads inside the API process or requiring NAS CPU compatibility. The runtime and known CUDA kernels are compiled into the Spark-target image when that image is created. Only unavoidable model-, configuration-, or hardware-dependent engine generation, JIT, or tuning remains as an explicit Prepare requirement with a compatibility key and reusable artifact identity. A normal Switch never repeats compilation that already matches those identities.

Prepare is a durable, preview-bound operation: resolve identities and compatibility; fetch/build missing artifacts; verify their digests; make them available through authenticated Controller delivery; optionally stage to the complete selected Spark group; verify target files and import the runtime image. Profile Prepare stages both model and image artifacts for its explicit target scope. Show per-object and per-Spark progress, bytes remaining and actionable failures. Reuse exact verified objects across recipes/profiles wherever identities match.

Spark agents use Controller-issued authenticated artifact delivery, verify destination bytes and image identity, import/reuse the image, then launch the pinned recipe. Normal rollout does not compile source or download from internet origins on Sparks. Recipe-specific GPU compilation, engine generation or tuning is an explicit exception with a typed preparation requirement and compatibility key. Required exceptions prevent the UI from claiming fully prepared; never silently fall back. Existing prepared workloads remain usable when remote catalogs are unavailable.

## States and review

Keep these facts separate: available upstream; preparing on Controller; ready on Controller; staging to Spark; ready on selected Sparks; starting; running. Controller cache completeness alone is not target readiness. Ready on selected Sparks means both exact model files and the executable image are verified there, with unavoidable remaining launch steps disclosed. Running requires actual readiness and endpoint evidence. Historical hardware qualification remains separate from these operational states.

Library shows model-cache status and runtime-image readiness independently, then one clear Prepare or Run action. Review lists missing model/image bytes, copy versus build work, target disk capacity, compatibility requirements, existing workload stops and any cleanup. Default retention preserves NAS objects and reusable target artifacts. A fully staged profile switch performs only necessary stops, launch and readiness checks; it must not rebuild or recopy unchanged objects.

## Distribution contract

Controller serves only verified immutable objects to authorized enrolled agents for approved operations. Grants bind object digest, size, target identity and operation; credentials are not exposed in UI, logs or copied commands. Support bounded streaming and safe resume/ranges with identity validation and final destination digest verification. No arbitrary filesystem path or caller-selected upstream URL. Models remain outside container layers. OCI manifests/layers or a verified archive retain canonical image identity through import. Reuse existing verified image delivery where possible rather than adding a competing transfer stack.

Network failure leaves a resumable checkpoint. Corrupt/incomplete files are not promoted to ready. Cleanup cannot remove in-use or referenced model/image objects, and Spark-local cleanup cannot evict NAS cache. Controller build workers and transfers are bounded durable jobs, not long blocking API handlers. CLI exposes the same preview, prepare, status, retry, staging and switching with stable JSON and request-key/digest semantics.

## Acceptance

1. Prepare an exact model+recipe for two Sparks: fetch/build once on Controller, deliver verified artifacts to both through authenticated transport; no Spark internet fetch.
2. Repeat preparation: no redundant model download, image build, target copy or import when verified identities still match.
3. Profile A to B to A retains and reuses prepared model/image objects; explicit idle targets remain idle. Check actual production operation orchestration, not only synthetic database state.
4. Interrupted delivery resumes safely; modified bytes, mismatched digest, unauthorized agent and stale plan cannot become ready or run.
5. Cached model with missing runtime image is visibly incomplete. Controller-ready but unstaged Spark is visibly incomplete. GPU preparation exceptions are visible before apply.
6. Web and CLI report identical identities, missing bytes, phases and target readiness. Stop/start success is not inferred from enqueue success.

Sol owns cross-worker integration and contract ledger. This authorizes repository implementation and tests, not live NAS/Spark deployment, external image publication or hardware acceptance claims.

## Design review and deferred recipe work — 2026-09-05

User requested design refinement and discussion before updating recipes. Do not modify recipe definitions/catalogs or introduce a new recipe schema as a side effect of this platform work. Requirements below are design decisions/proposals and an implementation gap ledger, not claims that current recipes enforce them.

### Verified gap ledger

Existing contracts bind model-version, harness and runtime-distribution references; runtime distribution pins ARM64 and an image digest; model-version inventory describes exact per-file hashes/sizes; build/import evidence binds output image identity. Existing run admission also checks installation, mapping/capacity/topology and readiness evidence.

Remaining gaps identified in review: recipe-local artifact inventory is not proven equal to the exact model-version file inventory; reported artifact-set identity is not consistently compared to the expected set; generic build does not establish ahead-of-time compilation completeness; builder placement is still agent-oriented; recipe-specific preparation exceptions lack a validated compatibility/gating contract; Controller and target readiness are not yet enforced throughout install/run. Initial RolloutPreparation DTO was a standalone projection and only required a nonempty verified target identity. It must instead compare target identity to the exact expected model set or image identity. A validated DTO alone does not prove execution enforcement.

### Proposed refinements for discussion

1. **One immutable resolved preparation manifest.** Resolve existing recipe/model/distribution facts into one Controller-owned manifest containing exact image identity, complete model-file inventory, launch parameters, topology and any justified preparation requirements. Do not duplicate editable artifact inventories in recipes, profiles and cache entries. Profiles reference intent; the preview binds the resolved manifest and effects. A later recipe-contract update should remove ambiguity at the source rather than add parallel authorities.
2. **Separate expected identity from observed evidence.** Manifest facts are immutable; cache availability, per-Spark verification, driver compatibility and running health are timestamped observations. A target becomes staged only when observed identities equal the manifest, all required files are present and the image is imported. Recheck relevant facts at apply/start; avoid binding unrelated changing telemetry into the plan digest.
3. **Three useful user states.** Use 'Cached on Controller', 'Ready on selected Sparks', and 'Running'. Explain partial preparation with missing image/model bytes or a named blocker. Full details can expose intermediate progress. Do not label cached artifacts as executable readiness, or claim staged assets guarantee immediate runtime health.
4. **Compilation is an image-build responsibility by default.** Runtime binaries and known CUDA kernels belong in the image. Only demonstrated model/config/hardware-dependent work becomes a preparation exception. Its compatibility identity must include the actual dependencies that can invalidate reuse. Treat output as Controller-managed implementation detail, not a new top-level Library category or something the user must curate. Do not claim a boolean recipe declaration proves there is no runtime JIT: require build/preparation evidence and a representative launch test.
5. **Stage without interrupting inference.** Download, verify and import ahead of time where resources allow. GPU-dependent warm-up may conflict with the active workload; show that interruption separately and do not promise it is nondisruptive. Stop/start review must show unavoidable remaining work and endpoint downtime. Do not launch a hidden second model just to turn a readiness indicator green.
6. **Keep safe recovery explicit.** Partial fleet application reports per-Spark actual state and offers resume or a preview of restoration to a previous profile. Do not promise atomic fleet switching or automatic rollback: capacity, failed nodes and warm-up can prevent restoration. Retain previous immutable assets by default to make recovery practical.
7. **Freshness without surprise changes.** Updating a cached upstream version creates a new immutable version. It must not change a saved profile's pinned model or image silently. Offer 'Update available' and a reviewed profile revision. Readiness survives unrelated catalog changes but is invalidated by changed target identity, incompatible host state or missing/corrupt artifacts.

### Discussion choices

Recommended preparation behavior: 'Prepare profile' stages its exact model and image assets on all assigned Sparks; 'Cache on Controller' is available for exploration without occupying Spark storage. Neither action stops running workloads implicitly. If preparation requires disruptive GPU work, present it as a separate reviewed step.

Recommended switching behavior: deliberate whole-scope preview, retain prior assets, stop only conflicting runs, start desired runs, verify serving readiness, report partial success truthfully. Request routing changes and any rolling-switch guarantees require explicit recipe/topology support; do not imply universal zero downtime.

Recipe updates are deferred. Platform consumers should expose unresolved requirements rather than fabricate a compatible manifest. Before later recipe changes, agree the manifest authority and exception policy, then validate a representative ordinary runtime and one actual GPU-dependent exception end to end.

## Superseding product direction: recipes should just work

User correction, 2026-09-05: readiness and mandatory review steps are overcomplicated. Recipes in our repository are our responsibility to test. This section supersedes earlier requirements for mandatory user-facing preparation/review ceremonies; integrity and execution checks remain internal.

Normal workflow is select model/recipe and Sparks, then **Run**; or select a saved profile and **Switch profile**. That click authorizes the disclosed replacement of workloads on those selected targets. Show current and requested model placement inline at the action, not in a required extra review page. The Controller automatically resolves, fetches/builds if needed, copies, verifies, stops conflicting workloads, starts and checks service health. Show plain progress such as 'Downloading model', 'Copying to Atlas', 'Starting model', then actual running state. Do not require users to understand readiness levels, approve a digest, qualify a recipe or manually install dependencies.

Preview/digest/request-key, source verification, authorization, fit checks, exact target identity and concurrency controls are internal correctness mechanisms. The web and CLI can obtain and apply a plan within the requested action. A stale plan that changes targets, workload replacement or deletion must not silently broaden the user's action; replan automatically only within that intent, otherwise ask the concrete unresolved question. Routine predictable internal steps do not require confirmation. Failures explain what failed and offer retry/recovery without exposing contract terminology.

'Cache on Controller' remains a secondary Library action for advance downloads. 'Prepare profile' may remain a secondary optimization for staging without switching, never a prerequisite. Cache/target status can appear as quiet factual context or details, not a qualification system or a dashboard of readiness badges. Advanced users and agents retain optional dry-run/plan inspection and detailed JSON. Normal CLI Run/Switch performs the same orchestration as the web.

Repository maintainers own recipe testing and compatibility. The goal is every shipped recipe working on its declared supported hardware with sensible defaults. Unsupported configurations should be unavailable with a concrete explanation; do not ask users to certify our recipes. Existing coverage gaps remain engineering work, not claims that all present recipes have already passed physical tests. Recipe definition changes and recipe test campaigns remain deferred until the user resumes that work.

Acceptance adjustment: normal cached and uncached Run journeys and profile switching require no mandatory preview/approval screen. Automatic preparation must perform actual work and preserve the user's exact target/retention intent. Real missing credentials, insufficient resources, ambiguous placement or destructive cleanup outside that intent can require a specific actionable choice; never introduce a generic 'are you sure' ceremony. Existing reviewed-state UI fixtures/spec cases should become optional advanced inspection cases rather than the default path.
