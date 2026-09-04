# Vonk Forge interface: critical review and revised plan

Updated 4 September 2026. Baseline: freshly re-fetched `origin/main`, `a31106bb152d3bd9fe60313e544babb7fc0cc377`. Proposed design and implementation plan; no UI, Controller, or Spark changes made.

Execution details: [screen-by-screen implementation specification](interface-implementation-spec-2026-09-04.md). The specification defines visual rules, every primary screen and state, CLI parity, and acceptance IDs A01–A16.

## The product contract

**Library chooses and prepares what should run. Fleet shows what is running and Spark health. Web and CLI are equal clients of the same Controller.**

The user confirmed an operator audience and these responsibilities:

- Library: understand model capabilities, families and versions; download exact model artifacts to the NAS first and manage updates; assign models and exact recipes to Sparks; create profiles for quickly switching the running setup across all Sparks.
- Fleet: what is running now, and what is the state of each Spark?

The user further clarified that NAS-cached models make installation an implementation detail: the main actions are running models and switching profiles, with copying and cleanup performed as needed. Full agent-usable CLI parity is mandatory.

This supersedes the first plan's emphasis on endpoint handoff as the organizing principle. Using a ready model remains useful, but NAS caching and whole-fleet profiles are core product workflows, not secondary infrastructure or optional expert conveniences.

Vonk Forge's distinction remains any model through current frontier recipes, including distributed execution. PAIR supplies a reference for visual clarity, not the product blueprint. Its official [overview](https://docs.nvidia.com/local-ai/nvpair/) describes routing independent requests among compatible engines. This review does not claim measured superiority over PAIR.

## Critical conclusion

The intended split is sound. The current implementation only partially realizes it:

1. Library is organized mainly as a recipe repository table, not an understandable model collection and NAS cache.
2. NAS caching is still substantial open platform work; catalog synchronization and model metadata resolution do not mean model payloads are downloaded.
3. Current profiles do not cleanly express replacement of the running setup across all Sparks while retaining installed artifacts.
4. Fleet gives too much prominence to profile editing/comparison and duplicated summaries before showing current hardware and workload state.

A visual redesign alone would conceal these gaps. Establish the model/cache/profile contracts first, then make them easy to operate.

## Open GitHub issues and their implications

Read all seven open platform issues and the one open recipe-library issue on 4 September; retrieved issue comments yielded no additional comments for the seven platform issues. Issue text is backlog evidence, not proof of shipped behavior. Source inspection below verifies the central caching and profile gaps.

| Open issue | Relevance to this interface | Plan consequence |
|---|---|---|
| [#593 — NAS pre-cache](https://github.com/CarstVaartjes/vonk-forge/issues/593) | Immutable download once, verification, shared reuse, LAN distribution, repair, update discovery, explicit fallback policy and protected eviction. | Core Library work. Add a standalone Download to NAS action that requires no running/online Spark; independent cache status and update management. |
| [#594 — reliable progress and ETA](https://github.com/CarstVaartjes/vonk-forge/issues/594) | Transfer, verification, build/load phases and progress while normal telemetry pauses. | Shared progress vocabulary for NAS download, Spark transfer and profile switching; unknown totals must remain unknown. |
| [#597 — durable/resumable lifecycle](https://github.com/CarstVaartjes/vonk-forge/issues/597) | Checkpoints, verified reuse, interruption, duplicate requests, partial distributed recovery. | Required to make prepare/switch/retry trustworthy. Reopening a drawer is not sufficient durability. |
| [#596 — runtime preflight](https://github.com/CarstVaartjes/vonk-forge/issues/596) | Detect deterministic runtime incompatibilities before expensive operations. | Placement and profile readiness explain disk, memory, runtime and fabric separately. Spark preflight must not block NAS-only caching. |
| [#595 — deployment provenance](https://github.com/CarstVaartjes/vonk-forge/issues/595) | Exact repository, publication, deployment, recipe, model and runtime identity; evidence age. | Concise version/changed indicators in Library and Fleet, with an evidence inspector. Do not put every digest into the main table. |
| [#598 — failure evidence](https://github.com/CarstVaartjes/vonk-forge/issues/598) | Bounded sanitized operation diagnostics without SSH. | Fleet faults and failed Library operations show an explanation, failed phase, supported remedy and optional evidence download. |
| [#551 — self-update recovery](https://github.com/CarstVaartjes/vonk-forge/issues/551) | Spark package activation, rollback and canary behavior. | Spark maintenance/recovery detail. Keep separate from model update and profile switching. |
| [Recipes #57 — creator candidates](https://github.com/CarstVaartjes/vonk-forge-recipes/issues/57) | Distinct runtimes, checkpoints, alignment and topology; some vision-named models have text-only runtimes. | Show model capability separately from what the selected recipe actually exposes; do not infer capability or alignment from names. Candidate intake is not installability or physical acceptance. |

Two needs are not explicitly covered by those open issues: a complete model/capability/version comparison experience, and whole-fleet profile replacement semantics plus a full profile editor. They need dedicated scoped implementation work rather than being buried inside #593. No GitHub issues were created or edited by this review.

## Library: understand, cache, assign, compose

Use three subordinate views inside Library: **Models**, **NAS cache**, and **Profiles**. Updates are contextual across models and cache entries, with a filter/queue rather than another top-level destination. Keep Fleet and Library as the main navigation.

### 1. Understand the model before choosing its runtime

Present a navigable identity hierarchy: model family → model/version → exact weight variant → compatible recipes. A quantization, fine-tune or creator runtime is not interchangeable with a new model generation. Keep human identity labels clear while retaining exact artifact revisions underneath.

A model page should explain:

- What it accepts and produces: text, images, audio, video, embeddings or other declared capabilities, as supported by actual metadata.
- Which capabilities are available through each recipe, the configured context limit and material constraints. Distinguish declared support from tested evidence; unknown remains unknown.
- Version/variant differences: parameter size where known, quantization, alignment/fine-tune, upstream origin, runtime and topology. Avoid unverified quality rankings and speed claims.
- NAS download size/cache coverage, required Spark count, per-Spark storage/runtime memory and current fit.

The current capability list is eight broad options and is normalized partly by string matching (`library-workcell.tsx:69`). It is useful filtering, not a sufficient capability explanation contract. Recipes #57 is concrete evidence that titles are unsafe capability authority. Model capability must not automatically become an enabled recipe feature.

Keep broad discovery: a model that cannot run on today's fleet may still be useful to cache or plan for. Execution readiness, fleet fit and physical qualification remain separate. An executable Candidate is not automatically prohibited from installation.

Default results should expose model/version, recipe choices, useful capabilities, NAS cache state and Spark requirement without horizontal scrolling. Keep the 18-column table as an optional expert view; move the 13 header filters into contextual filter controls with visible active chips and URL-backed state.

### 2. NAS caching is its own action and state

**Download to NAS** must work before choosing Sparks, including with no Sparks enrolled or all Sparks offline. The operator must still select an exact weight variant/artifact set; downloading a vague family or every variant would waste large amounts of storage. A recipe can resolve required artifacts without committing to placement or execution.

Show independent state dimensions:

| Dimension | Example labels |
|---|---|
| NAS artifact set | Not downloaded · Downloading · Verifying · Cached and verified · Incomplete · Needs repair |
| Spark distribution/installation | Not on Sparks · Transferring · Installed on Aurora + Borealis · Partial |
| Runtime | Stopped · Starting · Running · Degraded · State unknown |
| Version comparison | Current pin · New model revision available · New recipe revision available |

“Cached” must mean verified coverage of the complete selected artifact set. Shared artifacts deduplicate across recipes by immutable identity. Auxiliary encoders/draft models and recipe-required files must be accounted for; a weight-only cache hit is not necessarily ready to run.

Cache management should show actual NAS free space and reserve, unique used bytes, in-flight downloads, referenced/protected entries and reclaimable entries. Separate **Remove from NAS**, **Remove installation from Spark**, and **Stop model**. Use #593's protected-eviction policy; uninstalling a recipe must not delete shared cache contents.

Current source distinguishes metadata-only resolution (`control/src/vonk_control/model_resolution.py:1`) and job input/output CAS (`artifact_blob_store.py:1`) from this model-cache requirement. Neither proves #593 exists. Automatic recipe catalog sync is also not a model download.

### 3. Updates are several different operations

Do not offer an ambiguous Update all that can change running models.

| Change | Correct behavior |
|---|---|
| Catalog metadata refresh | Refresh availability and descriptions; do not imply payloads have been fetched. |
| Model checkpoint/weight revision | Download and verify a new immutable cache identity; retain previous referenced version. |
| Recipe/runtime update | Show configuration/image/source changes and whether existing model bytes are reusable. |
| Repair current cache entry | Re-fetch the same exact pin into quarantine, verify, then replace; no version change. |
| Deploy updated choice | Explicitly revise assignments/profile and review runtime impact; existing runs remain pinned until switched. |

#593 already requests immutable update and repair behavior. Its direct-upstream fallback should be an explicit deployment policy: default NAS-first, visibly offer a fallback only where allowed. Silent upstream downloads on each Spark would undermine the user's stated caching model.

### 4. Assign model + recipe to complete Spark groups

From a selected model variant and recipe, show eligible groups, why each fits or is blocked, what is cached, LAN bytes required, and what would stop. Permit multiple independent placements of the same recipe and multiple models per Spark when admission permits. A distributed recipe is one placement spanning members, not separately selectable healthy-looking copies.

Lead with **Run on selected Sparks** and **Save to profile**. The Controller automatically copies missing verified artifacts, reuses existing content, prepares the runtime, and starts the model under one reviewed operation. Do not require users to map, distribute, install and load separately. NAS downloading remains an independent Library action. Optional advance preparation is available for operators who want predictable switch latency; it is not a mandatory stage of the ordinary flow.

Show exact recipe revision, weight identity, node group/rank mapping and runtime settings in the review. Keep technical identities expandable rather than replacing these contracts with vague “best recipe” automation.

## Profiles: full desired running setup, with preparation before switching

This is the largest contract gap.

Current `FleetProfileInput` has assignments and an installation policy but no independent fleet scope (`fleet_profile_contract.py:100`). The planner derives target nodes from assignments (`fleet_profiles.py:362`). Under `keep-cached`, unlisted workloads are not reconciled away. Under `exact`, unlisted installations in scope are stopped and uninstalled (`fleet_profiles.py:473–533`). An unassigned Spark is outside the derived scope, and an empty profile cannot express “stop every model.”

The UI creates keep-cached profiles and mainly appends a recipe from its detail page (`library-profile-composer.tsx:84–100`). It restricts creation to currently eligible groups (`:39–43`, `:110`), and rejects a repeated recipe revision even though the backend permits that revision on different groups (`:95`). This falls short of a full setup editor and offline planning.

The desired contract should separate:

- **Scope:** explicit fleet membership, independent of workload assignments. An all-Sparks profile includes deliberate idle outcomes. Resolve and bind membership in the reviewed plan; a newly enrolled Spark must not be silently affected after review.
- **Desired running state:** the exact model/recipe placements that should run, with all other runs in scope stopped. An empty running set can intentionally represent an idle fleet.
- **Retention:** preserve NAS cache and, by default, reusable installed artifacts on Sparks. Runtime replacement must not imply uninstall or eviction.
- **Readiness:** draft, needs downloads/preparation, ready to switch, switching, matched, partially applied, blocked or drifted. These are proposed UI projections, not existing API fields.

A profile editor belongs in Library and must support create, duplicate, rename, edit/remove assignments, select every Spark's intended outcome, capture current setup, compare and save. Drafting should be allowed with offline/busy Sparks; applying still requires fresh authoritative checks. Save immutable recipe/weight pins; do not make saved profiles silently follow latest.

**Switch profile** is the primary action. Its Controller-authored plan automatically performs missing copies/preparation, reuses cached artifacts, handles required space recovery, stops conflicting workloads, starts the target groups, and verifies the resulting state. Copy before stopping where resource admission permits. Offer optional **Prepare ahead** for predictable switching, not as a required user step. If preparation cannot coexist because of disk pressure, the plan must expose the ordering and interruption rather than promise no disruption.

“Quick switch” means NAS caching removes repeated upstream payload downloads, and retained Spark artifacts avoid repeated copies. It does not mean instant model loading, guaranteed zero downtime, or transaction-like rollback across hardware. If preparation becomes stale, revalidate. A failed member must produce a truthful partial/blocked result, retain completed work and expose supported recovery (#597/#598). Bind all changes to one reviewed plan and never display the profile as active/matched solely because it was selected or an Apply request was accepted.

Do not automatically expand scope to stop a distributed run that also uses excluded Sparks. Require the operator to review the complete affected group.

## Running is the operation; copying and cleanup are subordinate phases

The NAS is the reusable source of verified model artifacts. The ordinary mental model is **choose model + recipe + Sparks → review → run**, or **choose profile → review → switch**. Installation is not a separate product destination or a mandatory expert checklist.

Local transfer is expected to be much faster than repeated upstream downloads, but no LAN throughput was measured in this review. Large copies, hashing, image import, runtime preparation and model loading can still affect completion. Show those as honest progress phases under the requested Run/Switch operation, not as extra decisions unless intervention is needed.

Space recovery belongs in the same plan when required: first identify reusable content, then any unreferenced/stale staging or idle artifacts eligible under the configured policy. Include reclaimed bytes and affected content in the impact review. Preserve active/shared references and the NAS's verified source; deleting NAS cache remains a separate operation. Routine switching should not uninstall everything absent from the target profile or clean merely because a model stopped. Keep retained Spark artifacts when capacity permits so switching back can avoid even LAN copying.

The agent/operator approves the consequential plan once. Do not ask for additional confirmations for every transfer, verify, import and load phase already covered by that plan. If scope or consequential effects change, obtain a fresh plan rather than continuing with unreviewed changes.

## CLI parity is a release requirement

Extend **vonkctl**, not a separate agent-only tool. The web app and CLI must expose the same Controller-owned objects, decisions, permissions, previews, operations and results. Parity means equal capabilities and selectable options, not reproducing visual layout in terminal output. An agent must not need browser automation, SSH, direct database writes or an alternative workflow to complete normal tasks.

Current source has substantial foundations: `src/cluster_profiles/controller_cli.py` provides Fleet list/show/history/enrollment/upgrades, Library browsing/comparison and recipe lifecycle actions, artifact jobs and Activity, with JSON, pagination and idempotency keys. However, the reviewed command tree lacks saved fleet-profile CRUD/preview/apply and the proposed NAS-cache workflows. Its `fleet profile` command is a Spark display-name edit (`:304–308`), not saved profile management. Its `library run` currently shows an existing run (`:528`), while starting uses lower-level load actions. Names must become unambiguous in the revised CLI; these are not existing high-level run/switch commands.

Required parity matrix:

| Capability | Web and CLI must both provide |
|---|---|
| Discover/understand | Families, exact versions/variants, capabilities and recipe support, every applicable filter, sort, compare, pagination and custom recipes. |
| NAS cache | Download exact artifacts, list/show coverage and storage, inspect progress, check updates, repair a pin, review eviction and execute permitted cleanup. |
| Run models | Select model/recipe and complete Spark groups, inspect fit, preview/run/stop and observe authoritative results; automatic missing copies and space recovery. |
| Profiles | List/show/create/edit/duplicate/delete, capture current setup, explicit idle/scope, exact pins, complete diff, optional prepare-ahead, switch and inspect match/drift. |
| Fleet | Current runs/placements and node state, capacity, freshness, history, enrollment, naming, supported maintenance and recovery. |
| Operations/evidence | List/show/watch/wait, supported cancel/retry/resume, per-node phases, failure explanation, provenance and evidence downloads. |

Agent contract:

- Stable, documented JSON schemas and enum values; clean JSON on stdout and diagnostics on stderr. Represent unknown and stale evidence explicitly. Avoid parsing human progress text.
- Structured input files or stdin for complete profiles and complex requests; bounded pagination plus an explicit way to consume all results. Durable IDs are usable as inputs; ambiguous human names return candidates instead of guessing.
- Preview returns the complete impact, typed blockers, freshness and exact plan digest. Execute the same plan with explicit apply intent and a reusable request key; do not recompute a different destructive plan silently.
- Return a durable operation ID immediately and provide bounded wait/watch plus resumable inspection. Define exit/result semantics for accepted/running, succeeded, blocked, partial, failed, cancelled, timeout and connection loss. A CLI wait timeout does not cancel backend work, and request acceptance is not completion.
- Reuse the same idempotency key when reconciling an uncertain response; observe the existing operation instead of duplicating a switch or download. Progress survives the invoking agent process exiting.
- Backend validates all inputs and permissions equally for both clients. Use the existing secure credential configuration, never secrets in command arguments/output. Agents operate within the user's granted permissions and already-approved plan, not a second approval gate for every internal phase.
- Provide discoverable help, machine-readable valid options/contracts, and examples derived from supported commands. In the web UI, offer a safe equivalent CLI example for an operation where useful, without embedding credentials or encouraging replay of stale plans.

Illustrative future command groups are `models`, `cache`, `profiles`, `fleet`, and `operations` under vonkctl. Final syntax should be designed against the existing parser; this plan does not present those groups as already implemented.

Parity acceptance must prove observable outcomes against the same disposable Controller fixture: cache while Sparks are offline; run a cached recipe on a selected group; switch A→B with an idle Spark; inspect identical impact/state through the other client; retry a partial failure; reconnect after an uncertain apply without duplicate work. Compare normalized plans and authoritative results, not just command-name or API-route coverage. Every new web capability must have CLI acceptance in the same delivery slice.

## Fleet: current workloads and Spark state

Fleet should answer the user's two questions in its first useful viewport. Library prepares desired state; Fleet reports observed state and operational exceptions.

Recommended hierarchy:

1. Compact current-state summary: running model groups, healthy/attention/offline Sparks, operation in progress and observation freshness. Show the matched or last-applied profile plus drift honestly.
2. Running workloads: model/version, recipe, assigned Spark group and aggregate service/job state. One distributed workload row with expandable members; multiple replicas remain distinct.
3. Spark roster: friendly name, connection/agent state, available/reserved memory, disk pressure, workload names, active operation and one actionable fault summary. Detail opens telemetry history and deeper evidence.

For small fleets, compose the workload and Spark sections so both are visible together. For larger fleets, provide compact views and filtering without repeating counters. Keep a compact **Switch profile** shortcut in Fleet that opens the same Library-owned review; do not duplicate editing or restore the full profile builder as the default Fleet canvas.

Distinguish telemetry freshness from runtime health. Busy transfer with delayed metrics is not automatically a dead Spark; #594 supplies independent operation progress. One healthy rank does not make a distributed model available. Use last observed state with age when offline. For job recipes, show running work/output rather than falsely promising a serving endpoint.

The existing friendly identity resolver should be shared throughout Library/Fleet: the current Library rail prints raw IDs that wrap over many lines, whereas Fleet presents Aurora/Borealis. Preserve useful timelines and resource charts in Spark detail, but do not let charts displace the answers above.

## Revised delivery order

| Slice | Deliverable | Dependencies and proof |
|---|---|---|
| 1. Domain and UX contract | Model/capability/version distinctions; independent cache, install and runtime state; explicit profile scope and retention semantics. | Work against current contracts; no compatibility shims or invented supported capabilities. Define full-fleet switching separately from installation cleanup. |
| 2. NAS cache + progress | Standalone download, verification, cache inventory, reuse, repair and update review. | #593 with shared #594 and checkpoint behavior from #597. Cache while all Sparks are offline; one WAN payload for two Sparks. |
| 3. Library models + placement | Understandable model/version pages, recipe comparison, prepare/run placement, expert table/filter preservation. | Use actual metadata and preflight (#596). Distinguish model-advertised from recipe-supported capability. |
| 4. Profiles | Full editor, explicit idle Sparks, one switch action with automatic preparation, exact pins, stop-unlisted-running policy while retaining artifacts. | New dedicated profile-contract work plus #597. Switch A→B→A, multiple placements, offline drafts, complete impact and failures. |
| 5. Fleet simplification + evidence | Running groups and Spark state first; contextual progress/recovery and provenance. | #594/#595/#598; #551 remains maintenance. Healthy/degraded/offline/unknown cases agree across screens. |
| 6. Visual refinement and acceptance | Consistent typography, row hierarchy, compact navigation, mobile drill-down and keyboard access. | Preserve the graphite identity; reserve the warm matrix for explicit comparison. Run end-to-end operator scenarios before aesthetic sign-off. |

Every slice must ship its matching web and CLI behavior together, with shared Controller contracts and parity acceptance.

Visual studies should compare compositions within this agreed architecture, not ask the user again whether Fleet or Library owns the workflows. Show equivalent Library Models, NAS cache, Profiles and Fleet screens with real representative fixtures. A capacity-oriented expert view can remain available without redefining the product.

## Acceptance scenarios that prove the requested experience

- Discover a family, explain two versions/variants, and identify a capability unsupported by one of its recipes without reading JSON or trusting a title.
- Download an exact artifact set to the NAS with zero online Sparks. Pause/reconnect/retry preserves verified work under supported checkpoint semantics.
- Use the same cached model on two Sparks with one upstream payload download and verified local transfers; shared bytes are not double-counted.
- Download a new model revision while an old profile continues using its pinned version; update notification never changes the active run automatically.
- Prepare a profile containing two independent models, then switch to a distributed model on both Sparks; switch back using cached/staged artifacts.
- A whole-fleet profile with one intentionally idle Spark stops unlisted runs there while retaining cached models and reusable installations. An all-idle profile is explicit and reviewable.
- Review every change for plans over six steps. Reject a stale preview or changed scope without applying unreviewed changes.
- Show partial failure during switching and the actual remaining workloads. Never mark the profile matched when one target group failed.
- From Fleet, identify the running model/recipe on each Spark and the most important fault in a proposed 10-second moderated task. This target is not yet measured.
- Complete discovery/cache/placement/profile review and failure recovery using keyboard, and test phone layouts beyond merely containing horizontal overflow.
- Complete the same workflows entirely through vonkctl using structured input/output; inspect a web-created operation from the CLI and a CLI-created operation from the web.
- Run/switch from a NAS cache hit with no separate install command; show only required copy/verify/load/cleanup phases and reuse retained Spark artifacts on the return switch.

## Review evidence

Method: independent design review by `/root/design_review` and static detector/acceptance review by `/root/evidence_review`, followed by parent synthesis. Design assessment was completed before detector results were incorporated.

Reviewed a clean archive of the fetched commit, preserving the existing checkout and its unrelated uncommitted audit. Inspected PRODUCT.md, DESIGN.md, Fleet, Library, placement, profiles, lifecycle/progress, navigation, styles, and acceptance coverage. Rendered the existing local fixture journeys; no deployed Controller or Spark was contacted.

Build and three selected Chromium checks passed: Fleet detailed keyboard/history, Fleet compact/topology/mobile, and Library repository table/responsive containment. Inspected resulting desktop Fleet and Library screenshots and compact mobile Fleet screenshot. Static Impeccable detector returned zero findings. No live detector overlay was injected. The static result is not a visual-quality or accessibility certification. Build emitted a chunk-size warning above 500 kB; measure startup performance before prescribing splitting.

Four legacy Library browser journeys are explicitly skipped, including full preview/partial-failure/retry and empty/error recovery. Component coverage exists; this is a browser journey gap. Existing active coverage provides useful focus, Axe, responsive containment, and keyboard foundations. Safari/touch and real operator task timings were not assessed.

## Remaining review limitations

This follow-up used current issue bodies and source inspection. It did not rerun unchanged UI tests, execute live downloads or profile switches, or certify physical behavior. The earlier expert heuristic score was 24/40; the clarified profile semantics expose a deeper product-contract gap than that visual score captures.

The plan is ready for scoped implementation/design work, but the current UI must not claim NAS cache availability or full replacement profile behavior before the supporting contracts are implemented. No GitHub mutations, commits, deployment, or physical qualification were performed.


## Full metrics requirement

The [metrics implementation and visual acceptance contract](interface-metrics-spec-2026-09-04.md) adds the SparkDash/PAIR coverage ledger, F2 hardware and inference detail designs, provenance and aggregation rules, and acceptance cases A17–A23. It is required scope for web, Controller/native telemetry and CLI, supervised by Sol.

## Controller preparation decision

The [Controller-owned rollout preparation contract](controller-preparation-contract-2026-09-05.md) makes the Controller the preparation and delivery authority for both model files and runtime images. Profile preparation stages both onto selected Sparks before quick switching. This is required implementation scope.
