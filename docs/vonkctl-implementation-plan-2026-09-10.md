# vonkctl implementation plan

Baseline: freshly fetched `origin/main` at `ff6abaa5` on 2026-09-10.
Integration branch: `codex/vonkctl-cli-first`.
Approved design: `/private/tmp/vonkctl-cli-design-2026-09-10.md` (user-revised whole-fleet/latest-cache design).

## Outcome and controlling decisions

Ship one current CLI with Fleet, Model, Recipe and Profile. Default profile is numbered 1; explicit `--profile N` selects another. Every profile covers the entire enrolled fleet, idling unassigned Sparks on load. Edits autosave incomplete groups with informational warnings. Load resolves latest verified cached compatible recipes/models and validates complete groups before changing workloads. Saved profiles do not pin versions; execution records do bind resolved immutable identities.

Controller cache removal cancels the object's download/build, preserves saved assignments and all running Spark-local copies, and prevents late workers from republishing removed content. Profile views show missing cache even when actively running. Repeating active downloads attaches; failed/incomplete attempts resume; completed downloads refresh. Friendly exact selectors, truthful progress and per-Spark memory headroom/additional-disk fit are required.

Implementation is authorized, including necessary API simplification. No live fleet deployment or recipe catalog edits are part of this plan. Existing web consumers receive mechanical contract updates where necessary; visual redesign waits.

Explicit user clarification: all databases are development databases; no production databases exist. Deleting development data and recreating the schema is authorized where needed for this implementation. Resolve each actual database target before a reset; never run an indiscriminate volume deletion. Do not add Alembic/data migration, dual readers, old-field adapters or compatibility tables.

Final route naming correction: remove `/v1` from all Vonk-owned API routes, callers, generated schemas and tests. The final namespaces are `/api/fleet`, `/api/model`, `/api/recipe`, `/api/profile`. References below to `/api` describe the inspected baseline or earlier proposal only and are superseded. Integrator owns the comprehensive mechanical replacement after worker commits; workers use final unversioned paths in their changed files. Third-party/OpenAI-compatible inference protocol paths retain `/v1` where required by that external protocol.

## API cross-check against this baseline

| Area | Existing authority | Required action |
|---|---|---|
| Fleet/metrics | `/api/fleet`, agent APIs, fleet projections and metric routes | Reuse actual measurements, names and timestamped availability. Add bounded authenticated log access only where current job logs cannot meet Spark loginfo. |
| Enrollment/client upgrade | Agent grant/certificate and signed upgrade routes | Reuse Controller orchestration; CLI resolves friendly names and automatically binds preview/apply. |
| Model/Recipe discovery | `library_api.py`, `library.py`, `/api/library`, `/api/library/recipes`, catalog projections | Consolidate consumer filtering/projection into canonical library responses; support requested facets, sort and local/running/preparing union. Do not rename every URL solely to match CLI nouns. |
| Model cache | `model_cache_api.py`, `model_cache.py`, model-cache inventory/download/retry/cancel/eviction routes | Existing cancellation/retry useful; current profile/running-reference eviction protection contradicts cache independence. Implement targeted remove/cancel through one service operation and remove superseded deletion logic after caller audit. |
| Recipe cache | `recipe_image_availability_api.py` start/list/get/retry and availability/build workers | Reuse preparation; add cancellation/removal with durable late-publication prevention and latest cached update view. Coordinate model dependency cancellation, shared blobs and runtime image state. |
| Profiles | `fleet_profile_contract.py`, `fleet_profiles.py`, `fleet_profile_api.py` | Replace persisted explicit subset scope and revision-pinned authoring with numbered whole-fleet recipe-choice assignments and optimistic edits. Keep execution snapshots exact. Simplify current CRUD/preview/switch service rather than introducing a parallel profile store. |
| Operations | Model-cache, availability, run-switch and profile applications | Preserve durable IDs/reconciliation but expose progress under corresponding nouns. No new user-facing Jobs area. |
| CLI | `src/cluster_profiles/cli.py`, `controller_cli.py`, `control_client.py` | Replace old plural/admin/library/profile trees directly. Keep secure existing transport and current token setup; no compatibility aliases. |
| Contracts | Full `control/openapi.json`, generated Python/TS, canonical nested models, Rust schema generation if wire changes | Regenerate once integrated. All application routes remain in full schema; update connected consumers and actual stored-JSON round trips. |

Specific inspected mismatch: `FleetProfileInput` requires `scope`, assignment `recipe_revision_id`, ranked nodes and one endpoint owner; `FleetProfileScope` requires at least one node. These describe execution, not permissive autosaved authoring. Retain strict execution types separately from the one current authoring contract. `model_cache.py` computes profile/running protection and eviction refuses it; metadata references must no longer imply physical Controller blob retention.

User clarification: structure the operator API like the CLI. Use `/api/fleet`, `/api/model`, `/api/recipe`, `/api/profile` as the four operator namespaces, with action paths matching commands where practical. This supersedes the earlier suggestion to retain old operator URL naming. Reuse service internals, replace old operator routes and update actual callers together; no aliases. Keep machine-facing agent transport separate. Internal preview/digest binding remains, exposed beneath the matching noun where independently needed. All routes remain in full OpenAPI with canonical types.

Initial route allocation: Fleet owns `/fleet`, `/fleet/{node_id}`, `/fleet/{node_id}/rename`, `/fleet/enroll`, `/fleet/{node_id}/re-enroll`, `/fleet/{node_id}/remove`, `/fleet/upgrade`, `/fleet/{node_id}/loginfo` and nested metrics; Model owns `/model` (local union), `/model/library`, `/model/{selector}`, `/model/{selector}/download`, `/model/{selector}/remove`; Recipe mirrors local/library/detail/download/remove and `/recipe/update`; Profile owns `/profile` (list), `/profile/{number}` (read/autosave), `/profile/{number}/load`, `/profile/{number}/progress`, with preview under that same number. Define static routes before dynamic selectors. Resolve selectors in one authoritative way; return canonical identities. Final exact typed signatures are coordinated by workers before CLI binding. C owns model/recipe mutation routes; D owns their reads and all Fleet operator routes; B owns Profile. Root handles shared app wiring/auth and generators.

## Parallel implementation slices

All workers use model `gpt-5.6-luna`, reasoning `high`, isolated branches/worktrees from the same fetched baseline. Each reads AGENTS.md and the approved design. Workers communicate route/type decisions before changing shared contracts. No worker runs generators or edits another worktree. Each commits a scoped result after focused checks and reports changed files and remaining issues.

### A — CLI experience

Own `src/cluster_profiles/cli.py`, `controller_cli.py`, new `cli_*` presentation/selection modules, CLI-only tests and `docs/runbooks/vonkctl.md`. Existing transport `control_client.py` may be changed only after notifying integrator. Build four singular areas, numbered selection, exact readable selectors, adaptive terminal tables, truthful progress, plain/JSON output and required actions. Remove obsolete command handlers/aliases. Reuse API service contracts; coordinate profile/cache new payloads rather than freeze guessed endpoints. Keep parser/help usable while other slices land.

### B — Whole-fleet profile authority

Own `fleet_profile_contract.py`, `fleet_profile_api.py`, `fleet_profiles.py`, dedicated profile tests and mechanical profile-specific web consumers. Add stable Controller profile numbers, autosave concurrency and recipe-choice authoring, latest-compatible-cache resolution, entire current fleet including idle, resource/cache projections and immutable execution snapshots. Validate topology at load, not draft save. Request shared database field edits from integrator with exact patch requirements. Coordinate cache resolution queries with C. Remove retired authored scope/pin shapes and update genuine consumers.

### C — Model/recipe cache lifecycle

Own model-cache contracts/service/API/progress, recipe-image availability API/service/contracts, recipe build cancellation integration, dedicated lifecycle tests. Implement remove-as-cancel, independent Spark caches, shared-object accounting, durable publication exclusion, operation retry/refresh distinctions and recipe update-all service support where absent. Audit existing library deletion path and consolidate into one targeted service; coordinate files outside scope. Supply exact API payloads to A and cached-resolution interface to B. No profile files or shared DB edits without integrator coordination.

### D — Fleet and library projections/API

Own library/catalog projection and route modules, fleet projection/action modules, narrowly scoped new log modules and their tests. Cross-check concrete full OpenAPI coverage for filters and fleet diagnostics; implement missing canonical facets/sort/local-state unions, resource fields and bounded authenticated loginfo. Reuse existing telemetry/agent authorities. Coordinate production route wiring through integrator. Never simulate successful remote log collection; if protocol support is absent, implement the required connected path or report exact dependency for integration. Do not edit cache/profile/CLI files.

### Integration owner

Own this plan, approved design copy, shared database/schema edits, `api.py` wiring, generated clients/OpenAPI, cross-slice tests, AGENTS/runbook consistency and final reconciliation. Inspect each worker result, integrate scoped commits, resolve contract callers and remove retired artifacts with consumers. If Rust wire changes are needed, use Pydantic-derived schemas and typify, never handwritten parallel fields.

## Sequencing and integration gates

1. Fetch latest main, isolate integration worktree, inspect actual APIs, write this plan.
2. Fan out A–D. Each reports intended interfaces early; integration owner resolves shared files immediately.
3. Land backend contracts and lifecycle changes, then CLI bindings; regenerate full OpenAPI and clients after authoritative types settle. Update mechanically affected web consumers without redesign.
4. Run connected CLI→Controller tests for the accepted journeys, persisted JSON round trips, required/optional/null semantics, operation outcomes and failures. Do not accept a parser mock as proof that removal cancels a real worker or load changes the fleet.
5. Review removed aliases/routes/types for remaining callers, check formatting and relevant test suites, and record exact evidence plus deployment/hardware boundaries.

## Required acceptance scenarios

- Default and numbered profile persisted across clients; concurrent save rejection; every enrolled Spark visible; new Spark idle; empty profile idles fleet on load.
- One/three-member two-Spark draft saves with information; whole load blocked before mutation; valid group loads via actual Controller service.
- Cached recipe update changes next load automatically, leaves live workloads untouched, preserves selected model variant and freezes in-flight execution identities.
- Model/image absent on Controller but present/running on Spark: profile says missing cache and running; no stop or assignment deletion. Reload reuses verified compatible copies.
- Removal during transfer/build cancels, cleans partial objects, preserves shared blobs and Spark copies; late completion cannot republish; dependent operation cannot silently redownload.
- Repeated active/failed/completed download follows/resumes/refreshes respectively; valid old copy survives failed refresh.
- Exact model facets and recipe local-model default, explicit model override, readable selector round-trip, ambiguous mutation rejected.
- Per-Spark resource headroom includes overhead/reserve; disk fit uses incremental peak space; shared memory counted once; unknown measurements never become zero.
- Signed upgrade single/all, named enrollment/re-enrollment/removal, bounded sanitized log access, non-duplicated distributed throughput.
- Human output at 80/120 columns and plain redirected output; JSON parseable, errors and accepted/partial/completed states distinct.

## Verification environment

Use task-specific writable uv caches and the sibling recipe contract package. Run focused tests per slice then integrated Controller/CLI/schema/web checks. Before Linux/container-dependent testing, verify `docker context show` and `docker info` identify OrbStack; use appropriate disposable container harness when inputs exist. Preserve real Spark/NVIDIA acceptance as a separate gate. Do not manufacture missing harness inputs or claim a deployment from repository tests.

## Progress

- Latest main fetched and isolated at `ff6abaa5`.
- Initial API inventory and concrete authoring/eviction mismatches recorded above.
- Worker dispatch follows this plan; final integration and evidence remain pending.
