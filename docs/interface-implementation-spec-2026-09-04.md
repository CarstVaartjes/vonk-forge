# Vonk Forge implementation specification

Owner: root design/acceptance reviewer. Supervisor: Sol. Implementers: Luna. Baseline: origin/main a31106bb. Date: 4 September 2026.

This is the execution companion to `docs/interface-improvement-plan-2026-09-04.md`. The latest user requirements override both documents. The plan owns product behavior; this specification fixes screen composition, interaction details, acceptance, and handoffs. Sol owns exact API/type decisions in one shared contract ledger. Individual agents must not invent conflicting DTOs, routes, state vocabularies or defaults.

Visual reference: `/Users/carstvaartjes/.codex/visualizations/2026/09/04/01a06e3f-0a71-7fe3-a377-64bbebf9a969/vonk-design-reference.html`. This is an interactive composition reference with explicitly illustrative data, not production code. Its prototype controls demonstrate selected states; they do not excuse placeholder controls, fake data or missing flows in the application. Use the actual existing logo/icon system and real Controller data. Do not copy illustrative model capabilities, fit, sizes, timestamps or version labels into production fixtures as verified claims.

## Non-negotiable product decisions

1. Library owns Models, NAS cache, and Profiles. Fleet owns observed workloads and Spark state. Exactly two primary navigation destinations; Activity/admin remains secondary. A visible operation indicator is available throughout the app.
2. Run and Switch are outcome operations. Transfers, verification, runtime preparation and necessary cleanup are automatic Controller phases. There is no mandatory map/build/distribute/install/load checklist in the primary user flow.
3. NAS download is independently available with no online Sparks. Catalog metadata synchronization is not payload caching. Exact artifact coverage determines whether the selected model/recipe is cached.
4. A profile describes an explicit fleet scope and complete desired running setup. Covered Sparks without a workload are deliberately idle. Unlisted runs in scope stop. NAS and reusable Spark artifacts are retained by default. Running-state replacement and storage cleanup are separate concerns.
5. Models have capabilities; recipes expose a subset subject to exact runtime/configuration/evidence. Unknown support stays unknown. Candidate qualification is not a blanket install prohibition.
6. Every web capability has an agent-usable CLI equivalent in the same delivery slice. Both clients use one authoritative Controller plan, operation and error model.
7. Never convert a selection into a success claim. Selected profile, last applied profile and currently matched profile are different. A published process is not enough to infer healthy distributed serving. Cached bytes are not physical qualification.
8. No live NAS/Spark changes, production artifacts or upstream model downloads are necessary to implement and fixture-test this work. Physical acceptance remains separately reported.

## Visual system and first-viewport contract

Operate mode, inherited graphite identity. The product should feel composed and legible, not sparse to the point of hiding essential state, and not a repository spreadsheet stretched across a dashboard.

| Element | Execution specification |
|---|---|
| Main background | Neutral graphite `#101310`; no gradient, glow or texture behind operational text. |
| Rail | `#141814`, one 1px divider `#343e34`, 164–184px at desktop. Brand at top; Fleet/Library links below; local Controller/account at bottom. Preserve existing brand mark unless product assets explicitly change. |
| Surface | `#1a201b` for bounded Spark inspectors and expanded local details. Use rows/dividers for model, cache, profile and workload lists; do not put each row inside nested cards. |
| Text | Primary `#f0f2eb`; supporting `#b0b9aa`. Default body 14–15px/1.5; secondary 12–13px, never compress important facts to 10–11px. |
| Headings | System UI face as existing Operate authority. Page title 30–32px/1.2, weight 650–700, tracking no tighter than -.035em; section 18–20px, object 16px. Sentence case; no all-caps eyebrow labels. |
| Action | Restrained green `#a5d777`, dark ink `#162311`, only affirmative main actions. Secondary outline neutral. No multiple competing green buttons in the same decision area. |
| State | Green + text for positive state; amber `#efc27c` for attention; inherited error red with contrast for failure. Unknown is neutral, not red or green. Always include words; dots are reinforcement. |
| Comparison | Warm `#efede4`, ink `#20251d`, divider `#c8cbbd`, supporting `#586050`. Reserved for explicit review/diff; never a permanent dominating Fleet profile dashboard. |
| Spacing | 4px baseline; related label/value gaps 4–8px; row padding 18–22px; section gaps 26–32px; desktop main inset 30–36px. Outer card radius 8px, control 6px. Avoid giant 48–80px empty section gaps in an operator screen. |
| Controls | 42–44px effective height, visible focus, labels outside inputs where ambiguity exists. Table/context links may be visually compact but have an adequate pointer/keyboard target. |
| Numbers | Tabular numerals for bytes, memory, durations and progress. One unit basis at a time; label GiB/TiB consistently. Unknown is “Not available” or an explicit dash with accessible explanation. |
| Motion | Subtle state/panel transitions around 140–180ms; no staged content entrance that hides operational data. Reduced-motion disables nonessential transitions. Never animate changing health into an optimistic success. |

At 1280×900: Fleet title/action, compact status strip, current workloads and the first two Sparks must fit together. On Models, the first three representative model rows show identity, principal capability, cache state, Spark requirement and action without horizontal scrolling. At 1920px limit useful measure (~1400px main content), expand comparison/roster sensibly, never stretch labels across a huge empty ocean.

At 768px: rail becomes compact top navigation, content 22px inset, preserve two-card Spark layout only if labels fit. At 360px: 16px inset, navigation stays reachable, title/action wrap, rows become labeled stacks, two-Spark cards stack. Comparisons show per-Spark Before/After blocks rather than an illegible matrix. Model action remains adjacent to identity. No document-level horizontal scrolling at 320/360/768/1024/1280/1920px; optional expert table has its own clearly signaled horizontal region.

The reference's healthy, cache-progress, profile review and recovery compositions establish hierarchy. Production must additionally implement every state below. Root reviews all shipped primary states, not only the happy screenshot.

## Navigation and persistent state

Retain established /fleet and /library entry points. Recommended subordinate routing: Models at /library, cache at /library/cache, profiles at /library/profiles, profile detail/edit and operation detail use durable IDs. Sol/web agent may adapt route spelling to existing router constraints, but each drill-down must be addressable and support Back/Forward, refresh and open-in-new-tab semantics. Model/version/recipe drill-down must preserve search/filter/sort and selected Spark context.

Global operation indicator: count active operations, open list with title/phase/affected resource, then durable detail. Do not make the user open the account menu to find a running model transfer. Existing audit history remains accessible to permitted roles. A preview/review is not an active operation.

Loading: shell and known content stay stable; skeleton/inline status replaces only unavailable data. Refreshing keeps last verified result labeled with age. Error: scoped explanation + retry; never erase unrelated model/cache/fleet information due to one request failure. Empty: explain next useful action. No-results: preserve filters, show Clear filters. Permission-limited users can inspect allowed state; actions explain missing permission rather than silently vanish.

## Screen M1 — Models collection

Header: Models; subtitle “Understand the model. Choose how it runs.” Secondary Custom recipe action. Library tabs Models / NAS cache / Profiles. Search followed by All models, Cached on NAS, active filter chips and More filters. Do not repeat linked/unlinked/repository totals above this workspace.

Each default result row: model family/title; human version/variant; up to three meaningful capability summaries; cached/not-downloaded/partial state for the exact variant; recipe count and required Spark range; Compare recipes action. Group variants within a model where the existing identities support it. A family selection reveals versions, not a flat duplication of every recipe. Catalog refresh state is a small timestamp/disclosure unless an update/problem requires attention.

More filters retains the existing full filter capabilities (family, exact model, creator, format/quantization, runtime, capability, topology/Spark count, readiness, qualification, source, update/local state and alignment where explicitly known). Use controls appropriate to cardinality, searchable selects for long option lists, remove-one chips and Clear all. URL encoding and CLI interpretation must agree. Preserve an expert table view with configurable visible columns; action and identity remain easy to find.

Capabilities cannot be inferred from title, tags or substring-only aliases when authoritative typed metadata exists. If metadata is missing, show “Support not declared” and retain raw technical details. Do not infer standard/abliterated from absence of metadata.

Empty models: browse/refresh supported catalog and Create custom recipe. NAS unavailable: model discovery still works; cache cells show unavailable rather than Not downloaded. Fleet unavailable: model browsing and caching still work; compatibility is unknown rather than incompatible.

## Screen M2 — Model/version/recipe comparison

Header shows family → model/version → exact weight variant with a concise origin link. Body begins with what the model accepts/produces and material variant differences. Offer weight variant selection before Download to NAS. Do not require a Spark choice to download.

Recipe comparison columns: recipe/runtime identity, exposed capabilities/limitations, configured context when declared, complete Spark requirement, per-Spark memory/disk, cache reuse, physical evidence level, next action. Show unsupported/unknown capability explicitly. Keep source/runtime/model revisions in an expandable provenance section. Performance numbers require evidence, hardware/configuration and observation context; omit unsupported rankings.

Selecting a recipe opens its placement/run detail with recipe revision and model identity retained. Main actions: Download to NAS if missing, Review run when choosing execution, Save to profile. Uncached model can still enter Run review, which must expose required upstream→NAS preparation; caching is not a blanket UI gate. Candidate alone must not disable an otherwise executable recipe.

Custom recipe creation/edit/fork and existing artifact-job/non-chat functionality remain available. Do not discard them while simplifying the model collection. For non-serving jobs, name the correct start/use/output action rather than inventing an endpoint.

## Screen C1 — NAS cache

Header NAS cache; subtitle “Download once. Reuse across recipes and Sparks.” Action Choose a model links to model discovery with download intent. Summary shows verified unique bytes, actual filesystem free/reserved space, and protected/reclaimable state only when backed by API. Do not double-count shared artifacts or partial downloads as verified bytes.

Rows show exact model/variant, immutable revision short label, references by profiles/installations, complete/partial coverage, size, current phase and action. Normal actions: Inspect, View progress, Check for update, Repair pinned copy, Review removal. Group related entries without hiding independent revisions.

Download detail: upstream source and exact pin, missing files/dependencies, expected bytes, storage after reserve, known authentication requirement, one Download action. Result immediately links to durable operation. No online Spark required. Duplicate concurrent requests share work according to Controller identity/idempotency contracts.

Cache operation phases distinguish download, verification and atomic publication. Known totals show bytes/throughput/ETA only when supplied; indeterminate verification shows elapsed and last-progress time. Cancel/retry/resume are shown only where supported and retain the prior verified copy. An interrupted or corrupt entry is never offered as a valid source.

Update review has explicit Current pin/New pin, changed model artifacts vs recipe/runtime-only change, incremental bytes and affected saved profiles. Downloading a new version leaves existing profiles/runs pinned. Changing those references is a separate review. Repair re-fetches the exact existing identity; it is not labeled update.

Removal review lists protected references and removable bytes. If blocked, show why and link to the reference; never offer a force-delete bypass. Routine Spark cleanup must not evict NAS artifacts.

## Screen P1 — Profiles collection and editor

Collection rows show name/purpose, scope count, running workload count, intentional idle count, readiness/match state and Review switch. Current-match is based on observed state. Actions Edit, Duplicate, Capture current setup and Delete remain available without cluttering each row. No giant matrix on Fleet.

Editor is a full workspace, not an append-only recipe form. Fields: name, optional purpose, explicit included Sparks, workload assignments with exact recipe/weight revision, complete Spark group/ranks and unique alias where applicable. Each covered Spark shows its resulting workload(s) or Idle. Multiple coexisting models and independent copies of one recipe are allowed when contracts support them. Do not incorrectly prohibit the same revision on a different group.

Actions: Add workload, change recipe/version, move complete placement, remove assignment, mark idle, Save draft, Review switch. Draft save works with offline/busy Sparks if structural references are valid. Explain apply blockers without disabling draft authoring. Adding a recipe from Models opens this same editor context; it does not create a second profile system.

Save draft has no runtime side effect. Dirty navigation presents Save/discard/keep editing with focus restoration, not a blanket navigation lock. Save failures preserve entered values. Duplicate receives a distinguishable name and fresh identity. Delete explains removal of the saved definition and must not imply stopping current workloads; server contract determines protection during active application.

Scope is explicit and plan-bound. Newly enrolled or removed Sparks are shown as scope changes requiring review, never silently included by a stale plan. A profile with all included Sparks Idle is valid. A group crossing excluded scope must block or require explicit scope expansion; no partial distributed stop.

## Screen R1 — Run/switch review

Use a dedicated focused review view for long plans; a contained modal is acceptable for a genuinely short confirmation only. Warm comparison surface, direct title “Run [model]” or “Switch to [profile].” Back returns to originating selection/draft intact.

Before/After grouped by affected Spark and atomic workload. Each entry identifies model, recipe revision, intended run/idle state and group. Show stops, starts, retained artifacts, missing copies, required cleanup and expected interruption. Summary may collapse repetitive details but every step and blocker is inspectable; no six-step truncation. Default retention must read “Keep cached artifacts” when true, not an unexplained keep-cached policy code.

The action says Run model or Switch profile. It submits exactly the reviewed digest and request key once. While submitting, disable duplicate execution but keep inspection available. Unknown response: show reconciling state and query the durable operation/request key, never generate a new request and repeat blindly. Stale plan: show what changed, refresh review, do not apply a new plan automatically. All internal phases already approved by that plan execute without repeated user confirmations.

Blocked review: named reason, affected objects, evidence freshness and an enabled relevant recovery/inspect action. A disabled button labeled Resolve blockers is prohibited. Allow return to edit/save draft. Unknown capacity/preflight is not green fit.

## Screen O1 — Operation detail and recovery

Header outcome intent, current phase, started/elapsed time, complete scope and stable operation identity via Copy details. Sequential phases with parallel per-node subprogress where applicable. One overall state and clear member exceptions; do not display identical repeated errors in multiple panels.

Phases may include prefetch/verify/transfer/prepare/cleanup/stop/start/final verification. Only actual phases appear. Counts and bytes cannot regress on a reconnect; unknown totals remain indeterminate. Telemetry pauses must not erase operation progress.

Close/navigate does not cancel. Supported Cancel has its own impact semantics. Failure shows original cause, failed phase/member, preserved successful work, supported recovery action and sanitized evidence. Collector failure is a secondary diagnostic issue. Retry resumes at a valid checkpoint or obtains a fresh recovery plan when effects changed. No unsupported promise of global rollback.

Succeeded means intended final observed state verified, not accepted request. For serving models provide contextual connection details/test where supported; for jobs show appropriate inputs/results. Partial profile application lists actual remaining workloads and never claims full match.

## Screen F1 — Fleet

Header Fleet; subtitle “Your models and the Sparks running them.” Compact Switch profile action; Add Spark remains discoverable as a contextual roster action. One compact live/freshness summary, not duplicate health cards and filters.

Current workload rows: model/version, exact recipe label, running/degraded/starting/job state, complete Spark group and details action. Key by placement/run identity, not recipe revision alone. The same recipe on independent groups must show separate rows; a unhealthy rank must make group status non-healthy.

Spark cards/compact rows: friendly name, hardware, connection/freshness, current workloads, memory available/reserved, local storage pressure, active operation and next useful inspection. No raw ID wrapping. Default two-Spark layout fits alongside workload overview in the first desktop viewport. For larger fleets, compact searchable list and health filtering replaces uncontrolled card expansion.

Offline/stale states show last observation age and unknown current metrics; do not keep green Healthy labels next to an offline warning. Link one fault summary to affected model and Spark detail. Normal metrics remain quiet; errors do not generate stacks of duplicate warnings. Profile selected/applied/matched/drifted states are distinct, and profile editing stays in Library.

Spark detail retains telemetry history, exact runtime/agent provenance, events, enrollment/repair/upgrade controls for permitted operators. Stop/manage-model shortcuts use the shared Library review. Maintenance does not masquerade as model update. Preserve chart keyboard descriptions and accessible range controls.

## Shared API and CLI contract

Sol keeps one ledger listing endpoint, operationId, strict input/output, schema version, roles, state enum, digest inputs, idempotency, polling/streaming and recovery behavior. Backend worker owns its routes; integration owns generated OpenAPI/Python/TypeScript clients; web and CLI consume them. No arbitrary casts/dictionaries to bypass missing shared types. No server mutation that exists only in a React click handler.

CLI covers every applicable option above, including full filters, model comparison, custom recipes, cache/download/update/repair/eviction, profiles CRUD/capture/diff/switch, direct run/stop, Fleet/maintenance and operations/evidence. Extend existing vonkctl; make naming unambiguous (existing fleet profile renames a node and existing library run inspects a run). Exact new syntax is Sol/CLI-owner responsibility and must be documented as implemented, not invented in help examples.

Machine output: stable JSON schemas; stdout JSON only, stderr diagnostics; supported stdin/file input for complete profile documents; pagination and bounded all-results; typed errors and distinct success/accepted/in-progress/partial/failure outcomes. Apply requires explicit intent and plan digest, same request key for uncertain retries. Wait timeout stops waiting only. An agent can reopen operation state after exiting. Human format can be different but cannot expose different authority/options.

Test normalized requests/results across clients against one disposable Controller, excluding transport-specific presentation metadata. CLI-origin must be legitimate, not falsely labeled button/drag-drop. Secure token-file configuration remains; no secret argv/output or direct DB/SSH fallback.

## Acceptance IDs and owner handoffs

| ID | Observable proof | Owner |
|---|---|---|
| A01 | Zero online Sparks: exact artifact set downloads, verifies and is shown as cached in web/CLI. | cache + both clients |
| A02 | Two Sparks consume one immutable NAS payload; every destination verifies identity. Repeat run reuses existing content. | cache + run |
| A03 | Interrupt prefetch/transfer; restart service and resume valid completed work without duplicate publication. | cache + durability |
| A04 | Repair failure preserves old verified copy. Model update creates new pin without switching active run/profile. | cache |
| A05 | Removal blocks referenced content and reports actual reclaimable bytes. Shared NAS cache survives stop/uninstall. | cache + run |
| A06 | Model advertised vision but recipe text-only: comparison explicitly distinguishes them; no unsupported green badge. | run/model truth + web + CLI |
| A07 | Profile A distributed→B single+idle→A: correct stops/starts, explicit idle, cache retained and reused. | profiles + run |
| A08 | Empty desired run set idles every covered Spark; omitted scope members stay outside; cross-scope distributed change blocks. | profiles |
| A09 | Offline draft editing, duplicate/edit/remove assignments, same revision on independent groups, stale-scope review. | profiles + web + CLI |
| A10 | More than six steps: full impact inspectable, exact digest applied; stale preview rejects without mutation. | profiles + clients |
| A11 | Lost apply response then same request key returns one durable operation; wait timeout does not cancel it. | durability + CLI |
| A12 | One distributed member failure yields non-healthy group, accurate partial profile state, typed recovery and sanitized evidence. | durability + Fleet |
| A13 | Web-created operation inspectable via CLI and vice versa; equivalent normalized plans for identical intent. | integration |
| A14 | 320–1920px no document overflow; 1280px first viewport shows current workload and first two Sparks; actions visible in model rows. | web + root |
| A15 | Keyboard full flow, focus return, live progress announcements, reduced motion, loading/empty/error/retry. | web |
| A16 | Generated contracts match backend; relevant tests/build pass; source vs container vs physical evidence separated. | Sol |

Use tiny real fixture payloads and disposable services to prove byte delivery/verification rather than downloading frontier weights for tests. Do not substitute a mocked success flag for cache mechanics. Test semantic effects rather than matching source strings. Existing unrelated test/CI audits and other worktrees must remain untouched.

## Integration and review protocol

1. Workers report contract decisions before client integration, then scoped commits plus exact test evidence. No overlapping file edits between workers.
2. Sol reviews each diff for missing real integration, placeholder actions, state truth, cleanup scope, idempotency and schema policy. Cherry-pick to integration worktree; regenerate shared clients once coherent contracts land.
3. Each requirement above is tracked as implemented, verified, blocked or outstanding with evidence. Do not declare all complete when only a foundation landed. Explicitly distinguish linked issue support implemented from entire issue closure.
4. Root reviews actual browser screenshots against this reference at 1280×900 and 360px, including Fleet healthy/offline, Models and comparison, NAS cache progress/error, Profiles editor, full review and partial operation recovery. Add 1024/1920/zoom checks where structure changes.
5. One batched visual correction pass addresses hierarchy, density, copy, overflow and missing states; verify corrected screenshots. Use Impeccable detector once for changed UI, and independent finish review according to the skill. A clean detector is not a beauty verdict.
6. No live deployment and no physical-model acceptance claim. Remote push/PR/merge is coordinated with root after local integration and review.

Stop-and-escalate to Sol (not user by default) if API truth cannot support a promised control, if cleanup crosses profile scope, or if workers disagree on an identity/state. Solve with a shared contract change; do not ship a fake button, silently cut scope or invent supported behavior.


## Full metrics requirement

The [metrics implementation and visual acceptance contract](interface-metrics-spec-2026-09-04.md) adds the SparkDash/PAIR coverage ledger, F2 hardware and inference detail designs, provenance and aggregation rules, and acceptance cases A17–A23. It is required scope for web, Controller/native telemetry and CLI, supervised by Sol.

## Controller preparation decision

The [Controller-owned rollout preparation contract](controller-preparation-contract-2026-09-05.md) makes the Controller the preparation and delivery authority for both model files and runtime images. Profile preparation stages both onto selected Sparks before quick switching. This is required implementation scope.
