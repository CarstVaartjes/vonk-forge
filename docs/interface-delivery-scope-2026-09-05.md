# Agreed delivery scope — 2026-09-05

User authorizes autonomous implementation of all agreed work using best judgment, without routine approval pauses. This is the current product scope when earlier design proposals conflict. Sol supervises Luna implementation agents in isolated worktrees; root reviews integration and actual interface evidence.

## Product

- Linked Models and Recipes overviews in generated recipe-library catalog and Controller Library. Models explain families, versions, supported capabilities, weight variants and sizes; Recipes explain exact model binding, image/runtime, topology, settings and requirements. Facts must have source evidence; missing capability evidence is unknown, not inferred by unioning runtime interfaces.
- Download to Library caches on Controller/NAS. Run and Switch profile automatically prepare/copy/verify/start with honest progress and retry. No mandatory review, qualification/readiness ceremony or Prepare profile action in normal UI. Optional advanced inspection does not become a prerequisite.
- Profiles express entire selected fleet scope including idle Sparks. Switching changes only intended scope and preserves reusable models/images. Partial results show actual per-Spark state and durable recovery.
- Fleet makes current running model/recipe and Spark condition immediately clear. Deep SparkDash/PAIR metric coverage is available in hardware/inference details; correct units, histories, source/availability and shared-memory accounting are required.
- CLI mirrors web lists/filters/downloads/actions/progress/history with stable JSON and automatic normal workflows.

## Runtime and distribution

Controller orchestrates preparation and authenticated local delivery of separate model files and exact OCI images. Compile runtime/known kernels into images; exceptional model/hardware-dependent preparation is justified and cached internally. Build once and reuse. Sparks fetch verified artifacts from Controller and run them; automatic execution must bind real production services, not only injected test adapters. Maintain existing local Spark build fallback where a supported recipe requires it, with plain progress; do not claim every build can execute on NAS hardware.

## Recipe conversion and downloads

Convert entire existing vonk-forge-recipes publication to a small index plus independent self-contained per-recipe packages, including exact metadata closure and build sources. No shared-package dependency graph. Shared repository authoring is acceptable; CI packages all needed small files independently. Weights and OCI payloads remain outside packages. Preserve runtime behavior while updating metadata/tooling needed for linked model/recipe overviews.

Controller normal sync persists packages by digest and downloads only changed/new/missing packages. Validate candidate catalog before promotion; maintain previous catalog during failure/offline use and preserve pinned history. Test actual full converted catalog through Controller consumers, not unused tooling alone.

## Completion evidence

Track each requirement from source/producer through API, CLI and web. Integrate compatible current main across repositories, run relevant contract/service/consumer tests and Linux container checks in OrbStack/CI as appropriate, inspect actual desktop/mobile interface and execute normal workflows. Separate repository checks, publication/CI, deployment and physical Spark validation. Never report fake or unsupported metrics, no-op transfer phases or unbound adapters as completed behavior. Keep committed changes scoped and reviewable. Git recovery does not authorize destructive live storage changes.

## Delivery authorization

User explicitly includes merges into main and deployments in autonomous scope. They will be away and cannot enter sudo commands. Carry verified scoped changes through CI/merge and compatible deployments using existing authorized signed installer and Controller-managed paths. Coordinate one merge/deployment authority across agents and repositories. Inspect active workloads and deployment health; preserve live data/secrets/volumes and unrelated working trees. Do not deploy an incomplete or contract-incompatible intermediate state. Do not bypass privilege controls or depend on interactive sudo. If a necessary operation cannot execute under available permissions, record the precise blocker and continue independent work. Approval is already provided for ordinary delivery; only truly destructive actions need a separate decision. Record exact merged, published and deployed revisions and distinguish hardware acceptance from CI.


## Navigation correction — 2026-09-05

Explicit user feedback: the large left Fleet/Library sidebar wastes screen space and is visually unappealing. Remove the permanent sidebar. Use a compact horizontal app header with small Vonk Forge identity, Fleet and Library navigation, and secondary account/connection controls. Library has a compact second navigation row: Models, Recipes, NAS cache, Profiles. Main content uses the recovered width. On mobile keep Fleet/Library directly reachable, with secondary account/admin actions in an accessible menu; do not replace the sidebar with an oversized header. Preserve active-route indication, semantic navigation, keyboard focus, skip link and access to existing administration routes. This supersedes earlier sidebar width/layout prescriptions in the design reference. Verify desktop1280 and mobile360 after the combined visual correction batch.

## Shared vLLM cache invariant

User supplied concurrent-session context: the vLLM harness centrally injects and enforces XDG_CACHE_HOME=/outputs/cache and VLLM_CACHE_ROOT=/outputs/cache/vllm. The latest direction prohibits recipe repetition or overrides; it supersedes the earlier suggestion to permit alternate recipe cache paths. The effective runtime must provide writable /outputs while keeping non-root, no capabilities and read-only root. Validate the effective launch contract, not merely recipe text. Coordinate with the other session's in-progress/main fix before implementation; do not edit its worktree or assume a pasted merge request means the fix is merged. Package conversion must include/reference the exact corrected harness metadata and preserve this behavior across all vLLM recipes.

## Engine-wide writable runtime audit

User extends the vLLM observation to SGLang and other supported engines. Inventory actual harness/runtime families in the catalog, verify upstream cache/temp/JIT/compiler requirements, and centralize invariant writable paths in engine harness contracts. Recipe-specific model behavior remains separate. Avoid assuming all engines honor vLLM settings or blindly assigning environment variables. Check framework dependencies and persistent compatible kernel caches as well as primary engine paths. Preserve compiled image contents; writable mounts must not hide runtime installation directories. Validate effective environment/mount containment and reserved-variable conflicts. Test representative real consumers with non-root, read-only root and declared writable volumes; GPU-required behavior needs its matching hardware evidence. Coordinate concurrent vLLM fix and package/runtime workers to avoid duplicate changes. Report inventory coverage, unsupported probes and remaining gaps explicitly.
