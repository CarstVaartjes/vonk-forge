# vonkctl implementation plan

Baseline: freshly fetched `origin/main` at `ff6abaa5` on 2026-09-10.
Integration branch: `codex/vonkctl-cli-first`.
Approved design: `/private/tmp/vonkctl-cli-design-2026-09-10.md` (user-revised four-noun/cache-authority design).

Design status: this plan is an implementation design, not a report that the proposed CLI, web UI, routes or behaviors already exist. Any command, route, payload or worker behavior shown below is a design example until verified in the integrated implementation.

## Outcome and controlling decisions

Ship one current CLI with exactly four operator nouns: Fleet, Model, Recipe and Profile. Default profile is numbered 1; explicit `--profile N` selects another. Every profile covers the entire enrolled fleet, idling unassigned Sparks on apply. Edits autosave incomplete groups with informational warnings. Profile editing offers only complete, verified model/recipe choices from the authoritative NAS/Controller cache and stores their canonical IDs. Apply first checks that cache, offers `profile prepare-cache` when incomplete, then fans out the exact selected assets to all target Sparks in parallel and reports per-Spark progress. Spark-local cache is observational only and cannot make an uncached profile usable. Execution records bind exact immutable identities.

NAS/Controller cache removal cancels the object's download/build, preserves saved assignments and all running Spark-local copies, and prevents late workers from republishing removed content. Profile views show missing cache even when actively running, but a Spark-local copy alone never preserves applyability. Repeating active downloads attaches; failed/incomplete attempts resume; completed downloads refresh. The CLI may transform unique friendly names into canonical IDs; the web UI displays friendly labels behind dropdowns but submits canonical IDs. Truthful progress and per-Spark memory headroom/additional-disk fit are required.

Implementation is authorized, including necessary API simplification. No live fleet deployment or recipe catalog edits are part of this plan. Existing web consumers receive mechanical contract updates where necessary; visual redesign waits.

Web and CLI consume one identical operator API: same routes, canonical request/response types, operation semantics and role authorization. The CLI may resolve a friendly name to a canonical ID before the request; the web UI uses canonical IDs behind friendly-label dropdowns. Only authentication transport differs (browser session cookie with CSRF protection versus CLI bearer token). No CLI-only/web-only business endpoints, DTOs, presentation-dependent service behavior or duplicate orchestration. Agent transport is separate by function, not by UI audience.

Explicit user clarification: all databases are development databases; no production databases exist. Deleting development data and recreating the schema is authorized where needed for this implementation. Resolve each actual database target before a reset; never run an indiscriminate volume deletion. Do not add Alembic/data migration, dual readers, old-field adapters or compatibility tables.

Final route naming correction: remove `/v1` from all Vonk-owned API routes, callers, generated schemas and tests. The final namespaces are `/api/fleet`, `/api/model`, `/api/recipe`, `/api/profile`. References below to `/api` describe the inspected baseline or earlier proposal only and are superseded. Integrator owns the comprehensive mechanical replacement after worker commits; workers use final unversioned paths in their changed files. Third-party/OpenAI-compatible inference protocol paths retain `/v1` where required by that external protocol.

## API cross-check against this baseline

| Area | Existing authority | Required action |
|---|---|---|
| Fleet/metrics | `/api/fleet`, agent APIs, fleet projections and metric routes | Reuse actual measurements, names and timestamped availability. Add bounded authenticated log access only where current job logs cannot meet Spark loginfo. |
| Enrollment/client upgrade | Agent grant/certificate and signed upgrade routes | Reuse Controller orchestration; CLI resolves friendly names and automatically binds preview/apply. |
| Model/Recipe discovery | `library_api.py`, `library.py`, catalog projections | Consolidate consumer filtering/projection into canonical Model and Recipe responses at `/api/model/library` and `/api/recipe/library`; support requested facets, sort and local/running/preparing union. There is no separate Library operator namespace. |
| Model cache | `model_cache_api.py`, `model_cache.py`, model-cache inventory/download/retry/cancel/eviction routes | Existing cancellation/retry useful; current profile/running-reference eviction protection contradicts cache independence. Implement targeted remove/cancel through one service operation and remove superseded deletion logic after caller audit. |
| Recipe cache | `recipe_image_availability_api.py` start/list/get/retry and availability/build workers | Reuse preparation; add cancellation/removal with durable late-publication prevention and new cached-revision view. Coordinate model dependency cancellation, shared blobs and runtime image state. |
| Profiles | `fleet_profile_contract.py`, `fleet_profiles.py`, `fleet_profile_api.py` | Replace persisted explicit subset scope and revision-pinned authoring with numbered whole-fleet cached model/recipe-ID assignments and optimistic edits. Profile choices come only from the verified NAS/Controller cache; apply preflights that cache and offers prepare-cache before any Spark mutation. Keep execution snapshots exact. Simplify current CRUD/preview/apply service rather than introducing a parallel profile store. |
| Operations | Model-cache, availability, run-switch and profile applications | Preserve durable IDs/reconciliation but expose progress under corresponding nouns. No new user-facing Jobs area. |
| CLI | `src/cluster_profiles/cli.py`, `controller_cli.py`, `control_client.py` | Replace old plural/admin/library/profile trees directly with the four operator nouns. Keep secure existing transport and current token setup; no compatibility aliases. |
| Contracts | Full `control/openapi.json`, generated Python/TS, canonical nested models, Rust schema generation if wire changes | Regenerate once integrated. All application routes remain in full schema; update connected consumers and actual stored-JSON round trips. |

Specific inspected mismatch: `FleetProfileInput` requires `scope`, assignment `recipe_revision_id`, ranked nodes and one endpoint owner; `FleetProfileScope` requires at least one node. These describe execution, not permissive autosaved authoring. Retain strict execution types separately from the one current authoring contract. `model_cache.py` computes profile/running protection and eviction refuses it; metadata references must no longer imply physical Controller blob retention.

User clarification: structure the operator API like the CLI. Use `/api/fleet`, `/api/model`, `/api/recipe`, `/api/profile` as the four operator namespaces, with action paths matching commands where practical. This supersedes the earlier suggestion to retain old operator URL naming. Reuse service internals, replace old operator routes and update actual callers together; no aliases. Keep machine-facing agent transport separate. Internal preview/digest binding remains, exposed beneath the matching noun where independently needed. All routes remain in full OpenAPI with canonical types.

Initial route allocation (design example): Fleet owns `/fleet`, `/fleet/{node_id}`, `/fleet/{node_id}/rename`, `/fleet/enroll`, `/fleet/{node_id}/re-enroll`, `/fleet/{node_id}/remove`, `/fleet/upgrade`, `/fleet/{node_id}/loginfo` and nested metrics; Model owns `/model` (local union), `/model/library`, `/model/{model_id}`, `/model/{model_id}/download`, `/model/{model_id}/remove`; Recipe mirrors local/library/detail/download/remove and `/recipe/update`; Profile owns `/profile` (list), `/profile/{number}` (read/autosave), `/profile/{number}/prepare-cache`, `/profile/{number}/apply`, `/profile/{number}/progress`, with preview under that same number. Define static routes before dynamic IDs. The CLI may resolve friendly names to canonical IDs; web dropdowns submit IDs directly. Resolve IDs in one authoritative way and return canonical identities. The apply plan must identify the exact trusted NAS/Controller assets, avoid an extra hash of trusted NAS cache content, then fan them out to Sparks in parallel. Final exact typed signatures are coordinated by workers before CLI binding. C owns model/recipe mutation routes; D owns their reads and all Fleet operator routes; B owns Profile. Root handles shared app wiring/auth and generators.

## Parallel implementation slices

All workers use model `gpt-5.6-luna`, reasoning `high`, isolated branches/worktrees from the same fetched baseline. Each reads AGENTS.md and the approved design. Workers communicate route/type decisions before changing shared contracts. No worker runs generators or edits another worktree. Each commits a scoped result after focused checks and reports changed files and remaining issues.

### A — CLI experience

Own `src/cluster_profiles/cli.py`, `controller_cli.py`, new `cli_*` presentation/selection modules, CLI-only tests and `docs/runbooks/vonkctl.md`. Existing transport `control_client.py` may be changed only after notifying integrator. Build four singular areas, numbered selection, exact readable selectors, adaptive terminal tables, truthful progress, plain/JSON output and required actions. Remove obsolete command handlers/aliases. Reuse API service contracts; coordinate profile/cache new payloads rather than freeze guessed endpoints. Keep parser/help usable while other slices land.

### B — Whole-fleet profile authority

Own `fleet_profile_contract.py`, `fleet_profile_api.py`, `fleet_profiles.py`, dedicated profile tests and mechanical profile-specific web consumers. Add stable Controller profile numbers, autosave concurrency and cached model/recipe-ID authoring, entire current fleet including idle, resource/cache projections and immutable execution snapshots. Restrict editing choices to complete verified NAS/Controller cache entries. Add cache-first prepare-cache/apply behavior: block before Spark mutation when cache is incomplete, then fan out exact trusted assets in parallel with durable per-Spark progress. Validate topology at apply, not draft save. Request shared database field edits from integrator with exact patch requirements. Coordinate cache availability queries with C. Remove retired authored scope/pin shapes and update genuine consumers.

### C — Model/recipe cache lifecycle

Own model-cache contracts/service/API/progress, recipe-image availability API/service/contracts, recipe build cancellation integration, dedicated lifecycle tests. Implement remove-as-cancel, independent Spark caches, shared-object accounting, durable publication exclusion, operation retry/refresh distinctions and recipe update-all service support where absent. Treat NAS/Controller cache as the profile source of truth; expose complete verified asset IDs and preparation state to B. Do not require an expensive extra hash of trusted NAS cache content during apply. Audit existing library deletion path and consolidate into one targeted service; coordinate files outside scope. Supply exact API payloads to A and cache-preparation interface to B. No profile files or shared DB edits without integrator coordination.

### D — Fleet and Model/Recipe projections/API

Own Model/Recipe catalog projections and route modules, fleet projection/action modules, narrowly scoped new log modules and their tests. Cross-check concrete full OpenAPI coverage for filters and fleet diagnostics; implement missing canonical facets/sort/local-state unions, resource fields and bounded authenticated loginfo. Reuse existing telemetry/agent authorities. Keep web dropdown values canonical IDs while exposing friendly labels for display. Coordinate production route wiring through integrator. Never simulate successful remote log collection; if protocol support is absent, implement the required connected path or report exact dependency for integration. Do not edit cache/profile/CLI files.

### Integration owner

Own this plan, approved design copy, shared database/schema edits, `api.py` wiring, generated clients/OpenAPI, cross-slice tests, AGENTS/runbook consistency and final reconciliation. Inspect each worker result, integrate scoped commits, resolve contract callers and remove retired artifacts with consumers. If Rust wire changes are needed, use Pydantic-derived schemas and typify, never handwritten parallel fields.

## Sequencing and integration gates

1. Fetch latest main, isolate integration worktree, inspect actual APIs, write this plan.
2. Fan out A–D. Each reports intended interfaces early; integration owner resolves shared files immediately.
3. Land backend contracts and lifecycle changes, then CLI bindings; regenerate full OpenAPI and clients after authoritative types settle. Update mechanically affected web consumers without redesign; dropdowns display friendly labels but persist/send canonical IDs.
4. Run connected CLI→Controller tests for the accepted journeys, persisted JSON round trips, required/optional/null semantics, operation outcomes and failures. Do not accept a parser mock as proof that removal cancels a real worker or apply changes the fleet.
5. Review removed aliases/routes/types for remaining callers, check formatting and relevant test suites, and record exact evidence plus deployment/hardware boundaries.

## Mandatory legacy removal audit

Explicit user requirement: earlier simplifications left multiple versions of the same logic. Completion requires one current implementation, not another facade alongside retired behavior.

- Each slice supplies a removal ledger: retired route/command, DTO/table/field, implementation branch, generated asset and corresponding callers/tests replaced or deleted.
- No old plural/admin/library CLI aliases, subset-profile authoring, pinned-profile authoring, alternative persistence readers, compatibility adapters or schema migrations remain active.
- Compare actual registered OpenAPI routes and auth/operation registries, not only router filenames. Superseded routes must disappear, including generated clients and hidden or indirectly mounted routes.
- Trace each operation end to end: CLI → route → authoritative service → durable state → worker → observed result. Internal reusable helpers are allowed; duplicate orchestration implementations for the same operation are not.
- Search deleted symbols, old route families and retired table/field references across production code, packaging, tests, web callers, installers and documentation. Historical documents may remain only explicitly inert history; no executable consumer can restore retired behavior.
- Tests must exercise the replacement connected path and fail closed on retired forms where that proves the current public contract. Do not keep old fixtures or compatibility constructors just to preserve passing tests.
- Record any necessary remaining distinct path with its distinct job (for example profile authoring versus a frozen execution snapshot), rather than calling overlapping behavior a special case.

## Required acceptance scenarios

- Default and numbered profile persisted across clients; concurrent save rejection; every enrolled Spark visible; new Spark idle; empty profile idles fleet on apply.
- One/three-member two-Spark draft saves with information; whole apply blocked before mutation; valid group applies via actual Controller service.
- A newly cached recipe/model revision becomes a selectable canonical choice; editing a selected profile leaves live workloads untouched until apply and freezes in-flight execution identities.
- Model/image absent on NAS/Controller but present/running on Spark: profile says missing cache and running, apply is blocked before Spark mutation, and it offers prepare-cache. The Spark-local copy is observational only; after preparation, apply fans out the exact NAS/Controller assets.
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
- Four Luna/high implementation workers dispatched: A CLI, B profiles, C cache lifecycle, D Fleet/Model/Recipe API. An additional Luna/high read-only audit checks cross-slice legacy-removal blind spots.
- Integration commit `903d3c37` removes owned API/agent version prefixes across 246 tracked files; no old `/api/v1` or `/agent/v1` references remain at this stage. External inference paths and protocol identifiers are retained.
- Prefix-change checks: 42 schema-completeness/client-request tests passed; another 184 auth/agent/healthcheck/generated-client tests passed. Regenerating OpenAPI with `--schema-only` produced no diff. All 29 native agent client tests passed under OrbStack Linux/Rust 1.97.1 after replacing an old positional test-server path parser with an explicit prefix parser. Native macOS compilation encountered existing Linux-only helper filesystem APIs, so Linux was used. These checks validate the prefix change, not yet the new CLI/profile/cache implementation.
- OrbStack verified as `orbstack`, operating system `OrbStack`, architecture `aarch64`. Full native agent library follow-up passed all 144 tests (including the 29 client tests), covering additional enrollment/execution route consumers.
- Final worker integration and full acceptance evidence remain pending.
- CLI commits `a1490ea7` and `25033fab` replace retired command trees and align current request contracts. Review still requires exercising watch/follow, confirmations, and terminal exit status against the completed service contracts; parser flags alone are not acceptance.
- Shared authentication accepts cookie plus CSRF or bearer on the same operator routes, including Fleet streaming. Login/session/logout/token creation are the only browser-specific surface. Focused auth, profile API and stream checks pass (52 tests).
- `4bc43d8f` disconnects the retired proposal/change authority, Library placement authority, and direct Recipe/Run-Switch public execution routes. Workers are removing their underlying retired implementations and consumers; this is not a compatibility-wrapper strategy.

## Cross-slice removal ledger

Before completion, verify all of the following against the integrated checkout:

- Remove proposal/change public DTOs, services, persisted proposal table, clients and fixtures. Preserve only authority-head state still used by enrolled agents and Fleet evidence.
- Remove hidden Library profiles, placement facade/routes/contracts and retry branches. Numbered whole-fleet profiles are the sole operator workload authority. Frozen application records remain the durable execution receipt, not another authoring model.
- Remove orphan `library_operations` authority and its isolated tests.
- Remove bulk eviction routes/DTOs/worker kind. Targeted model/recipe removal cancels related work and preserves Spark-local copies and other cache references.
- Remove direct public mapping/build/install/run/stop/uninstall and Run-Switch routes. Keep only current internals needed by Profile execution; delete the distinct model-uninstall operation end-to-end.
- Replace old human Agent routes with Fleet actions; retain authenticated Agent transport as a distinct machine protocol.
- Fold deployment provenance into Fleet evidence; remove the separate web consumer and route.
- Update browser consumers, qualification runner, candidate-recipe acceptance and Spark lifecycle acceptance to current contracts. Do not leave executable old-route callers merely because physical acceptance needs separate hardware.
- Regenerate OpenAPI, Python and TypeScript clients and remove retired exported DTOs. Verify route completeness, authorization coverage and producer/store/consumer round trips.
- Remove obsolete auth/operation registry entries only together with their route removal. Verify current mutations retain explicit role checks for both authentication transports.
