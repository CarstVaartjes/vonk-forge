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
