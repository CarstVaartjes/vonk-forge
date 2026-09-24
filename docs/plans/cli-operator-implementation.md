# CLI operator experience: detailed implementation plan

Status: implementation authorized and underway. Package evidence and remaining
work are tracked in the [implementation status](cli-operator-status.md).

Prepared 2026-09-22 against repository commit
`49389f3dced50d2ecf7680e256dbe1827817eaee`. This expands the
[CLI operator experience design](cli-operator-experience.md) into executable
work packages. That document owns the product rationale and example output;
this document owns delivery order, code boundaries, contract changes, and
acceptance evidence. Proposed commands below are not current operator guidance.

For delivery planning, start with the milestone table in section 2 and the
package acceptance criteria in section 4. Section 8 defines the work record,
operator walkthrough, and release decision. The status document records
implementation evidence separately; an implementation step described here is
not a claim that it has passed.

Use these entry points to review or execute the plan:

- [Scope and completion rule](#1-scope-and-completion-rule): what belongs in
  CLI-first delivery and when the web handoff can begin.
- [Milestones and dependencies](#2-delivery-order-and-dependencies): the full
  sequence and the next work in the current checkout.
- [Latest implementation batch](#shared-build-implementation-batch): bounded
  changes, exact regression scenarios and the recorded gate before moving on.
- [Shared interaction contract](#3-decisions-shared-by-every-package): output,
  automation, consent, identity and recovery rules.
- [Work packages](#4-work-packages): changed owners, implementation steps and
  acceptance cases for W00–W19.
- [API change ledger](#5-api-change-ledger): canonical producer and consumer
  changes that must ship together.
- [Validation](#6-validation-commands-and-evidence-levels) and
  [interface references](#7-traceability-to-established-interfaces): how each
  decision is checked and which established practice informs it.
- [Package records and operator acceptance](#8-starting-implementation-and-reporting-progress):
  the evidence required to finish the work and begin the web design.

Execution checkpoint: 2026-09-24. Continue from the integrated evidence in the
[package status](cli-operator-status.md#package-state), rather than restarting
implemented packages. Admission locking, memory uncertainty, exact removal
consent, durable removal/cancellation and build recovery are implemented. All
23 applicable checks pass at source `134bcbf9`, including Linux/PostgreSQL wire
coverage and both ARM64 acceptance workflows. The connected same-profile
journey is now implemented and passes locally at `f6985e40`: installed-only
save/import causes no effects, explicit reviewed running intent retains the
same Profile, a new CLI process reconnects by application ID, and endpoint
discovery follows current signed observation and route ownership. The existing
walkthrough smoke and interactive-clock regression also pass. This is
Controller-owner evidence, not a running model or independent human result.
The genuine U2 fixture now also passes at `b1eee312`, including later-page
discovery, verified managed assets and actual bounded Qwen CPU inference. The
combined automated walkthroughs pass four tests; two human sessions are
intentionally skipped. The independent human U1–U8 scorecard remains open.
Exact checks and remaining evidence boundaries are in the status document.

The [shared-build closure checklist](#shared-build-closure-checklist) retains
historical implementation detail. It does not supersede the current status.
Six existing disposable walkthrough smoke scenarios pass together, and the
read-only JSON pipeline passes. The default synthetic U2 fixture proves cache readiness only; the explicit
Qwen asset option now supplies a locally verified runnable candidate. The user will run every model;
that campaign and an independent human U1–U8 scorecard remain separate evidence.
Do not replace either with a synthetic success or mark W19 complete from smoke
checks alone.

## 1. Scope and completion rule

Deliver a complete CLI journey: connect → inspect → choose → prepare cache →
save a profile → review → load → follow/reconnect → use results → recover →
clean up. Include both serving and artifact-producing recipes.

Extend the existing `vonkctl` entry point and Controller services. Keep Fleet,
Model, Recipe, and Profile as the operational domains. Shared API changes
include their generated clients and minimum existing web call-site changes in
the same delivery; web interaction and visual redesign remain deferred.

The CLI is ready for the web handoff when the named behavioral and operator
acceptance gates pass. Cosmetic perfection is not a gate. Browser-based token
issuance remains a documented bootstrap dependency; a second authentication
system is outside this plan.

### Keep Controller work bounded by a CLI outcome

Every Controller change in this plan must identify the operator action it
enables, the existing owner it changes, and an observable failure it prevents.
For example, W09 ownership work is required so a reviewed `profile load`
cannot overcommit capacity or execute different effects, and W12 needs shared
consumer ownership so cancelling one preparation does not stop another.
These are prerequisites of the CLI workflow, not a separate platform rewrite.

Use existing services, worker scheduling, storage contracts and generated API
clients. A general cleanup, unrelated resource policy, speculative scaling
feature, new scheduler or alternative persistence system is outside this
delivery. If inspection reveals an additional prerequisite, assign it to the
affected package and name the failing CLI scenario before adding work. Close
the package when that scenario and its required boundary tests pass; do not
keep expanding it to make the entire platform theoretically perfect.

The user authorized implementation and routine decisions on 2026-09-22 and
confirmed that there is no active production environment. Fresh-schema changes
are therefore permitted when they simplify the intended design; compatibility
migrations are not required. This does not turn repository tests into deployed
or physical acceptance evidence. During implementation, follow
[engineering principles](../engineering-principles.md),
[state ownership and coordination](../architecture-overview.md),
[API contracts](../api-contracts.md),
[testing policy](../testing-and-ci.md), and
[error reporting](../error-reporting.md). Coordinate artifact changes with the
pending [storage implementation](resilient-artifact-storage.md); do not assume
its ownership cutover has already shipped.

## 2. Delivery order and dependencies

Each row is a reviewable work package, normally one PR. Split a large package
only along a working vertical boundary. Never split a required API contract
change from its active producers, generated clients, consumers, and tests.
The order is a dependency sequence, not a promise of calendar duration.

| Package | Result | Depends on | Design phase | Main risk |
| --- | --- | --- | --- | --- |
| W00 | Current command/route inventory and executable baseline | — | P0 | Mistaking old documentation for current behavior |
| W01 | Predictable streams, exits, prompts, and process interruption | W00 | P1 | Breaking automation |
| W02 | Explicit task renderers and safe terminal text | W01 | P1 | Hiding unavailable or malformed state |
| W03 | Offline orientation, connection diagnosis, completion | W01 | P1 | Network or credentials required for help |
| W04 | Safe enrollment-grant delivery | W01, W03 | P1 | Lost or exposed one-time credentials |
| W05 | Trustworthy fleet/catalog inspection and selection | W02 | P2 | Client invents readiness or ignores later pages |
| W06 | Observation pinned to durable identities | W01, W02 | P3/P5 foundation | Following another operation or resubmitting work |
| W07 | Lossless profile authoring and import/export | W03, W05 | P3 | Silent saved-state loss |
| W08 | Cache preparation and lost-response recovery | W05, W06 | P3 | Duplicate requests or discarded usable assets |
| W09 | Controller admission bound to a reviewed plan | W07, W08 | P4 | Executing unreviewed effects |
| W10 | Interactive and scripted review/load workflow | W06, W09 | P4 | Confirmation bypass or ambiguous submission |
| W11 | Model-operation cancellation | W08 | P5 | Cancelling means eviction or stale publication |
| W12 | Recipe-operation cancellation | W08, W09d E5 | P5 | Cancelling shared work needed by another request |
| W13 | Profile-application cancellation | W09, W10 | P5 | Issued effects outlive cancelled intent |
| W14 | Activity, diagnosis, and authorized resume | W06, W11–W13 | P5 | History errors hide current work; unsafe retry |
| W15 | Published endpoint discovery | W05, W07, W10 | P6 | Showing a guessed or stale route |
| W16 | Artifact-job create/upload/submit/result workflow | W06, W08 | P6 | Duplicate jobs or unsafe file publication |
| W17 | Explicit cleanup and sequential maintenance | W05, W06, W09 | P6 | Broader deletion or upgrade scope than intended |
| W18 | Packaged interface, documentation, and process checks | W01–W17 | P7 | Source works but installed executable does not |
| W19 | Connected acceptance and web handoff | W18 | P7 | Declaring parity from mocked or partial evidence |

Update the runbook, examples, and affected tests with each package; W18 checks
their completeness. Land one PR at a time under the repository's publication
rule. A documentation-only merge is not evidence that a new image exists.

### Milestones and usable outcomes

These are cumulative acceptance checkpoints. They group the packages above;
they do not introduce a second backlog or change package dependencies.

| Milestone | Packages | Demonstration required to close it |
| --- | --- | --- |
| M0 — Baseline | W00 | Every operator task has an owner, current command/route, or named gap. Record the baseline revision and known failures. |
| M1 — Predictable CLI | W01–W04 | From a clean shell, obtain offline help, diagnose access, read the fleet, and deliver an enrollment grant privately. Pipes, JSON, prompts, interrupts, and exits obey one contract. |
| M2 — Choose and prepare | W05–W08 | Find an exact candidate across pages, explain its fit and cache blockers, prepare assets, preserve a complete profile, and reconnect to the original operation after response loss. |
| M3 — Review and execute | W09–W10 | Review the whole-fleet effect, submit that exact decision interactively and from a script, refuse a stale decision before dispatch, and reconcile a lost accepted response. |
| M4 — Complete operations | W11–W17 | Cancel and recover without losing usable assets or reviving old intent; retrieve serving and artifact results; perform bounded cleanup and sequential maintenance. |
| M5 — Qualified CLI | W18–W19 | Repeat the complete journey using the installed wheel and actual Controller services; record operator walkthrough results and the separate deployment/hardware evidence. |

The main dependency chain runs through W00 → W01 → W02 → W05 → W08 → W09 →
W10 → W13 → W14 → W18 → W19. W07 is another admission prerequisite; enrollment,
endpoint discovery, artifact jobs, and maintenance have their own prerequisites
in the table. None may be omitted to shorten the chain. Work can be drafted
before all prerequisites close, but integration cannot be declared complete
until the depended-on contract and tests pass.

W09 and W11–W13 carry the greatest uncertainty because they cross transaction,
worker, and storage boundaries. Close their concrete ownership decisions and
failing race/restart scenarios before estimating remaining delivery dates.
Measure effort after M1 and M2 using completed packages; do not schedule this
as twenty equally sized tasks. W18 process checks and documentation should grow
with each package, followed by final qualification at M5.

For the current working tree, use the [package-state table](cli-operator-status.md#package-state)
and this remaining execution order. The detailed requirements below remain in
scope; passing an individual package's own criteria does not close its open
admission or storage dependency.

1. **Complete at `ab25b1d7`:** W17's Controller-owned pre-removal impact review and exact review
   binding, including interactive consent, scripted review/acceptance, changed
   scope refusal and same-key recovery. Preserve current durable intent,
   reference fencing and crash-safe worker checkpoints.
2. **Complete at `ab25b1d7`:** W12's cancellation/removal race through actual PostgreSQL and
   managed-storage owners. Pending cancellation must retain its references;
   removing one intent must not delete another consumer's artifacts.
3. **Complete at `ab25b1d7`:** the disposable U8 cleanup setup, affected
   installed and connected gates, and required full CI at one source revision.
4. Supply U2 with an exact compatible image/model pair that really starts and
   computes a result. Its current verified empty image is insufficient. Keep
   the later-page selection and distinct cache-blocked candidate intact.
5. Run the independent U1–U8 operator walkthrough, correct dangerous or blocked
   tasks, and record the scorecard without implementation coaching. The
   every-model campaign, release merge/publication, deployment and physical
   qualification each retain their own evidence. Web redesign remains outside
   this CLI scope.

Completed increments and exact validation commands are recorded in the
[current status record](cli-operator-status.md); historical checkpoints below
must not be read as the current outstanding-work list.

This is a continuation order for the same twenty packages, not another backlog.
Record each closure in the status document; source inspection alone does not
upgrade a package to verified.

### Shared-build implementation batch

This W09d batch passed connected repository qualification on 2026-09-23.
The relevant implementation is present in `recipe_build_cancellation.py`,
`recipe_operations.py` and `run_switch_operations.py`, with initial regressions
in `test_shared_build_cancellation.py`. Preserve that work and its evidence;
do not restart W00 or treat these changes as a finished public cancel command.
The [cancellation and adoption checkpoint](cli-operator-status.md#w09d-shared-build-cancellation-and-unbound-adoption-checkpoint--2026-09-23)
records implementation, focused and final connected evidence for all four
changes below, plus unresolved availability adoption. Continue with the next
open dependency; do not repeat these changes simply because they remain
described in this checklist.

The four changes were delivered in order as parts of the existing E5 slice,
with one connected qualification at the end:

| Change | Concrete implementation and test work | Required result |
| --- | --- | --- |
| 1. Durable producer intent | Reconstruct the lifecycle service after detaching the Run/Switch parent. Read the typed producer intent from the accepted job, using the same database and managed storage. Exercise both an independently requested build and a build created solely for its parents. | Restart preserves the distinction: an independent producer remains active; a dependency build becomes eligible for cleanup only after its last current consumer leaves. Original request replay does not become new intent. |
| 2. New consumer during cleanup | Use independent PostgreSQL sessions and a barrier while last-consumer reconciliation holds the exact build boundary. Attempt real availability acceptance before cleanup commits, after cancellation is recorded, and after cleanup settles. Cover both unissued and issued builds. | Contention/pending cleanup produces a typed refusal with no partially accepted parent. Issued work retains capacity until exact cleanup evidence arrives. After settlement, a fresh accepted request can proceed; the cancelled request stays cancelled. |
| 3. Malformed history and fair progress | Place enough malformed dependency jobs before one eligible valid build to cross the reconciler's existing batch boundary. Run successive reconciliation passes through the actual service and database. | Invalid evidence never authorizes cancellation, but cannot starve unrelated valid cleanup. The valid build settles; malformed records remain explicit unresolved evidence. No arbitrary new request limit is introduced. |
| 4. Connected behavior and evidence | Update older Run/Switch assertions for immediate logical parent detachment, then run the connected profile, Run/Switch, availability, build and cancellation cases. Run applicable static/type/contract checks, then regenerate and verify changed supply-chain evidence. | No late callback resurrects the detached parent; independent/shared work keeps its claims; only exact cleanup releases issued capacity. Record the tested worktree and unresolved E4/E6/W09e cases separately from the tested commit. |

Service reconstruction in change 1 proves persistence through object
recreation; it does not replace E6's real process-death/restart acceptance.
The batch test in change 3 proves bounded scanning reaches unrelated work;
it does not establish a general scheduler throughput guarantee.

Keep logical detachment and physical settlement distinct. An internal
Run/Switch parent may have relinquished its demand while shared work continues
for another owner, or while exclusive issued work awaits cleanup. W12/W13
must project that distinction before declaring the public cancellation outcome
settled. Never infer that all remote work stopped from the parent's state alone.

With this batch recorded, follow the remaining W09d E4/E6 and C/D/F
dependencies above. If a new failure appears, assign it to its existing owner
and add the smallest reproducing
case. Do not expand this batch into unrelated platform redesign or repeat
passing checks without a new change, failure or unresolved boundary.

## 3. Decisions shared by every package

### Process and interaction contract

- Human results go to stdout; human warnings, progress, prompts, and failures
  go to stderr. Following is append-only and preserves terminal scrollback.
- JSON emits exactly one validated result, or one structured failure, on
  stdout. No prompt, progress prose, spinner, or duplicate stderr error.
  `--json --follow` remains one final/deadline document, not JSON Lines.
- Preserve API documents for ordinary JSON results. Introduce a small local
  `CommandOutcome` dataclass for process decisions, rather than inferring an
  exit code from every document containing `state`. It is not a wire DTO or
  another state authority. For observation metadata, define one CLI-owned
  wrapper containing the intact response plus observation status, identity,
  and reconnect command; never overwrite remote `state` with local timeout.
- Exit 0: successful read, admissible preview, completed follow, or explicitly
  detached accepted submission. Exit 1: awaited partial outcome. Exit 2:
  usage/configuration/transport/authority/contract failure, blocked preview,
  unsuccessful awaited operation, or observation timeout. Exit 130: local
  interrupt. Exit 141: closed Unix output pipe. Inspecting failed work is a
  successful read; awaiting its success is not.
- `--no-input` suppresses interaction; `--yes` supplies consent only where
  supported. Neither changes permissions or admission. JSON, redirected
  input, or unavailable interactive output must never prompt.
- Require explicit positive `--profile N` for save/import/load/cancel. Read
  defaults may use Profile 1 when displayed. Keep no persistent selection.
- Read commands, profile saves, and ordinary cache preparation need no extra
  confirmation. Review/load, cancellation, eviction, re-enrollment, node
  removal, and upgrades identify their consequential scope before consent.
  Preserve the signed CLI updater's existing `--apply` command contract.

### Authority and recovery contract

- The Controller owns readiness, fit, intent, authorization, admission,
  retries, cancellation, reservations, and operation state. The CLI formats
  those facts and makes bounded requests; it adds no scheduler or state cache.
- A request key is generated before submission and retained across an
  uncertain outcome. A replay keeps its body and identity. New deliberate
  work has a new key. Authorization failure is never retried as connectivity.
- Bound input bytes, response bytes, network time, observation time, and disk
  use according to the resource being protected. Invalid options fail before
  a request; remove silent numeric clamping. Report the bound and observed
  value. Internal retry scheduling may respect a bounded server delay.
- Every wait names dependency, owner, deadline, and resume condition. Server
  waits release execution slots and SQL transactions. Managed artifact locks
  remain nonblocking and outside SQL transactions, with fenced publication.
- Keep current canonical JSON semantics, strict unknown-field handling, and
  meaningful null/false/zero values. Generate contracts; do not import the
  Controller/Pydantic stack into the standalone root CLI package.
- Prefer existing owned typed JSON where it is the simplest authority. A
  necessary fresh-schema change is within the user's implementation
  authorization; update every connected contract and test together, without
  migration shims or another independent authority.

## 4. Work packages

### W00 — Freeze the current surface and baseline

**Touch:** `src/cluster_profiles/cli.py`, `controller_cli.py`, the full OpenAPI,
`docs/runbooks/vonkctl.md`, `docs/README.md`, and existing CLI test modules.

1. Inventory each parser leaf, flags/defaults, route, authorization scope,
   identity type, side effect, output, and follow behavior. Mark implemented,
   proposed, and unavailable independently. Map every web operator task to a
   CLI command or an explicit exclusion.
2. Resolve the documentation claim that all mutations use `--apply`: it is
   inaccurate for current Controller commands. Keep `update --apply` and
   `profile load --dry-run` distinct. Remove obsolete examples with their
   active callers instead of adding compatibility aliases.
3. Resolve the supplied guide's `fleet node-profile` reference against the
   owning node-configuration capability. The current parser lacks it; the
   presence of node-profile models/events is not a mutation API. Record the
   owner and required route if it is an active operator task. Do not rename
   workload profiles or restore the obsolete `fleet profile` alias. This
   inventory decision is required before claiming complete parity.
4. Capture current process behavior and run the focused baseline below. Keep
   an evidence log with commit, environment, commands, results, and boundaries.

**Regression evidence:** existing CLI/client/error tests. The earlier design
pass recorded 75 passing focused tests; that is a baseline, not proof of this
plan. `test_cli_controller_parity_acceptance.py` builds a small handwritten
FastAPI test server. Keep its useful client checks, but do not count it as
proof of the actual profile route, service, or PostgreSQL admission boundary.

**Done:** every workflow has a current owner and every gap has an assigned
package. Newly discovered substantive scope is named before its implementation.

### W01 — Separate process outcomes from remote documents

**Touch:** `cli.py` (`main`, `_emit`, `_control_error`), `controller_cli.py`
(`result_exit_code`, `_bounded_timeout`, `_bounded_interval`), and the CLI/error
tests. A small new `cli_outcome.py` is appropriate if it keeps these concerns
out of the command router; avoid a general command framework.

1. Add explicit outcome contexts: read, preview, accepted submission, awaited
   operation, and observer failure. Preserve a validated remote document
   separately from local completion/interruption metadata.
2. Centralize stream routing, JSON single-document emission, and exit mapping.
   Keep structured error code, received status, safe endpoint, request ID, and
   retry decision when present; never fabricate unavailable evidence.
3. Add `--no-input`, explicit profile-selection detection, and one prompt
   policy. Detect explicit selection independently from the default value.
4. Validate timing options with finite range checks, including NaN/infinity;
   do not convert invalid requests into valid clamped values. Preserve current
   documented limits initially, documenting network versus observation bounds.
5. Handle Ctrl-C and broken pipes at the process boundary. Do not issue remote
   cancellation or write a traceback/second message to a closed pipe.

**Tests:** extend `test_controller_cli.py` and `test_error_reporting.py`; add
subprocess coverage through the real entry point for stream/exit behavior.
Catch a failed-operation read returning 2, a blocked preview returning 0,
progress mixed into JSON, implicit Profile 1 mutation, a redirected prompt,
and a broken pipe that triggers remote cancellation. Each must fail against
the corresponding wrong behavior, not merely assert a constant exit table.

**Done:** every existing command is classified and the process contract is
true under a terminal, pipe, and JSON invocation.

### W02 — Replace fallback formatting with explicit presentations

**Touch:** `cli_render.py` (`render_payload`, `_rows`, `_row_value`,
`progress_line`), `cli.py` sanitization, and `cli_select.py`.

1. Dispatch renderers by known command/validated response contract. Implement
   fleet, model, recipe, profile, preview, application, job, and error views.
   Share only small formatting helpers. Remove retired alternate response
   shapes and flattened-dictionary fallbacks with their callers.
2. Show identity/scope, actual state/freshness, desired differences, blockers,
   and next action in that order. Distinguish empty, absent, stale, and failed
   observations. Never turn a contract error into an empty table.
3. Render 60 columns as stacked records where needed; use tables at 80/120
   only when meaning survives. Keep complete selectors copyable and disclose
   pagination. Render measured bytes and timezone-aware times, while leaving
   JSON integers and timestamp strings unchanged.
4. Escape terminal control sequences in every human text surface, including
   logs, names, errors, and suggested commands. Provide ASCII-safe rendering;
   color is optional and must obey `NO_COLOR`, `TERM=dumb`, and redirection.
   Remove the current whole-screen clear in watch callbacks.
5. Separate human display sanitization from machine output. A validated,
   nonsecret JSON result preserves complete strings, arrays, numbers, and
   timestamp spelling within the transport's declared byte limit. Do not
   truncate strings or collections, or insert terminal escape text into JSON
   values. Apply explicit credential-delivery/redaction rules at the owning
   boundary; arbitrary string replacement is not a lossless data contract.

**Tests:** `test_availability_presentation.py`, CLI/error tests, and a few
targeted width fixtures. Exercise ANSI/OSC injection, wide Unicode names,
long exact selectors, unknown totals, stale telemetry, and real zero values.
Include a valid long string and collection beyond the current display caps:
human output may abbreviate with disclosure, but parsed JSON must retain the
original values. Verify permitted secret delivery remains confined to W04's
private file and never reaches either normal output stream.
Avoid snapshots of every possible table; test meaning and stream behavior.

**Done:** task-critical identity, blockers, and next action survive every
supported width; ordinary use does not require interpreting nested JSON.

### W03 — Make orientation, help, and connection checks self-contained

**Touch:** `cli.py` parser/startup, `controller_cli.py`
`add_controller_commands`, `control_client.py` (`from_environment`,
`_read_token_file`), package assets, and operator runbook.

1. No arguments prints offline orientation and examples. Help/version and
   proposed `completion bash|zsh` return before client creation/update checks.
2. Generate completion from the real parser, with no duplicate command list,
   network lookups, credential access, or automatic shell-file edits.
3. Add `--check-connection [--json]`: validate local configuration/private
   regular token file, TLS connection, and an authorized lightweight Fleet
   read. Report verified origin/client identity and check results only; do
   not invent a Controller version or treat a cookie-only auth route as a
   bearer-token identity endpoint.
4. Document current browser token issuance, expiration/renewal, environment
   setup, scope, follow defaults, and two realistic examples per task family.

**Tests:** absent configuration does not affect help/completion; offline
commands make zero transport/update-check calls. Distinguish unsafe file
modes, symlink, missing file, expired token, 403, DNS, and TLS failures. Check
generated Bash/Zsh script syntax and installed-command completion.

**Done:** a new operator can reach an authorized Fleet read from the runbook;
diagnostics expose no token or unsafe TLS workaround.

### W04 — Deliver enrollment grants without losing or exposing them

**Touch:** `_fleet`, `operator_projection_api.py`, enrollment service/contracts,
secure file helpers, and enrollment/security tests.

1. Add required `--output FILE` to grant-producing `fleet enroll` and
   `fleet re-enroll`. Create/reserve an exclusive private destination before
   issuing the grant, hold its descriptor, and reject existing/symlink targets.
2. Write the exact grant bytes only to that private file, with durable close
   semantics. Normal human/JSON output contains nonsecret receipt data and
   destination, never the grant. Keep diagnostic redaction separate from
   deliberate secret delivery.
3. For interruption or write failure after issuance, identify the original
   grant's status through its authority. Use an existing authorized revoke or
   status operation if available; otherwise add that canonical capability
   before promising automatic recovery. Do not silently mint a second grant.
4. Show the affected enrolled identity and consequences before re-enrollment;
   retain existing expiry, single-use, and signature checks.

**Tests:** real enrollment authority plus temporary filesystem boundaries:
existing path, symlink/race, restrictive mode, interrupted write, expired or
consumed grant, and denied re-enrollment. Capture both streams to prove secret
absence. Follow existing `test_enrollment.py`, `test_enrollment_bootstrap.py`,
`test_operator_projection_api.py`, and authorization matrix seams.

**Done:** a grant is delivered once to a private file or an actionable,
nonsecret uncertain-issuance result is returned.

### W05 — Make inspection and candidate selection truthful

**Touch:** `fleet_projection.py`, `operator_projection_api.py`,
`library_contract.py`, `library_api.py`, `library_projection.py`, `_fleet`,
`_model`, `_recipe`, `_recipe_rows`, and selector/rendering helpers.

1. Project friendly workload/placement names, fresh authenticated observations,
   actual versus desired state, and attention reasons. Count a distributed
   placement once by its canonical identity, with its member Sparks shown.
2. Add recipe `--ready` and `--fits-fleet` through canonical query/response
   contracts. Derive assessment and explanation from the same Controller
   predicates used by preview/admission. Readiness means exact usable NAS
   assets and admissible placement; fit alone does not imply cache readiness.
3. Return an explicit unavailable assessment when evidence is insufficient.
   Filters must not turn an unavailable cache/fleet source into a misleading
   zero-result set. Respect the current managed-storage transition boundary.
4. Keep explicit page size/cursor for normal lists. Bound full selector scans
   by an overall deadline and detect repeated/cyclic cursors. Distinguish a
   page from an entire catalog. No arbitrary total-record cap.
   Bind continuation cursors to the matching canonical identities/revisions
   and query, not just the last sort key. If that collection changes, require
   a fresh read rather than combining old and new choices. This does not
   require retaining another server-side snapshot store.
5. Resolve exact names/IDs/accepted selectors. Ambiguity returns full
   candidates and mutates nothing. A title match never overrides canonical
   identity, and selection cannot silently choose a different model variant.
   Keep full canonical candidates in a typed list beside bounded error copy;
   carry it through the central HTTP boundary, generated clients, and human
   and JSON output. Returning an unbounded prose message is not sufficient:
   the normal error-detail boundary would truncate the choices.
6. Expose `fleet node-profile SELECTOR` as a focused read of the node-owned
   identity, labels, and lifecycle fields from the current Fleet detail
   projection. Resolve the exact node first and reuse that canonical owner;
   do not create another profile store or interpret this as a whole-fleet
   workload profile. Any additional node-edit operation discovered in W00
   needs an explicit authorized route and revision policy before exposure.
   Do not add the retired `fleet profile` alias.

**Tests:** `test_fleet_projection.py`, `test_operator_projection_api.py`,
`test_library_canonical_projection.py`, `test_library_sort_recency.py`, and
CLI tests. Include a candidate on page two, cyclic cursor, changed catalog
head, same title twice, one two-Spark placement, missing assets, stale capacity,
and unavailable projection owner. Reuse policy tests rather than restating
admission conditions in CLI fixtures.
Verify node-profile selection targets the enrolled node, preserves labels,
and cannot write or load a workload profile.

**Done:** one can choose a candidate and understand its precise blocker from
human output; JSON retains exact authoritative evidence and page scope.

**Assessment implementation decision:** `LibraryAssessment` composes the
existing Run/Switch placement and ModelCache owners. `inspect_candidate` shares
the planner's mapping, document, resource, and conflict predicates but cannot
create a build plan. The mapping owner supplies deterministic rank/role
assignment. Fleet fit describes current capacity, not hypothetical released
capacity after stopping workloads. Readiness additionally binds the planner's
image identity to exact authorized NAS assets. Primary-model cache metadata is
insufficient: the cache owner's exact recipe manifest checks every selected
file and companion dependency. NAS readiness is independent of Spark copies.

Assess only the displayed page for ordinary reads, but assess all otherwise
matching candidates before applying readiness filters and pagination. A
five-second shared assessment budget leaves headroom within the CLI's default
15-second request timeout. The internal maximum is ten seconds; no arbitrary
candidate-count cap is introduced. This is a computation/admission budget,
not cancellation of an in-progress storage read: late results are rejected.
No API observer owns a worker slot. An unexamined or stale candidate remains
unavailable, and a filter with unavailable evidence fails explicitly. Identity
selection uses the documented `assess=false` query and cannot combine it with
readiness predicates. This creates no second readiness store or authorization
path. Fleet presentation groups the existing run and installation presences by
their canonical IDs, keeping Controller state beside owner-projected observed
health, per-rank state, freshness, and membership. It does not calculate a
second health decision from whichever members happen to be visible. Saved
desired assignments remain the Profile owner's projection.

### W06 — Follow exactly one operation through interruption

**Touch:** `_poll_path`, `_follow_mutation`, `_watch_resource`,
`_submit_profile_load`, `_profile`, `progress_line`, and owning progress
contracts only where evidence is missing.

1. Represent an observation target internally as its domain, durable ID,
   canonical path, and reconnect command. Obtain it from a validated receipt
   or explicit selector, never by probing several namespaces until one works.
2. Add `model progress ID`, `recipe progress ID`, and profile progress
   selectors `--application ID` or `--request-key UUID`. Validate selectors
   are mutually exclusive and belong to the explicitly selected profile.
3. Without a profile selector, read latest once, then follow the returned
   application ID. A later load cannot replace it. Keep a resource watch
   distinct from awaiting an operation outcome.
4. Add `fleet progress JOB_ID` for supported fleet job receipts. The current
   upgrade's `operation_id` names a job; bind it explicitly to the job route
   or correct the canonical receipt field with all consumers. Never infer
   an operation route solely from the field's name.
5. Use monotonic observation deadlines; each HTTP timeout/backoff must fit
   within remaining time. Preserve last confirmed state and its age during
   transient loss. Retry bounded eligible reads, stop on denied authority or
   invalid contract, and report server wait dependencies and deadlines.
6. Exit with the observation wrapper at timeout; Ctrl-C leaves accepted work
   running. Unknown states fail contract validation rather than imply success.

**Tests:** extend existing CLI load/follow/lost-response regressions. Add two
consecutive applications, latest-route mutation while following, a hanging
HTTP read near deadline, Retry-After exceeding remaining time, expired auth,
malformed state, local interrupt, and recovery in a new process. Use injected
clock only for scheduling unit tests; process/HTTP tests prove actual bounds.

**Done:** every followed command prints a durable reconnect instruction and
cannot change the operation being observed or its execution deadline.

### W07 — Preserve the complete saved profile during editing

**Touch:** `fleet_profile_contract.py`, `fleet_profile_api.py`,
`fleet_profiles.py` (`_choices`, `_view`, save/update paths),
`_authoring_assignments`, `_profile_save`, `_profile`, and connected web edits.

The current `FleetProfileAssignmentView` lacks some authoring fields,
including desired state and model variant. `_authoring_assignments` also
silently skips malformed data, and `_profile_save` sends assignments/name
without preserving other metadata. A renderer-only fix cannot make this safe.

1. Extract one canonical `FleetProfileDefinition` for authoring fields;
   derive `FleetProfileInput` from it with the existing revision precondition.
   Add proposed `GET /api/profile/{number}/definition` returning identity,
   revision, and that definition. Materialize it from stored canonical choices
   and profile metadata, independently of optional cache/live projections.
   Do not reconstruct desired state from observed state or create a new store.
   An unused number has revision `0` and no persisted identity. Saving with
   `expected_revision: 0` is create-only; an existing profile requires its exact
   current revision. The read projection may also embed the same canonical
   definition for existing web editors; it must derive from the same owner.
2. Read/modify/write this definition. Preserve description, favorite, labels,
   installation policy, assignment name, model variant, and desired state.
   Reject malformed assignments/revision rather than dropping them, sorting
   away invalid values, or substituting an empty profile.
3. Add `profile add --state installed|running` and `profile configure
   --description ... --retention keep-cached|exact --favorite true|false
   --label KEY=VALUE --remove-label KEY`. Omission preserves values; conflicting
   label edits fail. Preserve legal incomplete topology as a saved draft.
4. `profile export` emits only the canonical definition, without observed
   state, credentials, or an execution snapshot. `--output` creates a private
   exclusive file. `profile import --file FILE|- --expected-revision N` validates
   bounded JSON and saves with that explicit live revision. Import never loads;
   exported revision context cannot silently authorize replacement.
5. Keep revision conflicts visible; do not retry a stale whole-definition
   write. Return the saved revision and “Saved; running fleet unchanged.”

**Tests:** real save → definition read → single edit → save → read round trips
in `test_fleet_profile_api.py`, `test_fleet_profiles_canonical.py`, and CLI
tests. Include installed-only state, named variant, false/empty values,
metadata, incomplete draft, unavailable cache, malformed assignment, duplicate
label intent, and concurrent revision conflict. A deliberately omitted
metadata field must cause the regression to fail. Use the canonical generated
schema in the standalone CLI; no handwritten copy of its field allowlist.

**Done:** editing one property changes only that property and revision;
export/import is a definition round trip, never an application operation.

### W08 — Prepare cache with durable submission recovery

**Touch:** `_model`, `_recipe`, `_follow_mutation`, `model_cache.py`,
`model_cache_api.py`, `recipe_image_availability.py`, and its API/contracts.

1. Retain existing download/update actions and same-key semantics. Present
   reuse, resume, transfer/build, verification, publication, and real measured
   progress from the owner. Unknown totals remain unknown.
2. Add canonical lookup by request key where absent: proposed
   `GET /api/model/requests/{request_key}` and
   `GET /api/recipe/requests/{request_key}`. Extend noun progress with
   `--request-key UUID` as an alternative to ID. Require current authenticated
   read authority plus the original issuer for request-key correlation.
   Preserve the existing shared Fleet visibility of reads by operation ID;
   knowledge of another issuer's key must not disclose its binding or permit
   replay. Current mutation authority is required for every POST, including
   a replay. Use the explicit lookup policy below instead of treating a UUID
   as authorization.
3. On ambiguous submission, reconcile by original key. After an authoritative
   not-found, at most a bounded replay of the identical idempotent request is
   allowed. Preserve the key in failure output if the Controller cannot be
   reached. Never convert “unknown” into “safe to start new work.”
4. Show exact missing asset and disk shortfall. Failed refresh preserves the
   last verified artifact; compatible partial bytes survive interruption.
   Use the owning storage implementation; this package does not independently
   move cache authority from SQL to files.

**Tests:** CLI transport response loss plus actual owner tests in
`test_model_cache.py`, `test_model_cache_ranges.py`,
`test_model_cache_availability_regressions.py`, and
`test_recipe_image_availability*.py`. Cover commit-before-response-loss,
same-key different payload, process restart, failed replacement, missing
filesystem object behind stale success, and unrelated work during a busy
artifact. Concurrency/process proof uses PostgreSQL and real storage seams.

**Done:** a restarted CLI can find the original work and prove whether it was
accepted; reuse/resume claims are backed by managed artifacts.

#### W08 implementation sequence

These slices close the existing package; they are not additional milestones.
Keep the matching API, generated consumers, CLI behavior, and meaningful tests
together for each exposed capability. The table records the baseline problems
identified on 2026-09-22 and the required end state. Delivery evidence belongs
to the [implementation status](cli-operator-status.md): original download
replay, single-operation lookup/following, and submission recovery now have
partial checkpoints. Elapsed-time transport enforcement now has real HTTPS,
resolver, and process-interruption evidence. The durable update parent now has
connected CLI, process-death, and PostgreSQL race evidence. W08e now has actual
managed-file, worker-process, and PostgreSQL recovery evidence, including fixes
for busy writers holding execution slots and hidden eligible work. W08's
repository gates are qualified; installed and combined operator qualification
remain W18/W19. No slice implies deployment or physical acceptance.

| Slice | Existing boundary and required change | Evidence needed before closing |
| --- | --- | --- |
| W08a — Original download intent | The baseline resolved selectors and recomputed model previews before checking accepted keys. Persist the canonical original operator request beside its accepted exact bindings. Authenticate and check that request before mutable catalog, disk, or preview work. One owner predicate compares issuer, action, selector, and all action options; both early replay and concurrent insert recovery use it. | Replay returns the original ID after partial progress, a changed catalog head, missing metadata service, or changed free space. Changed action, selector, options, or issuer never returns or creates a second operation. |
| W08b — Lookup and single-operation following | Add the two request lookup routes with the policy below. Progress accepts exactly one ID or request key, and resolves a key once before following its ID. The baseline followed only `operation_id`, silently missing recipe preparation's canonical `id`. Use explicit current response types for each action, including a durable batch response from W08d. A malformed receipt is not detached success. | Real CLI/generated-client/registered-route tests follow model and recipe downloads, reconnect by key, refuse foreign/mismatched bindings, and keep following the first ID when newer work is submitted. |
| W08c — Ambiguous submission | Add a small submission helper with explicit route, action, request body, lookup, and receipt validation. Use the bounded decision table below; retain W06 as the observation owner. Keep `with_model` only on recipe removal; reject it on recipe download and model actions. Preserve qualified model selectors through routing. | Inject failure after durable acceptance but before HTTP delivery. Lookup finds the accepted operation and no second POST occurs. Authoritative absence permits only one identical replay; a failed lookup never generates a new key. A 403, invalid receipt, and Ctrl-C cannot claim unconfirmed acceptance. A real HTTPS slow-body test must prove the elapsed-time deadline, not just timeout arguments passed to a fake transport. |
| W08d — Durable recipe update batch | Replace `update`'s transient loop and response array with one durable parent request and frozen child scope, as specified below. Remove the old synchronous orchestration path and update all current consumers. | Restart before any child, between children, and after a child commits but before the parent records it. The original parent and exact children are recovered once; a later catalog/cache change cannot expand or shrink the accepted batch. |
| W08e — Storage and process qualification | Exercise these receipts through the existing model and recipe storage owners, with actual managed files and PostgreSQL concurrency. Reuse existing resume, replacement, and busy-artifact tests where they already prove the required seam. Add only missing connected failure cases. | Compatible partial bytes resume; failed replacement retains the last verified asset; stale success cannot hide missing files; a busy artifact does not block an unrelated eligible operation. A fresh CLI process reconnects using a caller-retained request key. |

**Replay and visibility decision.** Store original intent in the operation's
canonical typed document and reference its already-owned exact artifact or
recipe binding. Do not create a second idempotency database. Model operations
already have an issuer and operator selector; recipe operator submissions need
their original selector/options recorded separately from the resolved revision.
Internal preparation requests retain their own exact-revision contract.
Project the original operator request on operation responses when it exists,
so the CLI can verify a recovered action and scope without another catalog
lookup. This is a typed field for operator submissions; internal preparation
does not acquire invented operator intent.
Normalization follows the authoritative model's declared defaults; do not
reinterpret a replay through the latest catalog or collapse distinct valid
intent. In particular, force-download and force-rebuild are separate options,
not one interchangeable boolean. Bind all accepted options, and refuse unknown
or inapplicable ones.

Request lookup returns the same canonical operation type as its ID route.
Absent keys, keys owned by another issuer, and keys outside the requested
operation family return the same bounded not-found response. Reusing a key in
a POST with different intent returns a conflict without the other request's
details. Thus a not-found response means no visible binding; it is not proof
the global unique key is free. Any permitted replay still passes the owner's
unique constraint and intent comparison. After a concurrent insert conflict,
roll back before reading and validating the winning request. An in-process
mutex is not cross-worker protection. Preserve the key binding while the
durable operation is retained; define any later pruning policy before making
expired keys reusable.

**Submission decision table.** The CLI does not infer execution from a lost
response. Distinguish receipt validation from operation completion.

| Observed result | CLI decision | May submit again automatically? |
| --- | --- | --- |
| Valid accepted receipt with the expected key/action/scope | Detach with that receipt, or follow its exact canonical operation ID. | No |
| Transport loss or a server-side failure with uncertain acceptance | Perform one authorized lookup of the original key. Retain safe endpoint/status/request-ID evidence. | Only after the not-found case below |
| Lookup returns the expected accepted binding | Continue from its canonical receipt; do not re-preview. | No |
| Lookup returns authoritative not-found for the caller | Replay the identical route, body, and key once within the remaining submission budget. The owner rechecks authority, uniqueness, and intent. | One replay |
| Lookup is unavailable, denied, malformed, or belongs to different intent | Exit with the original key and an exact noun `progress --request-key` reconnect command. State acceptance is unknown when it is unknown. | No |
| Original POST returns a definite authorization, validation, or conflict refusal | Report that refusal and the current authorized next action. | No |
| Success response cannot be validated | Treat acceptance as unknown and use read-only lookup for diagnosis; never use the malformed receipt to follow or justify a replay. | No |
| Local interruption before an accepted receipt is known | Report interrupted submission and retained key; make no remote cancellation request and no claim that work was accepted. | No |

Limit submission recovery to the original POST, one lookup, and at most one
identical replay. Derive its total network budget from these three calls and
the configured per-request timeout, enforce one monotonic deadline, and give
each call at most the smaller of its normal timeout and remaining time. A
second ambiguous POST ends with an unknown outcome and the same reconnect
command. W06's separately documented observation deadline begins after a
validated accepted receipt; neither deadline cancels Controller work or resets
the worker's persisted deadline.

The 2026-09-23 transport probe exposed an implementation gap: a
localhost HTTPS response sent one byte every 50 ms. A 200 ms socket timeout
returned only after 1.108 seconds with a malformed-receipt error. Socket
inactivity is not the required elapsed-time bound. The current transport uses
the already-pinned HTTPX async stream behind the synchronous CLI interface,
with one monotonic cancellation deadline spanning connection and every read.
Generated JSON calls and artifact transfers use the same transport; artifacts
retain their separate transfer budget and bounded streaming.

The implementation follows the documented distinction between
[HTTPX phase/inactivity timeouts](https://www.python-httpx.org/advanced/timeouts/)
and a total [Python cancellation deadline](https://docs.python.org/3/library/asyncio-task.html#timeouts),
using [explicit streamed-response closure](https://www.python-httpx.org/async/#streaming-responses).
System DNS is an additional boundary: cancelling an asyncio task does not stop
the blocking resolver, and waiting for its executor at shutdown exceeded the
deadline in a regression. One daemon resolver per identical in-flight lookup
returns addresses only; it never receives credentials or performs HTTP work.
Cancelled callers discard its answer and do not delay process exit. Completed
answers are not cached here. No new dependency or global socket mutation.

Real HTTPS tests now qualify slow headers/body, received refusal metadata,
connection closure, generated-client reads, certificate rejection, redirects,
streamed verified artifacts, and actual SIGINT. Resolver tests qualify both
late-answer isolation and a CLI process exiting while system DNS remains
stalled. Preserve these gates and the connected acceptance/receipt tests when
changing this boundary: neither interruption nor a late response may authorize
a new key or an extra POST. This qualifies W08c's request boundary; W08d/W08e
still own durable batch orchestration and Controller/storage process recovery.

Generate the key before the first POST. Human mode prints and flushes the key
and reconnect command before submission. Controlled JSON success, failure,
timeout, and interruption each retain it in their one final document. Scripts
that require recovery after unconditional process death must supply and retain
`--request-key` before invocation: a killed JSON process cannot promise to emit
a final receipt. State that limitation in help and test this case with a fresh
CLI process. Do not introduce an implicit local operation-state database to
hide it.

**Recipe update parent decision.** Use the existing PostgreSQL `Job` authority
for a typed recipe-update parent, handled by the existing recipe availability
service and worker integration. Persist the issuer, original request key and
scope, resolved exact recipe revisions, deterministic child request keys, and
per-child admission/progress references before issuing any child effect.
Resolve `--all` as the complete current cached recipe scope, with one accepted
current revision per logical recipe; historical succeeded job rows are not
cache availability. Remove the silent `.limit(100)` truncation. Bound the
serialized request/scope by the actual API/document budget, report the bound
and observed size, and either admit the complete scope or refuse it before
dispatch. No accepted `--all` request may mean an undisclosed subset.

Persist the parent in a short transaction. Outside that transaction, reconcile
each deterministic child key and start missing child intent against the
parent's frozen exact revision. Store its returned ID with a conditional
parent-fence check in another short transaction. A crash between child commit
and parent update is repaired by lookup of that same child key. Persisted
children, not the current result of repeating the original selection query,
determine later observation and replay. Keep metadata refresh, cache scans,
builds, and child completion outside SQL transactions.

The parent uses no transfer/build slot while awaiting its children. Record the
child dependency, owning service, next attempt, persisted deadline, and resume
condition; release its execution claim before waiting. On takeover, reconcile
issued child effects and reject stale parent writes. Validate the dependency
graph before dispatch. A child failure stays attached to that child and does
not suppress unrelated eligible children. Aggregate state derives from their
canonical outcomes; show partial acceptance/failure explicitly. An empty
scope is an explicit successful no-op, and replay preserves that empty scope.
Newly revoked authority prevents fresh child admission, while already-issued
effects remain visible. Batch cancellation and shared-child ownership are
part of W12, including this parent type.

The current update-array response is retired with its callers. The new parent
receipt participates in the recipe operation union, request-key lookup,
renderer, and Activity provider; W14 must paginate or summarize child detail
without losing the complete parent scope. W08 cannot close after adding only
the two lookup routes: original-intent replay, recipe following, and update
batch restart recovery are part of its acceptance gate.

### W09 — Bind Controller admission to the reviewed decision

**Implementation checkpoint:** the canonical reviewed decision has recorded
[W09a evidence](cli-operator-status.md#w09a-reviewed-decision-checkpoint--2026-09-23).
Current source also contains the required numbered-load precondition,
issuer-bound replay, current-user authorization, roster/catalog admission
fences, planner-owned capacity assessments, read-only review, and original-review
stop/removal bounds for retries. See the
[admission and review checkpoint](cli-operator-status.md#w09bw09c-admission-and-review-checkpoint--2026-09-23)
for the connected tests and their limits. Those changes are not evidence that
all admission races are closed. The later
[workload-effect checkpoint](cli-operator-status.md#w09d-workload-effect-checkpoint--2026-09-23)
adds atomic effect comparison and child bounds. Resource reservations, managed
deletion and final connected qualification remain open. Later checkpoints have
verified installed-only execution, durable disk/port/memory handoff and physical
memory pool accounting. Exact build-memory inheritance and declared reserve
preservation now have their own recorded checkpoint. The remaining W09d C–F
boundaries are measured-use and post-stop evidence, shared-build cancellation
and replanning, single-slot progress and the common acquisition order. Follow
those with W09e asset lifetime, W09f rebuilt-image
identity and W09g combined acceptance; do not repeat the completed prerequisite
work or count its tests as evidence for these remaining boundaries.

**Touch:** `FleetProfileLoadRequest`, `FleetProfilePreview`, application
progress/intended contracts, `fleet_profile_api.py`, and `fleet_profiles.py`
(`load`, `preview`, `_queue_application`, `application_by_request_key`). Update
all generated clients and existing load call sites in the same PR.

1. Require `plan_digest` and a client-generated `request_key` in the
   current schema-2 load request. Remove its unused `dry_run` field and active
   callers; dry-run already uses the separate preview route. There is one
   authorized load path, with no old-body fallback.
2. Define a canonical reviewed-decision object used by both digest generation
   and explanation. Bind profile identity/revision/definition, entire enrolled
   scope including idle Sparks, resolved recipe/model/image identities,
   placement/effects, and relevant admission decisions. Exclude display times,
   volatile progress counters, and incidental telemetry changes that do not
   change a safety decision. Recheck real safety conditions at admission.
3. Preserve the distinction between two existing meanings of digest:
   `preview.plan_digest` identifies reconciliation; `_queue_application`
   currently hashes it with request key/retry identity to produce the
   application's `plan_digest`. Store the reviewed digest explicitly in the
   existing canonical intended/progress document and project it with a clear
   name. Do not compare a review digest to an execution-attempt digest or
   redefine the latter in place without updating every consumer.
4. Reconcile an existing request before rereading/re-previewing changed saved
   intent. Authorize the lookup; compare its original profile/request/review
   binding; return that accepted application for an identical replay even
   after a later profile edit. Reject same-key changed intent. Never let replay
   bypass current authority or dispatch a second application.
5. For a fresh request, validate the reviewed decision and atomically admit
   its frozen effects under current coordination rules. The existing node
   subset check is insufficient for a whole-fleet review: concurrent roster
   addition/removal must not silently alter scope. Use an established common
   admission/roster serialization boundary or transactionally checked revision
   owned by the same authority; locking only previously known nodes cannot
   prevent a newly inserted node. No long SQL transaction, external storage
   wait, or artifact-lock acquisition inside SQL.
6. Reconcile asset references/reservations with the storage owner so removal
   after preview cannot admit missing bytes. Do not hash whole cached assets
   on ordinary review. Refuse changed choices or unavailable resources with
   canonical reasons and require a new review before different effects.

**Tests:** actual load route + actual service + PostgreSQL, extending
`test_fleet_profiles.py`, `test_fleet_profiles_canonical.py`, and connected
contract tests. Race profile edit, roster addition/removal, exact recipe head
change, asset loss, and competing reservation between review and admission.
Test stable digest across generated timestamps, identical replay after edit,
changed-body same key, concurrent duplicate requests, and unauthorized replay.
Assert no stop/dispatch occurs on stale admission. Existing internal
stale-preview tests alone do not prove the operator route enforces it.

**Done:** accepted intent binds exactly the reviewed decision; concurrency
cannot admit a different scope or substitute different artifacts. PostgreSQL
evidence and the coordination scanner are mandatory for this package.

#### W09 implementation sequence

These slices close one package. Keep the required load body, generated clients,
CLI submission, and existing web caller together; they cannot be released as
incompatible halves. The 2026-09-23 checkpoint records implementation and tests
for W09a/W09b, capacity/read-only behavior in W09c, and part of W09f. Reuse that
evidence; next complete W09c's effect presentation alongside W09d/W09e, then
qualify the remaining retry and connected boundaries in W09f/W09g.

| Slice | Concrete change and owner | Evidence required before closure |
| --- | --- | --- |
| W09a — One reviewed decision | `FleetProfileReviewedDecision` owns the semantic fingerprint and explanation. Preserve exact assignments, effects and full roster. | A replaced run with the same name/count changes the digest; incidental times/counters do not. Existing checkpoint supplies this evidence. |
| W09b — Admission identity and authority | `load`, `apply`, `_load_replay`, and `_matching_application` require the original digest/key, recover an accepted receipt, and check current authority. The API projects `request_key`. | Real API rejects missing preconditions, changed same-key intent, and revoked authority; concurrent identical requests recover one application after an intervening profile edit. |
| W09c — Complete admission projection | `RunSwitchFleetProfileAdapter` carries the owning planner's admission decisions as well as preparation. Extend the canonical reviewed decision only with facts needed to explain or bind a safety decision. | A planner rejection still blocks the profile when `preparation` is present. Resource headroom, reservation conflicts, and route changes agree between review, explanation and admission. |
| W09d — Atomic control decision | `_admission_session` and `_queue_application` validate the frozen definition, roster, catalog identities, affected workloads, overlapping intent and reservations under a common ordered SQL boundary. | Independent PostgreSQL sessions race insertions, removals, effect replacement and competing admission. One accepted decision reserves its exact scope; the loser is refused before stop/dispatch. |
| W09e — Asset lifetime | Profile admission, `ModelCacheService`, recipe-image removal and collection share the storage plan's durable reference/deletion protocol. | Actual managed removal cannot invalidate a provisional/accepted reference. Worker death, storage loss and unavailable reference scans fail safely and recover without substituting an artifact. |
| W09f — Retry within original consent | Intended configuration refers to the original reviewed application. Remaining-work plans may reuse newly completed work but cannot acquire new effects or choices. | Recovery after partial completion succeeds; a missing/invalid review source, newer intent, changed asset identity, or a new workload outside the original effect set prevents dispatch. |
| W09g — Connected closure | Regenerate all contracts and qualify actual route → service → PostgreSQL/storage → worker boundaries. | All races below, generated checks, lock-order audit and affected regression pass at one recorded revision. W09 remains open until then. |

**W09c detail.** The earlier preparation projection already carried planner
blockers through `preparation.reasons`; omission of the full plan was not proof
that blockers were lost. The remaining projection gap was resource headroom
and the read path's ability to author a build. `RunSwitchAssessment` now owns
the shared planner fields for both Run/Switch and profile review.
`FleetProfileAdmissionDecision` binds semantic demand, eligibility, settings,
stop identities and preparation order, while `assessments` retains observed
capacity outside the fingerprint. The CLI shows both current and after-stop
headroom and the intended endpoint alias. Profile and library inspection use
the existing planner with build creation disabled. Typed recovery may defer
missing source preparation to the accepted worker, while other planner
blockers remain effective. Keep these predicates in the planner rather than
copying them into profile or CLI code.
Demand includes the recipe's declared reserve and existing reservations; no
extra platform reserve or round-number cap may reject an otherwise fitting
recipe. Informational budgets remain warnings unless a real resource is
unavailable. Keep volatile usage observations out of the fingerprint when
they do not change the reviewed action or eligibility, and recheck eligibility
at admission.

**W09d detail.** Inventory all writers before extending the current lock set:
enrollment/revocation, catalog acceptance, profile editing, run and installation
state, resource reservations, inventory, parent cancellation and child result
adoption. The production workload fence is `AgentNode.workload_intent_ordinal`,
inherited by profile, Run/Switch, lifecycle and agent work. `ResourceReservation`
owns run, installation and build capacity claims. Inspection on 2026-09-23
found no production caller of `NodeLeaseService`; the existence of that helper
and `NodeMutationLease` is not evidence of active ownership. Do not introduce a
second scheduler or reactivate that unused path to satisfy a diagram. Extend
the current intent and reservation owners, removing an obsolete path if the
connected ownership change retires it. Every writer affecting a reviewed
predicate must participate in its serialization boundary. New-row insertions
must be covered as well as updates to rows seen during preview.

The current short transaction protects roster/head membership and SQL-owned
workload effects with PostgreSQL table locks, and checks saved definitions and
exact reviewed assignments. `_control_effects` supplies the common projection
for review, admission and worker queues; acceptance compares it before changing
intent. It also fences inventory and reservation writers while the existing
Run/Switch resource predicate rechecks memory/disk eligibility, required ports
and preparation order. Harmless headroom changes are accepted; lost capacity or a changed
stop-before-prepare/transfer decision requires a new review. This recheck
retains typed resource evidence during an authorized missing-image repair and
does not reopen artifact or capability inspection under SQL.
Serving and rendezvous port demand now has one owner in `run_admission.py`.
Fresh-install review, existing-install review, the final profile recheck and
runtime reservation consume it. `ports_required` appears in each typed Spark
fit and the semantic admission decision, and the CLI presents it. Only port
claims owned by an exact reviewed run stop are excluded from the after-stop
assessment. Install-only and logical artifact-job paths require no serving
ports. Accepted profile port ownership and its atomic child handoff are now
implemented as described below. Durable parent memory claims and their exact
runtime handoff are also implemented; measured usage, post-stop evidence and
complete shared-builder recovery remain open.
Cancellation's ordered `Job`/`StoredOperation` rows now use `NOWAIT`.
Retain that simple boundary unless measured contention or a lock-order conflict
justifies changing it. Do not describe it as resource reservation ownership or
protection for storage facts it does not lock. Declare explicit and implicit locks in the architecture's
common order, including cancellation's `Job`/`StoredOperation` rows, foreign
keys and unique request keys. Use nonblocking acquisition and central finite
budgets. A conflict rolls back the whole attempt; no external calls, cache
resolution, storage scan, retry sleep, or child wait belongs inside it.

Re-evaluate the exact affected run/installation/pending-intent set and resource
admission under this boundary. Compare semantic effects to the review, then
persist the application, ownership and resource claims in the same commit.
Children inherit parent ownership. If a change requires an earlier lock,
release the transaction and restart its bounded admission attempt. If a second
same-key caller committed, return that original receipt before reporting a
newly stale preview. Never convert contention into approval of a new plan.

Implement W09d in this order. These are the inspected entry points, not a claim
that the writer audit or common lock boundary is already complete:

| Step | Existing owner to extend | Reviewable output and failing case |
| --- | --- | --- |
| 1. Inventory competing writers | Run/install admission, recipe-build reservation, inventory recording and lifecycle release, listed below | Map each reviewed predicate to every writer and the exact SQL lock set. Include new rows and implicit locks. Real API regressions reproduced lost memory/disk capacity and occupied ports after the final preview; the shared fenced recheck now refuses them. Durable claims and builder admission remain to be joined. |
| 2. Reconcile acquisition order | Those admission owners plus `agent_jobs.py:request_superseded_workload_cancellation_in_session` | One shared ordering with nonblocking acquisition and central wait budgets. The cancellation helper has PostgreSQL evidence for contested `Job` and `StoredOperation` rows; finish acquisition order across intent, capacity and child admission, including separate parent/child transactions. |
| 3. Bind effects and resource claims | `fleet_profiles.py:_admission_session` and `_queue_application`, using the current workload ordinal and `ResourceReservation` owners | Compare frozen run/installation/pending effects and reserve the exact scope in the acceptance commit. Independent PostgreSQL sessions race same-alias replacement, a new installation, and two fitting requests; no unreviewed stop/removal or double capacity claim is accepted. |
| 4. Preserve the boundary at execution | Profile adapter `_plan_queue` and `_start_child`, plus the owning Run/Switch dispatch path | Replanning may reduce remaining work but cannot expand original consent. Race effect replacement and newer intent between acceptance and child admission; no unrelated effect is dispatched, including after restart. |
| 5. Join asset lifetime and review | W09e references and W10 presentation | Complete the review with the actual route/stop/removal/resource decisions from the same owners. Pass acceptance → reference → dispatch tests before closing M3. |

Keep these steps in one coherent admission change with generated consumers and
tests. A wider table lock alone is not proof of reservation ownership, and a
resource claim must not make a parent wait for capacity that only its child can
release. Document the child inheritance and release conditions with the code.

The inspected capacity writers are:

| Owner / entry point | Writes | Required integration |
| --- | --- | --- |
| `run_admission.py:accept_run_in_session` | Runtime memory and service/rendezvous ports | Port and memory handoff are implemented: consume the exact parent's promised rows atomically with run/start-job creation, refusing changed claims or stale intent. Measured-use accounting, post-stop evidence and complete shared-builder recovery remain open. |
| `install_admission.py:accept_install_in_session` | Installation disk | Implemented: transfer the same reviewed claim to the exact installation atomically, without counting it twice. Stable claim identity reconnects a committed installation after checkpoint loss; retain it when the parent finishes. Remaining common lock-order and cancellation races still apply. |
| `recipe_builds.py:reserve_in_session` | Builder disk and host memory | The exact current Run/Switch build child can now borrow its parent's memory promise; independent requests retain the capacity conflict. Shared-consumer cancellation, replanning and the common lock order remain open. |
| `recipe_operations.py:_release` and its completion/cancellation callers | Reservation state and release time | Release only reconciled effects. Retained run/installation capacity must not disappear merely because the profile parent finishes, restarts, or is superseded. |
| `inventory_repository.py:record` | New inventory snapshots | Acceptance reads capacity under the writer fence; later inventory loss is explicit execution/recovery state, never proof that a reservation can be silently removed. |
| Profile acceptance, Run/Switch queueing and lifecycle/agent dispatch | Workload ordinal, child identities and supersession | Pass one exact owner/fence through the existing lineage. Adopt committed children before acquiring new claims; newer intent prevents stale dispatch. |

Before implementing claim inheritance, write the state transition for each
kind: accepted parent claim → child acquisition/adoption → retained runtime or
installation claim → reconciled release. Cover cancellation and supersession
at every transition. Use existing resource-demand owners; do not restate their
arithmetic in the profile adapter. A profile's reviewed after-stop capacity
must not be counted as presently free, and a later independent build must not
consume capacity promised to accepted work. The memory-ownership checkpoint
proves parent promises and exact runtime handoff; it does not prove measured-use
accounting, common lock ordering, builder integration or artifact lifetime.
W09d remains open until those connected races and restart/release cases pass.
Disk, port and memory ownership have the connected evidence described below;
these are prerequisites to the remaining shared-capacity work.

The [disk-ownership checkpoint](cli-operator-status.md#w09d-disk-ownership-checkpoint--2026-09-23)
implements the disk transition in `profile_capacity.py`. Acceptance reserves
each assignment that still needs installation. The installation transaction
validates the parent intent and exact reviewed amount, then changes the same
reservation's owner. A deterministic claim identity reconnects that installation
after a lost preparation checkpoint, before mutable capacity is assessed again.
Terminal application transitions release only still-parent-owned reservations;
installation completion/cancellation remains the installation owner's concern.
Reservation contention reschedules the original child with a visible retry time.
Recovery inspection is read-only; worker advancement is explicit.

Port ownership uses the same reservation ledger with a `promised` state for
accepted profile ports. A live run retains its own active rows until its stop
is reconciled. Separate unique indexes enforce one active owner and one pending
profile owner per node/port. Admission observes both, so an unrelated run cannot
take the port before or after the reviewed stop. The exact child validates its
parent, assignment, alias, ports and workload ordinal, then changes the promised
rows to active run ownership in its start transaction. Restart adopts the same
start request before reassessing its already-owned ports. Parent termination
releases only unassigned claims, and contention reschedules the original child
with `run.capacity_busy`. Fleet counts physical reserved ports once when live
and pending owners overlap. This is a fresh-schema change; no migration path or
deployed acceptance is claimed.

The shared memory decision now lives in `resource_planning.py`:
`memory_requirement` owns the canonical settings demand, memory kind and
system/platform reserve; `memory_capacity_snapshot` selects the corresponding
inventory; `plan_capacity` decides eligibility. Profile review and low-level
runtime admission consume these same owners. The reviewed requirement includes
`memory_kind` and `memory_floor_bytes`, which are visible in the CLI. Run
acceptance reuses its fenced, freshly validated plan instead of maintaining a
second arithmetic check. PostgreSQL boundary tests compare actual profile
review with runtime start and the persisted reservation, including unequal
host/GPU totals and one byte below the required reserve.

Complete the remaining memory/builder ownership in the connected runtime slice:

1. Keep the shared typed requirement as the profile-capacity input; its formula
   remains owned by resource planning. Physical-pool accounting now includes
   durable parent promises as well as builders and runtimes. Complete physical
   usage attribution without charging already observed use a second time.
2. Promises now cover only assignments needing a start; retained runs keep
   their own claims. Exact reviewed replacement envelopes overlap in the ledger
   and preserve ownership after the old claim is released. Finish the distinct
   physical after-stop evidence requirement before calling this boundary closed.
3. Exact memory claims now use the parent's lifecycle/run lineage and transfer
   in the start-job transaction. Preserve original-request adoption before
   mutable admission on restart. Require current physical evidence after a stop
   rather than treating the released reservation as newly free memory.
4. Extend the connected run-ownership cases through shared build preparation,
   cancellation and resource reserve preservation. Then complete the common
   acquisition-order audit across all capacity writers and the corresponding
   concurrency cases. Passing handoff tests alone does not close these steps.
5. Carry the accepted `memory_floor_bytes` through the canonical signed start
   and artifact-job requests and their compiled placement. Validate equality
   between request and placement, generate the Rust wire fields, and have the
   local readiness check use demand plus that exact floor. Keep the container
   limit at workload demand. The current helper instead adds a fixed 4 GB,
   which can reject a reviewed fitting recipe: 120 GB demand plus a declared
   2 GB reserve fits 123 GB free, but fails the helper's 124 GB threshold.
   First reproduce that refusal, then test the exact declared boundary and one
   byte below it, request/placement mismatch, and Controller-to-signed-request
   propagation for both starts and jobs. Run native checks in the Linux lane;
   Controller tests alone cannot close the runtime policy mismatch.

The physical-pool prerequisite is now implemented in the working tree. Inspection of
`rust/crates/vonk-agent/src/inventory.rs:parse_gpus` shows that a GB10 reporting
unavailable dedicated-memory counters is projected from host memory. The
H100 path reports separate accelerator counters. The Rust inventory tests
exercise both cases. Canonical `InventoryRequest.memory_pool` now carries the
required `shared`/`separate` relationship through the native producer, API,
PostgreSQL and admission. Equal byte counts are not evidence of a shared pool;
unequal counts are not evidence of separate pools. The Controller does not
infer it from counts or telemetry labels. Agent inventory and telemetry now
use the same hardware classifier. Unknown/missing pool values and shared
memory without a GPU are refused, rather than defaulted.

The [physical-pool checkpoint](cli-operator-status.md#w09d-physical-memory-pool-checkpoint--2026-09-23)
records A/B evidence. The subsequent
[memory-ownership checkpoint](cli-operator-status.md#w09d-memory-ownership-checkpoint--2026-09-23)
adds parent claims and exact same-row runtime handoff from C/D. Their remaining
physical-usage and post-stop evidence requirements still need implementation;
E's shared cancellation/replanning and F's common acquisition order are also
open. The later build-inheritance checkpoint covers only its named subset.

| Step | Concrete change | Required evidence before moving on |
| --- | --- | --- |
| A. Carry physical pool evidence | Add one required, typed pool relationship to canonical authenticated inventory. The agent derives it alongside its actual memory readings; unknown or unsupported evidence remains explicit. Carry it through the generated wire, agent producer, API, stored inventory and admission view together. Remove constructors/fixtures that omit the current field instead of adding a compatibility default. | Rust collector cases for shared GB10 and separate accelerator memory; a real serialize → API → PostgreSQL → inventory-read round trip; omission and contradictory evidence refused. Run the native producer/wire checks in the Linux lane. |
| B. Share the reservation projection | Extend the existing resource-planning owner with the pool relationship and use it in run, Run/Switch and builder admission. Host and accelerator claims compete when they refer to one physical pool; separate pools retain their independent limits. Keep recipe system reserve and builder demand explicit. | A host-memory build prevents a non-fitting unified run, and a unified run prevents a non-fitting build, in both preview and acceptance. Exact boundary fits; one byte below fails. Separate-memory cases prove that independent capacity is not wrongly combined. |
| C. Reserve future runtime demand | Use the reviewed per-assignment requirement and exact reviewed stop set to persist parent-owned memory claims in the same acceptance commit. Retained runs keep their own claims. Represent replacement ownership without treating promised capacity as already free. | An independently fitting start/build preview becomes inadmissible after profile acceptance. Stop completion cannot open an ownership gap. Existing live work plus its pending replacement is not counted as two independent future workloads. A stale or unrelated run cannot supply stop credit. |
| D. Hand off without reacquisition | Extend the current disk/port parent lineage to validate the exact memory amount, pool, assignment, digest and workload ordinal, then transfer the same claim in the run/start-job transaction. A completed stop does not prove newly available physical bytes; obtain the owner's current evidence before dispatch. | Single- and two-node PostgreSQL cases for exact handoff, changed amount/pool, missing claim, changed intent, contention and restart on either side of commit. The original committed child is adopted before mutable admission. |
| E. Join preparation and build ownership | Follow the existing preparation/build identity and model shared dependants explicitly. A bound builder may use only capacity authorized by that lineage and phase; independent builds still see the parent's promise. Release the temporary build use before runtime handoff, without releasing the durable parent demand. | A profile with one eligible builder and one worker slot completes rather than waiting on its own reservation. Shared-image requests use one build; cancelling one parent does not release another dependant's capacity. A lost build checkpoint reconnects the exact build before acquiring new resources. |
| F. Close acquisition order and presentation | Audit explicit and implicit locks across these writers, including new claim inserts and releases. Reuse finite SQL budgets, nonblocking contention and durable retry reasons. Update the typed review/Fleet views to identify reserved capacity and its current owner. | Opposite-order two-node requests, simultaneous run/build/profile admission and cancellation/dispatch races return within their budgets without overcommit. CLI review and acceptance describe the same physical capacity and required stop. |

The current accounting owner is `memory_reservations.py` plus the pure
`resource_planning.py` predicate. Shared pools count host, accelerator and
unified claims once each. On separate hardware, a unified demand must fit
each independent pool; their reservations are not added together. Both
constraints are reconsidered after a reviewed stop, so a change in the
limiting pool cannot admit an otherwise non-fitting workload. The physical
relationship is included in typed profile review and stored runtime plans;
changing it requires a new reviewed digest. CLI output distinguishes the
recipe's demand kind from that relationship and labels available memory as
the current limiting pool's observation.

The required inventory field is a breaking current contract, delivered as
protocol package 3.0.0 with its exact wheel, lock and container pins. The retired
2.2.0 wheel is removed. It also adds a required fresh-schema inventory column.
Controller and agent artifacts must therefore be published together; no old
inventory reader or database migration path is retained. This is repository
implementation evidence, not a deployment claim.

The byte-count arithmetic and the ownership transition have separate tests:
neither passing boundary tests nor a correct reservation handoff alone proves
both. In particular, a recipe's peak reservation is not a measurement of bytes
freed by stopping it, and an inventory observation must not double-charge
already materialized usage as if it were another independent reservation.
Keep these cases visible while replacing the shared accounting implementation.
The ledger now represents a reviewed replacement as the larger live/future
claim envelope for each physical pool. This is ownership across sequential
phases, not physical-use evidence. `StopImpact.run_plan_digest` binds the exact
runtime behind that overlap; its existing `plan_digest` still identifies the
separate stop action. Both overlap accounting and planned-stop reservation
release use the same exact-run query. A missing, changed or unrelated claim
cannot supply that overlap.

The build-inheritance checkpoint connects the exact accepted preparation and
Run/Switch child request to the parent promise. The claim stays parent-owned
through build completion, and independent requests cannot borrow it. Builder
admission now retains the largest declared reserve of the overlapping current
profile/runtime claims. Cached or active build adoption validates the reviewed
input before bypassing mutable capacity. These close the reproduced admission
and reconnect defects; shared-consumer cancellation, complete replanning and
single-slot worker acceptance still remain in E. Complete the measured-use and
post-stop evidence accounting in C/D with that lineage, then the common writer
order and presentation in F. Do not close W09d until A–F pass together with the
existing disk/port recovery cases.

**W09d E execution checklist.** Keep this work within the existing build,
profile and Run/Switch owners. These are the full E acceptance requirements;
the status checkpoint identifies the subset with current evidence:

| Order | Production boundary | Change and acceptance condition |
| --- | --- | --- |
| E1 — Prove lineage | `profile_capacity.py`, accepted profile review/progress, and canonical Run/Switch plan/result | Resolve the exact build ID/input, builder, assignment and current workload ordinal. Borrow only the claim bound to the current authorized preparation phase. Refuse stale/cancelled parents, changed identities and unrelated builders. A matching recipe name alone supplies no authority. |
| E2 — Adopt before reserving | `run_switch_operations.py:_execute_container_build` and `recipe_operations.py:build` | Recover the original exact build/job before mutable capacity checks. Cover committed child with lost parent checkpoint, already completed child and repeated delivery. Each case retains one request and one build effect. |
| E3 — Account for temporary use | `recipe_builds.py:reserve_in_session`, `memory_reservations.py`, and the shared resource-planning predicate | Retain the parent's promise while its exact build uses the same physical pool. Count sequential phases once, preserve the recipe's required system reserve, and keep other consumers visible. Test exact fit and one byte below, shared and separate pools, and an unrelated simultaneous build. |
| E4 — Align preview and admission | `recipe_builds.py:prepare_plan` and `run_switch_operations.py:_select_build` | Derive exact build identity before deciding whether an existing parent can supply capacity. Preview stays read-only where required; execution revalidates authority and claims. A proposed random build ID must not establish lineage or trigger a second build after restart. |
| E5 — Reconcile release and cancellation | Existing build result/release path and profile/RunSwitch consumer ownership | Completing the temporary build releases its use while preserving the runtime promise. Cancelling or superseding one consumer cannot cancel work still owned by another. Last-consumer cancellation reconciles issued effects before release; a late result cannot revive cancelled intent. Supply this shared-child foundation to W12/W13. |
| E6 — Close the connected boundary | Real profile acceptance → Run/Switch preparation → build admission → result → runtime handoff | Demonstrate progress with one eligible builder and one worker slot, restart on both sides of each commit, and simultaneous cancellation/admission. Busy claims roll back and schedule a visible bounded retry without holding SQL, artifact locks or the child's slot. Join the F lock-order audit before closure. |

Start with a connected regression that reaches the actual container-build
admission and records its canonical refusal or wait reason. A fixture blocked
by a missing source bundle, unrelated preflight failure, or an unobserved
worker phase is not evidence of the inheritance defect. After the fix, verify
that the same parent claim survives build completion and that independent work
still sees it. Do not infer physical free memory from this ledger test; C/D
retain their separate observation and post-stop gates.

##### W09d E5 shared-build cancellation sequence

**Baseline defect and implementation checkpoint, 2026-09-23.**
`RecipeBuild.plan` is read as the strict canonical `RecipeBuildRequest`.
Inspection found direct cancellation and cache removal adding non-contract
fields that made subsequent parsing fail; fresh submission also excluded a
settled cancelled receipt. The build-cancellation checkpoint reproduces these
failures through actual services and PostgreSQL, removes those plan mutations,
and uses the exact lifecycle job's typed cancellation record. Reconciliation,
late-result handling and cleanup now read that same authority. A fresh request
after settled cleanup can execute without reviving the cancelled request;
cancelled replacement work preserves the last verified image. These fixes do
not yet establish shared-consumer cancellation safety.

Run/Switch currently waits for an active preparation child when cancellation
is requested. Initial guards now derive current consumers from accepted
Run/Switch and availability intent and refuse direct shared-build cancellation.
Acceptance and binding use a nonblocking build-row fence. These guards do not
yet prove complete parent detachment or last-consumer reconciliation. The
availability composition now persists a typed child request before dispatch,
recovers an accepted child before mutable planning, handles cancellation
explicitly, and returns pending work to the durable scheduler. A real
availability scheduler with one slot completes two parents sharing one child
in the connected regression. This does not close unresolved-builder adoption,
the full profile-to-runtime pipeline, or process-death acceptance. Changing
only a CLI flag or one cancellation branch cannot close those remaining gaps.

The implementation decisions for this slice are:

- Keep the canonical build request immutable. The lifecycle job's existing
  typed cancellation result owns cancellation of that exact attempt. Update
  producers, reconciliation, late-result handling and cleanup readers together;
  do not add a permissive parser or a second cancellation flag on the plan.
- Derive current consumers from accepted, validated operation intent. Bind
  exact build/input, attempt where relevant, owner and current workload fence.
  Cover Run/Switch/profile preparation and recipe-image availability. Do not
  infer ownership from a matching recipe name or a historical successful job.
- Serialize consumer registration, detachment and last-consumer cancellation
  on the same build ownership boundary. A scan of consumers followed by an
  independent cancellation transaction is insufficient. Declare the complete
  lock order with admission/result writers before adding the fence; restart a
  bounded attempt if an earlier lock is needed. No waiting under SQL or while
  holding a child execution slot.
- Parent cancellation removes that parent's demand; it cannot revoke another
  current consumer. A direct cancellation of the underlying shared build must
  refuse while other authorized consumers remain, with safe dependency detail.
  Unknown or malformed ownership evidence defers that build's cancellation;
  it must neither authorize release nor block unrelated work.
- A settled cancellation remains final for its original request. A new explicit
  request may reuse verified bytes or start a new attempt after prior effects
  are reconciled. Cancelling a replacement build preserves the last verified
  image unless a separate authorized removal owns its deletion.

Implement the following connected slices in order, closing E4's original-child
adoption before E5f's worker acceptance. E5 supplies ownership to W12/W13; it
does not depend on those later public command/API additions.

| Slice | Owners changed together | Acceptance condition |
| --- | --- | --- |
| E5a — Keep the build request valid | `_cancel_build`, `_project_node_result`, `reconcile_cancelled_builds`, `_release_cancelled_build`, and cache-removal cancellation | Use the existing typed job cancellation record for issued attempts. Re-read the canonical build request after direct cancellation, removal and restart. A planned build with no issued job remains valid; removal is fenced by its owning removal/reference intent rather than invented execution history. |
| E5b — Permit new intent after settled cancellation | `RecipeBuildService.persist_plan_in_session`, lifecycle `build` and original-request lookup | Replaying the cancelled request returns its original outcome. A new key can create a new authorized attempt only after the prior issued effect is reconciled. Preserve the last verified receipt during cancelled force-rebuild; generic retry cannot revive cancelled intent. |
| E5c — Register and detach exact consumers | Run/Switch acceptance/adoption, availability child binding, and lifecycle build admission | A second current consumer joins the exact child durably before relying on it. Cancel one parent and prove the other still owns the work and capacity. Recover a committed child even if its parent checkpoint was lost. Reuse existing typed references; extend their canonical contract only where the exact relationship is missing. |
| E5d — Fence the last consumer | Consumer registration/detachment and lifecycle cancellation under the common owner | Race a new accepted consumer against last-consumer cancellation using independent PostgreSQL sessions. Either it joins before cancellation, or it observes the cancelling child and waits/reviews as required. It cannot attach to a child whose capacity has already been released. |
| E5e — Reconcile late effects | Agent result projection, cleanup receipts, build reservation release and original parent progress | An unissued child settles without remote cleanup. An issued child retains capacity until exact cleanup evidence arrives. Late success, duplicate cleanup and process restart cannot publish cancelled output, release a newer attempt's claim, or remove another consumer's promise. |
| E5f — Resume without occupying the child slot | Availability composition and Run/Switch advancement through existing dependency waits | All child terminal states, including cancellation, reach an explicit outcome. Persist the exact dependency and next observation, release the parent executor/transaction, and resume the same intent. With one eligible builder and one worker slot, the child and surviving parent complete. This is also E6 evidence. |

Use these regression cases to close E5, extending the real existing service
tests rather than constructing a separate fake cancellation engine:

| Scenario | Required result | Existing test seam |
| --- | --- | --- |
| Cancel before agent claim | Valid stored request; original job cancelled; only its reconciled claims released | `test_recipe_builds.py` |
| Cancel an issued build and lose the cleanup response | Durable cancelling intent; capacity retained; same cleanup identity recovered | `test_recipe_builds.py`, actual lifecycle/result services |
| New request after cancellation | Old request stays cancelled; new accepted attempt uses a new request identity | `test_recipe_builds.py` through actual plan persistence and admission |
| Cancel a force-rebuild | Prior verified image remains usable; cancelled replacement cannot supersede it | Build service plus actual managed image receipt |
| Two accepted consumers, one cancelled | Exact child and remaining consumer continue; no shared reservation released | `test_profile_build_memory.py` joined to actual availability ownership |
| Last cancellation races new consumer | One serialized result, no orphan consumer, duplicate build or ownership gap | Independent PostgreSQL sessions with deterministic barriers |
| Parent dies between child commit and checkpoint | Reconnect before cancellation/replanning; do not mistake a missing checkpoint for no child | Run/Switch and availability production-path tests |
| Old result arrives after a new attempt | Old result is fenced; new claims and receipt are unchanged | Actual agent-result and cleanup projection |
| Parent executor loses its claim | Late progress, success and failure cannot overwrite the new owner or its exact child dependency; an expired or superseded claim cannot renew itself or dispatch a new child | Availability service and production builder checkpoints cover the callbacks, lease, binding and actual child-admission transaction; the broader profile/RunSwitch writer audit remains |
| Cancellation races explicit removal | Distinct intents; referenced assets protected; unavailable scans defer removal | Recipe availability plus W09e reference/removal tests |
| One worker slot and builder | Parent waits without retaining resources required by its child; surviving work eventually completes | Actual worker composition with PostgreSQL and managed storage |

For each bug fix, first observe the failure through the named owner, then rerun
the same case after the change. Record which wrong implementation it rejects.
Use controlled barriers for races, not timing guesses. Canonical parsing tests
are necessary but do not prove shared ownership or eventual recovery. E5 closes
only after the consumer races, cleanup/restart and one-slot cases also pass;
W09e still owns the complete asset deletion protocol.
The cancellation/removal race is a joint E5/W09e acceptance case: close it when
the reference protocol is present and rerun it at W09g. It does not require
W09e to be complete before starting E5's canonical cancellation and consumer
ownership changes.

###### Shared-build closure checklist

Continue from the initial guard implementation in
`recipe_build_cancellation.py`; do not add another consumer registry merely
to make counting easier. Accepted canonical plans and runtime references own
the relationship. This checklist breaks E4/E5/E6 into executable changes;
it is not a new package or an additional release gate.

1. **Finish the existing guard's failure paths.** In
   `recipe_image_availability.py`, translate contention on both the build row
   and lifecycle-job row into the owning typed retryable refusal. Roll back
   all removal/acceptance changes on contention. Classify ownership contention
   and pending cleanup as dependency waits where appropriate, with a visible
   next attempt; they must not consume transfer/build failure budgets.
   Validate runtime identity through the canonical contract instead of
   replacing malformed data with an empty object. Exercise held build and job
   rows separately, then release each and prove the same intended work resumes.
2. **Cover every writer of an accepted dependency.** Audit Run/Switch apply,
   retry and adoption, availability start/retry/builder selection, and accepted
   profile applications whose Run/Switch child is not materialized yet. A
   parent either publishes its exact dependency under the same build fence or
   has no authority to rely on that child. Unresolved builder selection must
   acquire the fence before binding. Pass the exact execution claim through
   production builder recovery, selection, binding and post-dispatch checkpoint
   writes; an operation ID alone does not prove that executor still owns the
   current attempt. Reuse the availability service's owner/attempt/lease check
   instead of defining a weaker second predicate. An explicit identity mismatch is a
   refusal, not evidence that no matching build exists. Include cancellation
   between acceptance and child dispatch; no cancelled parent may create work.
   Profile acceptance now locks every changing assignment's exact existing
   build under this fence, including an image that is ready at admission but
   later needs cache recovery. Consumer discovery includes the accepted profile
   until its deterministic Run/Switch child commits; dispatch and recovery
   derive that key from one helper.
   `RecipeLifecyclePhaseExecutor._execute_container_build` now passes the
   current Run/Switch parent guard into lifecycle build admission. That guard
   checks the accepted actor/request, canonical plan, workload ordinal, phase,
   child checkpoint and cancellation. It locks the target/builder nodes and
   parent nonblockingly through child commit. The same predicate fences
   post-dispatch progress and late child observations/failure writes. This
   closes the tested build dispatch/checkpoint gap; it does not establish
   independent producer ownership or last-consumer cleanup in step 3.
3. **Separate producer intent from shared consumption.** Identify whether a
   build was independently requested or created solely for accepted parents.
   A parent that merely adopted an independent build cannot cancel its
   producer's intent. Parent cancellation removes only its own demand; it
   preserves the child's execution and claims while any current owner remains.
   Last-owner cancellation uses the existing lifecycle cancellation service,
   under the same serialization protocol as registration. Test two parents,
   an independent producer, newer workload intent, malformed ownership, and a
   new consumer arriving on either side of the cancellation boundary.
4. **Recover the exact child before planning another.** In
   `availability_production.py`, `run_switch_operations.py`, and the build
   service, recover accepted child identity before evaluating new capacity or
   generating a plan. A global input-only request key cannot distinguish a
   new explicit request from replay of a cancelled request. Derive producer
   identity from durable intent, retain it through retries, and adopt a shared
   child by its exact reference. Repeated observation retains the child key;
   recovery after a settled failed child or missing archive creates a new
   execution identity under the same authorized parent. A cancelled child
   requires fresh explicit intent. Test response loss after child commit, parent
   checkpoint loss, completed-child adoption, and fresh intent after settled
   cancellation. Each yields one execution for its accepted effect.
5. **Return waiting parents to the scheduler.** Replace the blocking child
   polling loop with the existing durable dependency-wait mechanism. Persist
   exact child identity, owner, next observation, applicable deadline and resume
   condition. Account explicitly for success, failure, cancellation, lost
   observation and cleanup still in progress. Release SQL, artifact locks and
   execution slots before waiting. A real one-slot composition must complete
   its child and surviving parent, including a restart while waiting.
6. **Qualify the connected result and close the slice.** Audit the common
   acquisition order with F across acceptance, binding, cancellation, removal,
   result adoption and cleanup. Use independent PostgreSQL sessions and
   controlled process barriers for the races. Run affected profile, build,
   availability, Run/Switch and cancellation tests together, then applicable
   static, contract, worker and supply-chain checks. Record the exact working
   revision and remaining W09e joint case. A cancellation refusal alone does
   not close E5, W12 or W13.

The initial `test_build_consumer_ownership.py` scenarios are regression
foundations: direct cancellation while availability/profile demand remains,
and acceptance against a held build row. Expand them at the real service seam
for the cases above. Passing those initial cases does not establish automatic
parent cancellation, absence of deadlock, or eventual completion.

The [shared-build guard checkpoint](cli-operator-status.md#w09d-shared-build-guard-checkpoint--2026-09-23)
records passing evidence for the contention/retry-budget paths in step 1 and
explicit identity/malformed-cleanup refusals. The
[availability observation checkpoint](cli-operator-status.md#w09d-availability-build-observation-checkpoint--2026-09-23)
adds evidence for the bound-child portion of step 4 and availability-worker
portion of step 5, including prior-image reuse and stale failure fencing.
The [availability claim checkpoint](cli-operator-status.md#w09d-availability-claim-fencing-checkpoint--2026-09-23)
extends the service fence to progress, model waiting, receipt authorization,
success, renewal and delayed claim delivery. The
[builder claim checkpoint](cli-operator-status.md#w09d-production-builder-claim-checkpoint--2026-09-23)
then carries the same claim through production recovery, selection, binding
and checkpoint writes. Planning runs outside SQL; persistence checks the claim
inside its transaction. The lifecycle service checks the parent in the actual
child-admission transaction, closing the check/dispatch gap. The
[profile dependency checkpoint](cli-operator-status.md#w09d-profile-build-dependency-checkpoint--2026-09-23)
adds accepted profile demand before child dispatch, the shared build fence at
profile acceptance, and committed-child discovery before a lost checkpoint.
The [Run/Switch build fence checkpoint](cli-operator-status.md#w09d-runswitch-build-fence-checkpoint--2026-09-23)
adds transactional child admission and current-checkpoint guards for build
observations, including cancellation/newer-intent races and an admission commit
barrier. These cases do not establish the last-consumer cancellation protocol,
all non-build phase writers or the full common-lock-order audit.
The [cancellation and unbound adoption checkpoint](cli-operator-status.md#w09d-shared-build-cancellation-and-unbound-adoption-checkpoint--2026-09-23)
adds independent producer intent, parent detachment, last-consumer cleanup,
new-consumer races, fair progress past malformed history, and unresolved
availability adoption before fresh capacity admission. Its one-slot scheduler
cases reuse the same exact execution under saturated or stale inventory.
Continue with the remaining writer and preview/admission identity audit in
steps 2–4 and the
complete pipeline/process-death cases in step 5. The final combined gate in
step 6 still requires those changes; these passing regressions do not close
E4/E5/E6 or W09 on their own.

The [profile build-to-runtime process checkpoint](cli-operator-status.md#w09d-profile-build-to-runtime-process-checkpoint--2026-09-23)
adds six real subprocess/PostgreSQL cases for a first installation: abrupt death
before and after the build and start commits, plus completed-child adoption
while the parent is down. One synchronous worker reaches route publication
without duplicate lifecycle work and preserves the runtime memory promise.
Managed image files and preparation are exercised; Spark/model-transfer and
publication adapters remain simulated. Continue the combined cancellation,
remaining commit-boundary and F lock-order gates before closing E6.

The [restored-image binding checkpoint](cli-operator-status.md#w09f-restored-image-binding-checkpoint--2026-09-23)
fixes the accepted-reuse case: rebuilding the same image preserves the existing
installation and its compiled plan, without creating a new disk claim. Image
identity comes from the original approved review, including when an automatic
retry's remaining-work plan has no available image receipt. The worker checks
completed builds and prepared receipts against that identity before advancing.
Changed image/archive output is refused with named changed fields and a request
to review again. Four subprocess regressions failed before these fixes, and the
existing cache-retry regression also exposed the required original-review lookup.

Continue W09f with the **new approval** path after changed output: the new review
must accurately show replacement installation, resource claims, any running
workload and route effects, and then execute exactly that decision. Qualify
already-cached replacement, restored published images, reference lifetime and
concurrent replacement at the remaining worker boundaries. The current refusal
and same-image recovery evidence do not close those gates or W09f as a whole.

The [new image approval checkpoint](cli-operator-status.md#w09f-new-image-approval-checkpoint--2026-09-23)
now covers the first-installation and installed-only paths after a changed
source rebuild. Reuse checks the immutable image in every compiled rank, and
review/admission share disk arithmetic using the actual rebuilt archive size.
The accepted claim is handed to a replacement installation and the approved
image reaches the agent's start payload and published route. The September 24
integration checkpoint additionally exercises replacement of an already-running
workload through a separate worker process with PostgreSQL and managed storage.
Those cases retain both archives, cover both a replaced receipt and distinct
old/new build records, and use deterministic Spark receipts and inventory.
A changed-recency case also preserves the exact image after acceptance instead
of silently reducing dispatch to a no-op. Published-image recovery and the
reference/concurrency gates below remain open.

Next, make these remaining cases concrete before changing their predicates:

1. Retain the September 24 running-workload and distinct-build regressions in
   the combined gate. Profile state, preparation selection, disk reservation
   and Run/Switch installation reuse must choose the same reviewed identity.
   Verify exact stop/route replacement effects and post-stop capacity evidence;
   the installed-only subprocess cases do not prove those effects.
2. Cover missing **published** image receipts through the production preparation
   callback. A known registry reference without an available archive is not a
   newly observed different image. Preserve the original approved output while
   restoring bytes, then compare the complete actual receipt before execution.
   Inspect the partial-observation predicate in `_preview_run` as part of this
   gate; do not fill absent observed values with successful verification claims.
3. Race replacement/removal and newer workload intent with these transitions,
   using the common ownership/reference protocol from W09d/W09e. Keep source,
   managed-cache, publication and physical qualification evidence separate.

**W09e detail.** Implement one reference protocol across admission and removal
before considering the CLI load workflow qualified:

1. In a short SQL transaction, reserve the exact model/image references for
   the current request and fence. A deletion reservation for the same identity
   excludes this acquisition. The reservation is control ownership, not an
   assertion that files exist or are verified.
2. Release SQL, then ask managed storage to attest the exact receipt and
   presence/type/length of the immutable objects. Keep only a bounded snapshot;
   ordinary admission does not rehash all model weights.
3. In the final admission transaction, recheck current authority, unchanged
   reservation fences and the complete reviewed control decision. Convert the
   provisional references to accepted operation references atomically with
   the application and resource claims. Any refusal releases provisional
   claims through their owner.
4. Removal/collection uses the same authority to acquire an exclusive deletion
   reservation before touching bytes. It must not cross an accepted operation
   reference. Reference-read failure defers removal. After SQL is released,
   acquire at most one artifact lock nonblockingly and recheck the deletion
   fence using only the permitted short, nonblocking SQL path.
5. Restart recovery reconciles provisional claims and actual effects under
   their request/fence. Lease expiry alone cannot authorize a stale worker to
   publish or a collector to delete. No transaction, artifact lock or execution
   slot remains held while waiting for another owner.

Recipe removal must create its durable owner before deletion. Persist a typed,
normalized immutable intent containing action, submitted selector, exact resolved
revision and effective `with_model`; bind caller and request key through the
existing Job owner. Reconcile an existing key before resolving today's mutable
selector. Identical replay observes the original owner even after its catalog
head disappears; changed caller, selector or model-removal choice conflicts
before effects. POST and both operation/request reads project the same stored
intent and current stage, including required `with_model`. A finished image
unlink cannot imply success while model-removal children remain unsettled.

Join these regressions to the resumable remover: replay after catalog-head loss;
changed actor/selector/choice without effects; owner-scoped request lookup; and
process death after image unlink and between model-child removals. Recovery must
retain the original owner/fence and choice without duplicate releases or false
success. An identity-only handler change does not close the shared removal gate.

Use the existing storage-cutover owner if this protocol has landed there;
otherwise implement the connected reference/removal slice in W09 and update
that plan. W17 adds the operator removal review on top. Deferring the shared
protocol until W17 would create a dependency cycle and leave M3 unsafe.

Managed deletion and uncoordinated disk loss are different boundaries. A pin
can prevent cooperative removal; it cannot prevent a disk disappearing.
Recheck exact prerequisites before the first consequential worker effect and
before publication. On loss, wait/repair within the original authorization or
report the precise blocker. Never claim SQL and filesystem existence were
committed atomically, and never replace a bound image digest with a rebuilt one.

**W09f detail.** Preserve the original review document while any retry refers
to it. Validate its profile/issuer/scope and exact assignments, then prove the
new remaining-work effects are a subset of that authorized intent. Newly
completed transfers may reduce work. A new unrelated run, revoked grant,
newer request, or altered identity cannot expand it. Keep the review reference
direct so recovery does not require an unbounded ancestor walk. New deliberate
work receives a new request identity and review.

The current implementation checks that retry stop/removal effects remain a
subset of the original review, both during queue admission and persisted-intent
consumption. First queue planning and fresh/adopted Run/Switch children also
enforce the accepted destructive effects. Tests refuse changed effects at load
acceptance, after acceptance and when an assignment's child is re-planned.
Resource ownership across execution, retention of every referenced root and
exact rebuilt-image admission remain part of W09d–W09g closure.

**Installed-only closure.** The workload-effect checkpoint reproduced false
success for an uninstalled assignment with `desired_state=installed`. Deliver
an explicit install-without-start action through the existing Run/Switch
planner, execution phases and final verification. Bind it into the profile
queue, child identity/adoption and result contracts; regenerate all current
consumers together. Prove actual installation receipts and no runtime-start or
route publication, including recovery after the installation child commits.
An already-running assignment requested as installed must stop the reviewed
runtime while retaining its installation. Resource assessment must reflect
installation/preparation demand, without imposing serving-only memory or port
requirements. This is required behavior for M3, not an optional later feature.

The 2026-09-23 installed-intent checkpoint implements this slice: `install`
shares the Run/Switch owner, the profile queue carries its exact child kind,
and a canonical installation-verification receipt records every installed
rank and the absence of active runs or unwithdrawn routes. Restart tests cover
both child-commit/parent-checkpoint gaps using PostgreSQL, including children
that finish before the parent resumes. See the status record for the tested
revision boundary and limitations. This closes the reproduced empty-queue
false success; it does not close the resource/reference/dispatch gates below.

Use deterministic barriers around the real boundaries, not sleeps that hope
to hit a race:

| Boundary changed after review | Required outcome |
| --- | --- |
| Saved profile, enrolled roster, or accepted recipe head | Fresh submission refuses; an identical already-accepted request still resolves to its receipt under current authority. |
| Existing run replaced by a different run with the same alias/count | Refuse the stale effect set; do not stop the replacement. |
| Concurrent fitting requests compete for the same capacity | Claims serialize; no overcommit from two independent previews. |
| Managed model/image removal before reference acquisition | Admission refuses or waits on the named deletion owner; no dispatch. |
| Managed removal after reference acquisition | Removal defers/refuses under the shared reference policy; exact referenced bytes remain usable. |
| Disk disappears after acceptance | Worker reports exact loss before dependent effects; bounded authorized recovery preserves intent. |
| Current user authority revoked or reduced | No new admission or dispatch using stale authority; reads follow the current authorized lookup policy. |
| Retry follows completed transfer or a partial stop | Reuse completed work; retain original consent; never target a newly introduced workload. |
| Admission/collector/worker dies at a handoff | Reconcile the durable request and fence; release abandoned claims safely; unrelated eligible work continues. |

### W10 — Present and submit the exact reviewed effects

**Touch:** `_profile`, `_submit_profile_load`, preview renderer, parser,
`control/web/src/api/client.ts`, its types/call sites as needed, and CLI tests.

1. Preserve `profile load --dry-run`. Render complete whole-fleet effects:
   unchanged/start/stop/install/remove, idle nodes, atomic placement members,
   exact assets, cache blockers, target reuse, headroom, and endpoint changes.
   A blocked preview exits 2 and sends no load request.
2. Interactive load previews, shows effects, confirms once, and submits that
   exact digest. On a stale-plan refusal, display changed reasons and require
   new review; never auto-accept a refreshed plan.
3. Noninteractive load requires `--expected-plan DIGEST --yes`, plus explicit
   `--profile N`. `--yes` alone cannot approve an unseen freshly generated
   decision. Preserve `--detach`, observation options, and request-key replay.
4. Register the request identity before mutation. A lost response follows
   W09's existing-request reconciliation. JSON uncertainty contains the key
   and recovery command without adding a second output document.
5. Update the existing web consumer to pass the same required precondition
   and handle refusal; this is contract maintenance, not a web redesign.

**Tests:** command → real HTTP route → actual service integration, plus a
small PTY interaction test. Cover reject/accept once, JSON and pipe no-input,
`--yes` missing digest, stale review, detached receipt, lost response after
commit, and a later profile edit during recovery. Assert exact submitted digest
and absence of a second dispatch. Regenerate and validate the web client.

**Done:** a human and a script can review, submit, and reconnect to the same
authorized application, with no alternate unreviewed load path.

#### W10 implementation sequence

Current source has the consent flags, bounded original-key recovery and the
minimal web request update. Finish and validate these slices against W09;
their presence in the parser is not the M3 acceptance gate.

1. **Complete the review.** Extend the existing preview renderer with W09c's
   owner-projected headroom, blockers and route changes. Lead with selected
   profile, affected/idle Sparks, starts/stops/removals, then exact assets and
   one next action. Keep full IDs copyable and human text safe at narrow widths.
2. **Close the interaction matrix.** Interactive preview goes to stderr,
   followed by one default-no confirmation; only the accepted receipt/final
   result goes to stdout. Dry-run never prompts or submits. JSON, redirected
   input and `--no-input` require the explicit reviewed digest plus `--yes`.
   EOF, refusal or invalid consent flags create no remote intent. A stale
   review ends with the refusal and a command for a new review; it does not
   silently preview and submit again.
3. **Close uncertain submission.** Allocate one UUID after consent and before
   POST. Validate receipt request key, original review digest and application
   ID. On a lost answer, query the original request; only a canonical not-found
   result and an eligible transport failure may permit a bounded identical
   replay. Denied lookup, malformed receipt or an unavailable answer does not
   authorize fresh work. Show the original key and reconnect command even when
   no application ID was received. Detach means accepted, not completed.
4. **Demonstrate both modes.** Exercise a real PTY for yes/no/EOF and a packaged
   process for JSON/pipe/no-input. Drive a real HTTP route and service for lost
   POST response, concurrent saved-profile edit, authority revocation and stale
   admission. Confirm the existing web caller sends the same required digest
   and never automatically approves a refreshed plan. Complete the runbook and
   installed checks after the new contract is generated.

The intended scripted sequence is the same operator review with explicit
consent. These are target examples; use the current runbook for released
commands:

```bash
# Inspect this exact decision before approving its digest.
vonkctl --profile 2 --json profile load --dry-run > reviewed-plan.json

# REVIEWED_DIGEST is the full plan_digest from that reviewed document.
vonkctl --profile 2 --json profile load \
  --expected-plan "$REVIEWED_DIGEST" --yes --detach

# ORIGINAL_REQUEST_KEY comes from the accepted or uncertain-submission receipt.
vonkctl --profile 2 profile progress \
  --request-key "$ORIGINAL_REQUEST_KEY" --follow
```

Run the first command successfully and inspect its `allowed` state, blockers
and effects before setting `REVIEWED_DIGEST`. Do not pipe a freshly generated
plan directly into unconditional approval in the operator examples. A script
may apply its own explicit approval policy, but the Controller still enforces
current authority and the exact reviewed decision.

### W11 — Cancel model work while preserving usable assets

**Touch:** `model_cache_api.py`, `model_cache.py`, canonical operation
contracts/authorization, `_model`, and storage/worker boundary tests.

1. Add proposed `POST /api/model/operations/{id}/cancel` and
   `model cancel ID --yes`. Accept an idempotent cancellation request identity
   and reason under the operation's current authorization.
2. Record cancellation durably before responding. Fence stale publication,
   signal local executors only as an acceleration, and reconcile already
   issued work. Show `cancelling` until effects/ownership have settled.
3. Preserve complete verified assets and resumable managed partials. Cancel
   the logical request; preserve any work legitimately shared by other intent.
   An explicit later download is new intent and may reuse those bytes.
4. Keep eviction separate. `_remove_model_content` currently cancels work and
   removes cache; it is not an implementation of cancel-only semantics.

**Tests:** cancellation during transfer, verification, and publication;
worker restart; stale worker result after cancellation; repeated cancellation;
shared artifact consumer; and later explicit reuse. Use real process/storage
and PostgreSQL boundaries for fencing. Despite its name,
`test_model_cache_cancel_api.py` currently exercises removal behavior; retain
that coverage and add distinct cancellation tests.

**Done:** cancellation cannot delete a verified object, resurrect the request,
or falsely report settled work while a stale executor can still publish.

### W12 — Cancel recipe preparation without breaking shared work

**Touch:** `recipe_image_availability.py`, its API/contracts, owning child
operations, `_recipe`, and recipe availability/cancellation tests.

1. Add proposed `POST /api/recipe/operations/{id}/cancel` and
   `recipe cancel ID --yes` with the same durable acknowledgement policy as
   W11, implemented through this operation's own authority.
2. Track parent/child intent and exact shared artifact consumers. Cancel only
   children exclusively owned by the cancelled request; do not stop a shared
   image build or model download still required by another authorized request.
3. Preserve verified archives, compatible build/download checkpoints, and
   fenced ownership. Reconcile the child before final cancellation. A parent
   releases slots while waiting and its children inherit node ownership.
4. Include W08's recipe-update parent. Cancellation prevents admission of its
   remaining children and reconciles already-admitted ones under the same
   shared-consumer rules. A restarted parent cannot resume dispatch after
   cancellation or report the whole batch cancelled while an exclusive child
   can still publish.

**Tests:** two requests share one artifact; cancel one during build or model
download; duplicate cancellation; parent crash after child commit; stale
publication; cache removal racing cancellation; unchanged unrelated request;
and update-parent cancellation before, during, and after partial child admission.
Extend `test_recipe_image_availability.py`, its API tests, and real child
operation seams; mocked child success is insufficient.

**Done:** the requested intent stops eventually, while other authorized
consumers and last verified assets remain usable.

### W13 — Cancel a profile application with truthful partial effects

**Touch:** profile API/contracts, `fleet_profiles.py`, switch adapter,
`recipe_operations.py`, child ownership, and `_profile`.

1. Add proposed `POST /api/profile/applications/{id}/cancel` and
   `--profile N profile cancel ID --yes`. Check that the selected application
   belongs to that profile and that the actor may cancel it.
2. Integrate with existing superseded-intent cancellation and issued-effect
   reconciliation. Do not create another cancellation engine or auto-rollback
   to an older profile. Report completed, pending, and cancelled effects.
3. Persist cancellation; prevent new dispatch; reconcile children and fence
   old results. Do not revive the application via recovery after restart.
   Newer authorized profile intent takes precedence over older cancellation.
4. Finalize only after remaining effects and leases are reconciled. Surface
   owner/dependency/deadline while waiting, without retaining a worker slot
   needed by its own child.

**Tests:** extend `test_fleet_profile_recovery_current.py`,
`test_fleet_profile_recovery.py`, `test_profile_child_crash_recovery.py`, and
recipe cancellation tests with real PostgreSQL/process boundaries. Cover
cancel-before-dispatch, cancel-after-one-target-stop, committed child before
parent checkpoint, worker death, late result, duplicate cancel, and newer load.

**Done:** output describes actual effects; cancellation remains authoritative
across process death and cannot dispatch or restore obsolete desired state.

### W14 — Expose actionable activity and authorized recovery

**Touch:** `operation_api.py`, `operation_contract.py`, `operation_progress.py`,
`jobs.py`, activity route registration, `_fleet`, and renderers.

1. Add `fleet activity` backed by one canonical authorized aggregation of
   operation/job/audit references, with deterministic ordering, target/request
   filters, and an opaque continuation cursor. Do not concatenate the first
   pages of three independent lists and call the result complete.
2. Keep exact owner kind and ID with each reference and derive the reconnect
   command from it. Show malformed historical entries as bounded unavailable
   evidence without letting one row hide unrelated eligible current work.
3. Retain bounded `fleet loginfo`. Lead failure detail with task, blocker,
   next safe action, and structured diagnostic context; no raw credential
   bodies or token-bearing URLs. Distinguish refusal, integrity failure,
   transient wait, observation loss, partial effects, and final failure.
4. Add `fleet resume JOB_ID --yes` only when the job owner projects a current
   authorized resume action. The resume route rechecks eligibility and current
   intent. Client flags cannot revive cancelled/superseded work or convert a
   new action into an old-request replay.

**Tests:** operation API/progress tests and CLI tests for pagination across
equal timestamps, denied references, one malformed history row, old versus
new intent, resume action disappearing before submission, and server refusal.

**Done:** a new shell can discover work and its safe recovery path without a
local receipt cache, manual ID guessing, or generic unqualified retry.

### W15 — Return only authorized published endpoints

**Touch:** endpoint contracts/routes, profile read projection, `ControlClient`
`endpoint`, `_profile`, and route/runtime tests.

1. Add `--profile N profile endpoint [ALIAS]`. Project the association from
   saved/loaded assignment to current endpoint alias through the Controller.
   Use one scoped `/api/profile/{number}/endpoints?alias=...` response to bind
   membership and endpoint ownership together. Without alias, list this
   profile's assignments; with alias, validate membership in that same read.
   Recheck loaded application, exact run and route generation before returning
   a usable endpoint, closing the separate-check/use race.
2. Show API base, client model identifier, route generation, freshness, and
   expiry when supplied. Print credential-free usage examples. Do not infer
   an endpoint from node IP, recipe name, successful install, or cached assets.
3. Distinguish installed-only, not published yet, expired, withdrawn, and
   denied. An unavailable endpoint is not replaced with another route.

**Tests:** route publication, generation replacement, revocation, expiry,
alias belonging to another profile, installed-only assignment, and absent
endpoint after application success. Extend `test_route_runtime.py`, route
tests, profile projections, and actual endpoint-client tests.

**Done:** a displayed endpoint is a verified authorized route; usable
inference remains a separate deployed/physical acceptance check.

### W16 — Complete the artifact-job workflow

**Touch:** `artifact_job_api.py`, `artifact_jobs.py`, canonical job contracts,
`ControlClient.upload_file`/`download_file`, `_recipe`, and job tests.

1. Add `recipe job list --run ID`, `create --run ID --file INPUT_JSON`,
   `submit ID`, `detail ID [--follow]`, `cancel ID --yes`, and
   `download ID --output DIRECTORY`. Add `upload ID --file INPUT_JSON` as an
   explicit way to resume input delivery to an existing draft. Creation
   allocates a draft, uploads/finalizes its inputs, and never submits it.
2. Define one bounded CLI input-file binding format: canonical create payload
   plus local named input paths. Validate it against capabilities and the
   canonical create contract; derive sizes/digests from opened regular files.
   Paths stay local and are not another server wire schema. Reject changed
   files, duplicate names, unsupported inputs, and capability byte limits.
3. Preserve caller-generated UUIDs via the existing `x-request-id` boundary:
   `api.py` accepts valid UUIDs and create uses `request.state.request_id` for
   idempotence. Validate UUIDs locally rather than rely on its invalid-header
   replacement. Declare the idempotency-bearing header in the owning API
   contracts and reject an invalid supplied key at those mutation boundaries;
   a direct API caller must not silently receive a new intent identity. Give
   each semantic create/submit/cancel action its own stable key; a retry keeps
   that key. Add authorized lookup if needed to recover an accepted create
   before an ID was received.
4. Display the draft identity before upload in human mode and include it in
   single-document failure output. Resume only missing/unfinalized inputs
   through actual job state. A failed upload must not cause a second draft or
   automatically submit incomplete input.
5. Use existing streaming verification helpers and finalize route. Downloads
   validate result metadata, size/digest and destination paths; prevent path
   traversal, symlinks and unintended overwrite. Publish each verified file
   atomically, report partial collections honestly, and allow verified reuse
   on a later download. Do not promise HTTP byte-range resume without support.
6. Use the existing artifact-job cancellation authority, preserving its
   cancelling/settled distinction and reason requirement.

**Tests:** `test_artifact_jobs.py`, `test_artifact_job_api.py`,
`test_control_client_requests.py`, and CLI process tests. Exercise accepted
create with lost response, interrupted upload, resumed existing draft,
capability limit, changed local file, submit replay, remote cancellation,
malicious result filename, checksum mismatch, disk failure, and existing
destination. Test actual byte routes and service, not only mocked JSON calls.

**Done:** a non-chat recipe yields verified output files with a recoverable
draft/execution identity at every interruption point.

### W17 — Make cleanup and maintenance scope explicit

**Touch:** cache removal routes/services, `_fleet`/`_model`/`_recipe`,
`FleetUpgradeRequest`, `agent_upgrades.py`, CLI update code, and owning tests.

**Entry condition:** W09's shared reference/deletion protocol already protects
admission. This package adds reviewed removal impact and maintenance behavior;
it must not introduce a second collector or postpone that prerequisite.

1. Show cache-removal impact from its owning policy: exact assets, saved
   references/readiness, active work, and `--keep-model` versus `--with-model`.
   Add a canonical impact projection if existing responses cannot support
   review. Recheck authoritative references at mutation; stale inspection
   cannot authorize newly consequential deletion. Cancellation commands must
   remain separate from eviction.
2. Fail closed on unavailable reference scans. Preserve the distinction
   between explicit authorized eviction and automatic garbage collection.
   Do not infer permission to stop a workload or erase profiles from eviction.
3. Keep the one signed Controller-authorized upgrade command. Remove active
   `all-at-once` parser, request literals, service branches, web callers, and
   fixtures together; retain `--strategy one-at-a-time`. Reject retired input,
   do not silently coerce it. No candidate/apply/plan-digest upgrade tree.
4. Follow the exact fleet job, show one affected Spark at a time and any
   rollout stop condition. Keep `fleet upgrade`, Controller-host deployment,
   and `update [--apply]` clearly separate. Preserve signed CLI update
   verification and explicit origin/channel behavior.
5. Bind removal receipts to the submitted action, exact target, actor, request
   key and recipe model-retention choice. Persist the canonical removal intent
   before effects, compare it on replay, and project the stored choice through
   the response contract. The integrated replay correction now stores a strict
   current intent and binds selector, actor, key, revision and `with_model` to
   its Job envelope. It refuses changed or malformed replays before mutable
   catalog resolution; the API projects the stored choice and the CLI checks
   it before following. Those deterministic replay/receipt tests pass. The
   durable remover now persists its Job before effects, arbitrates simultaneous
   first submissions, and resumes exact checkpoints after process death or
   contention. The remaining review increment must bind its accepted review
   digest as well; do not manufacture receipt proof by echoing an incoming
   request.

**Tests:** reference appears between inspection/removal, failed scan,
keep-model semantics, active transfer, denied eviction, two-Spark sequential
upgrade, failure prevents consequential advance, replay, retired strategy
rejection, and invalid signed update. Extend `test_agent_upgrades.py`, cache
tests, `test_cli_update.py`, and connected contract tests.

**Done:** removal cannot broaden its reviewed effect, upgrades follow the
standing sequential rule, and no SSH or unsigned fallback exists.

### W18 — Qualify the installed CLI and finish current documentation

**Touch:** package/entry-point configuration, completion assets,
`docs/runbooks/vonkctl.md`, documentation index, focused CLI process tests,
and existing CI lanes.

1. Test the built wheel's `vonkctl` executable in the standalone environment,
   not only `main()` inside the Controller environment. Confirm no accidental
   Controller/Pydantic dependency and that schemas/completion assets ship.
2. Exercise TTY and redirected modes at 60/80/120 columns, `TERM=dumb`,
   Unicode, no-color, JSON, EOF, Ctrl-C, and closed pipes. Keep a small number
   of meaningful PTY tests; pure formatting checks need no real terminal.
3. Check documented existing examples against the parser without executing
   mutations. Remove proposed labels only after the matching capability ships.
   Include bootstrap, request-key recovery, explicit profile scope, import
   replacement, deadlines, cancellation, endpoints, jobs, and maintenance.
4. Generate completion from the packaged parser. Document manual shell setup;
   no automatic configuration edits. Ensure help remains available offline.
5. Run focused regression, required static/type checks, contract regeneration,
   appropriate fast/lane tests, and release verification below. Record real
   unavailable inputs; do not replace a missing lane with synthetic success.

**Done:** the installed artifact and current runbook implement the same
interface, and every changed boundary has evidence appropriate to it.

### W19 — Run connected acceptance and define the web handoff

Use actual registered routes and service ownership in a disposable Controller
with PostgreSQL and managed storage. A deterministic test executor may isolate
control-plane behavior, but its result is not physical Spark acceptance.

| Scenario | Injected boundary | Required observation |
| --- | --- | --- |
| First connection | Missing/expired/denied token; then valid token | Offline help works; safe diagnosis; authorized Fleet read |
| Find and prepare | Candidate beyond first page; asset absent | Exact choice, actionable blocker, durable cache operation |
| Preserve authoring | Metadata/installed state/variant; concurrent edit | Single-field edit preserves rest; stale save refused |
| Review and load | Definition, roster, artifact, or reservation changes | Stale decision refused before dispatch; new review required |
| Ambiguous submit | Commit succeeds and response is lost | Same-key lookup returns exactly one original application |
| Recipe update batch | Parent/child response loss and restart; cached scope changes | Original parent and frozen complete scope survive; each child is adopted once; no silent subset or expanded replay |
| Reconnect | CLI death, Controller restart, newer application | Original work and progress retained; observer never switches |
| Recover storage | Worker death or missing/corrupt partial object | Bounded recovery, valid assets reused, no false ready state |
| Cancel | Issued child, process death, shared artifact, late result | Durable cancellation; truthful effects; other intent survives |
| Serving result | Publication, revocation, and route generation change | Only current authorized endpoint displayed |
| Artifact result | Interrupted input/output and invalid digest | Existing draft resumed; only verified files published |
| Maintenance | New reference during removal; first upgrade fails | Refusal/review when scope changes; sequential safe stopping |
| Automation | JSON, redirected input, closed pipe, timeout | One result; correct exit; no prompt or remote cancellation |

Run a short operator walkthrough without implementation help: configure access,
find a usable candidate, explain one blocker, save a profile, explain its
effects, load and reconnect, distinguish observer loss from remote failure,
and retrieve an endpoint or artifact. Record task completion, wrong-target
attempts, unclear terms, and commands that require manual JSON inspection.
Fix dangerous ambiguity and blocked core tasks before handoff; record purely
cosmetic refinements separately. Do not claim a usability study until performed.

Hand off the command-to-route inventory, canonical review and progress examples,
error/next-action vocabulary, authorization cases, scenario evidence, and
remaining bootstrap/deployment/hardware limitations. The web then consumes
these same contracts; it does not reimplement scheduling or admission.

**Done:** all required CLI/Controller gates have recorded evidence, every
deployed or physical claim has its own matching evidence, and no unresolved
core task is concealed by “CLI complete.”

## 5. API change ledger

This ledger records the target contract changes from the plan's baseline.
"New" describes that baseline difference, not current delivery status; consult
the [implementation status](cli-operator-status.md) for what has shipped in the
working tree and what remains. Final paths and operation IDs are registered in
the full OpenAPI and generated together; no hidden routes.

| Capability | Canonical owner and transport | Required semantic closure |
| --- | --- | --- |
| Lossless profile definition | W07; new `GET /api/profile/{number}/definition` | Derived canonical authoring definition and live revision; no new store |
| Exact reviewed load | W09; existing preview/load routes | Required review digest and request key; remove unused body `dry_run`; atomic admission and replay |
| Installed-only intent | W09; existing profile and Run/Switch owners | Explicit `install` action and profile child kind; exact installed membership verification, no serving start, same-child adoption after checkpoint loss |
| Exact profile observation | W06; existing application-ID/request-key routes | Verify selected profile membership; follow ID after one latest lookup |
| Cache request reconciliation | W08; new noun `/requests/{request_key}` reads | Authorized original intent lookup; same-key payload conflict |
| Recipe update receipt | W08; existing update route and recipe operation union | Durable parent, frozen complete scope, child request identities, restart recovery; replace the transient update array |
| Cache cancel | W11/W12; new noun `/operations/{id}/cancel` | Durable intent, shared-work ownership, stale-result fencing, preserved artifacts |
| Profile cancel | W13; new `/api/profile/applications/{id}/cancel` | Existing child/intent reconciliation; no rollback promise |
| Fleet fit/readiness | W05; existing library list/detail routes | Named authoritative assessments and unknown/unavailable state |
| Selection ambiguity | W05; typed validation problem on existing read/mutation routes | Complete canonical candidates separate from bounded detail; exact identity priority and no mutation on ambiguity |
| Fleet activity/resume | W14; aggregate read plus existing job resume | Complete paginated view and owner-advertised/rechecked resume |
| Profile endpoints | W15; profile links plus existing endpoint GET | Published authorized route, exact generation, membership and expiry |
| Artifact jobs | W16; existing create/upload/finalize/submit/cancel/result routes | Stable request headers/lookup; verified file lifecycle |
| Enrollment delivery | W04; existing issuance plus status/revoke if missing | Safe one-time delivery and unambiguous post-issuance failure |
| Removal impact | W17; owning cache API | Exact effects with authoritative recheck; unavailable scans refuse |
| Fleet upgrade strategy | W17; existing fleet upgrade/job routes | One-at-a-time current path only; receipt identifies actual job |

Every mutation/lookup needs allowed/denied tests and current authorization at
the service boundary. An unguessable UUID is not an authorization check.

## 6. Validation commands and evidence levels

These are implementation instructions, not a claim they have all run for this
documentation change. Choose focused tests for each package, then the required
broader gate at a coherent milestone. Do not run both test trees in one pytest
invocation. Preserve the root environment's independence from the Controller.

Set the real sibling library path for all applicable checks:

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
```

### Focused CLI regression

```bash
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-root-cache \
  uv run --python 3.14 --frozen --with pytest==9.1.1 \
  --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" \
  pytest -q tests/cluster_profiles/test_controller_cli.py \
  tests/cluster_profiles/test_control_client_requests.py \
  tests/cluster_profiles/test_error_reporting.py \
  tests/cluster_profiles/test_availability_presentation.py \
  tests/cluster_profiles/test_cli_update.py -m "not lane"
```

Use each package's owning control test files in a separate invocation:

```bash
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-control-cache \
  uv run --project control --frozen --with-editable . pytest -q \
  control/tests/test_fleet_profile_api.py \
  control/tests/test_fleet_profiles_canonical.py \
  control/tests/test_cli_controller_parity_acceptance.py -m "not lane"
```

This example is a fast selection, not the W09 admission or cancellation gate.
Run their real PostgreSQL/process cases in the lane environment without the
`not lane` filter. Ensure newly added tests carry the appropriate marker or
use the PostgreSQL fixture that applies it automatically.

### Generated contracts and boundary checks

For changed API contracts, install the generator's pinned tooling and regenerate:

```bash
npm ci --prefix tools/openapi-client
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-control-cache \
  scripts/generate-control-clients
python3 control/tests/coordination_boundaries.py
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-control-cache \
  uv run --project control --frozen --with-editable . pytest -q \
  control/tests/test_api_contract_completeness.py \
  control/tests/test_api_contract_graph.py \
  control/tests/test_generated_contract_roundtrip.py -m "not lane"
```

`generate-control-clients` has `--schema-only`, but no `--check` option. Commit
the generated changes with their producers/consumers. Rerun generation in a
clean checkout and require no diff in `control/openapi.json`,
`schemas/control-openapi.json`,
`src/cluster_profiles/schemas/control-openapi.json`,
`src/cluster_profiles/generated_control/`, and
`control/web/src/api/generated.d.ts`. Do not treat the intended uncommitted
generated diff as a failure or edit generated fields manually.

### Required implementation checks

```bash
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-root-cache uv run --frozen ruff check .
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-root-cache uv run --frozen ruff format --check .
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-control-cache uv sync --project control --frozen
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-root-cache scripts/check-python-types
npm ci --prefix control/web
npm run build --prefix control/web
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-control-cache \
  uv run --project control --frozen --with-editable . \
  python scripts/generate-agent-wire --check
git diff --check
```

Use root-pinned Ruff 0.16.1; the control environment cannot satisfy that pin.
Do not expand a reviewed type/coordination baseline to conceal a new violation.
Regenerate supply-chain evidence last when curated inputs change using
`scripts/verify-supply-chain --generate`, then verify with
`scripts/verify-supply-chain --json`. A no-op on uncurated changes is correct.

### Milestone regression and real lanes

```bash
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-root-cache \
  uv run --python 3.14 --frozen --with pytest==9.1.1 \
  --with pytest-xdist==3.8.0 \
  --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" \
  pytest -q tests -m "not lane" -n auto
UV_CACHE_DIR=/private/tmp/vonk-forge-cli-plan-control-cache \
  uv run --project control --frozen --with-editable . \
  pytest -q control/tests -m "not lane" -n auto --dist loadfile
```

On macOS, inspect `docker context show` and `docker info` and select the
intended OrbStack engine explicitly before container-backed tests. Run
PostgreSQL/concurrency/process, Compose, and Linux-only wire/agent behavior in
their appropriate OrbStack or designated CI lane; omitting the lane filter is
necessary but not sufficient without that environment. For Compose, use the
control environment and short `TMPDIR=/tmp/vk` as documented in the repository
guide. Run the actual NAS/lifecycle acceptance harness only with its required
candidate, Controller, Compose, and acceptance inputs.

| Gate | Evidence to record | What it establishes |
| --- | --- | --- |
| Repository | Exact commit, focused/fast checks, contract generation, process tests | Source and packaged CLI behavior at tested boundaries |
| Concurrency/storage | PostgreSQL, managed storage, process-death and race results | Admission, recovery, cancellation and coordination semantics |
| CI/package | Required CI, immutable wheel/image identities, signatures, accepted generation where applicable | Published artifact integrity and reproducibility |
| Controller | Deployed version/configuration and real API journey | Behavior of that actual deployment |
| Physical Spark | Exact platform/library/recipe identities, serving or artifact execution, failure/recovery evidence | Real hardware execution for the tested scope |
| Operator walkthrough | Participants/tasks, failures, corrections | Usability of the tested journey |

Record each gate as pending, passed, or blocked with evidence. Unit tests do not
establish physical readiness; a published image does not establish deployment.
Do not deploy or manipulate live Sparks merely to fill a planning checklist.

## 7. Traceability to established interfaces

The design's primary-source comparison validates patterns, not implemented
Vonk behavior. These links tie those patterns to concrete packages and checks.
The referenced documentation was rechecked on 2026-09-23. This was a
documentation comparison, not a hands-on benchmark of the referenced tools.

| Reference | Applied packages | Validation |
| --- | --- | --- |
| [Command Line Interface Guidelines](https://clig.dev/) | W01–W04, W18 | Offline help; useful errors; composable streams; explicit noninteractive behavior |
| [GitHub CLI formatting](https://cli.github.com/manual/gh_help_formatting) and [environment](https://cli.github.com/manual/gh_help_environment) | W01/W02 | Human/JSON separation, terminal controls, redirected output |
| [GitHub CLI watch](https://cli.github.com/manual/gh_run_watch) and [exit codes](https://cli.github.com/manual/gh_help_exit-codes) | W01/W06/W14 | Named durable work, outcome-aware waits, documented Vonk-specific exits |
| [Terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan) and [workflow](https://developer.hashicorp.com/terraform/cli/run) | W09/W10 | Reviewed effects bind exact submission; stale decision refuses |
| [kubectl wait](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_wait/) | W06 | Explicit condition and bounded observation distinct from execution |
| [Kubernetes paginated lists](https://kubernetes.io/docs/reference/using-api/api-concepts/#retrieving-large-results-sets-in-chunks) | W05 | Pages must describe a consistent collection. Vonk refuses continuation after the matching collection changes; it does not retain historical list snapshots. |
| [Docker attach](https://docs.docker.com/reference/cli/docker/container/attach/) | W06/W11–W13 | Observation/signal/cancellation semantics are explicit; Vonk Ctrl-C is local |
| [Tailscale CLI](https://tailscale.com/docs/reference/tailscale-cli) | W03/W05 | Readable device state with structured diagnostics; reachability is not workload readiness |
| [GitHub CLI completion](https://cli.github.com/manual/gh_completion) | W03/W18 | Offline shell-specific completion generated from the installed surface |
| [AWS Builders' Library: safe retries](https://aws.amazon.com/builders-library/making-retries-safe-with-idempotent-APIs/) | W04/W08/W09/W10/W16 | Bind caller-provided identity to original intent, atomically record acceptance, refuse changed parameters, and reconcile late or lost responses. Vonk uses the existing durable operation owner; it does not copy another service's retention window. |

Do not copy exit numbers, authentication choices, infrastructure, generic
query languages, or signal-forwarding behavior merely because another CLI
uses them. The repository's security, ownership, and recovery rules govern
Vonk's implementation.

Three differences deserve explicit acceptance coverage: GitHub's `run watch`
uses an optional `--exit-status` flag, whereas Vonk awaits success by default;
Docker attach may forward signals, whereas Vonk observation must not cancel
remote work; Tailscale warns that its status JSON format can change, whereas
Vonk's output must follow its generated current contract. These are deliberate
Vonk decisions, not properties established by adopting another tool's syntax.

The JSON failure stream is another deliberate choice. The CLI Guidelines'
ordinary diagnostic convention is stderr; Vonk human mode follows it. In JSON
mode, Vonk instead returns one structured failure document on stdout with a
nonzero exit, so callers have one declared result channel. W01/W18 must verify
that parser, transport and remote failures all obey that contract, with no
duplicate prose. Neither the source comparison nor a documentation review
substitutes for those process tests or the W19 operator walkthrough.

## 8. Starting implementation and reporting progress

For a fresh implementation, start with W00, then W01. For the current run,
continue from the next open dependency in the status record; do not repeat
completed packages merely because they appear first in this plan.
Track each package with its owner, scoped branch/PR,
dependencies, changed canonical contracts, regression that fails before the
fix, relevant checks, and outstanding evidence gates. Refresh the baseline
against current `main` before implementation; this plan is anchored to the
commit above and concurrent storage work may have changed its seams.

Do not batch all twenty packages into one long-lived rewrite. Close a coherent
vertical package and its generated consumers before moving to the next. Report
progress in terms of usable workflows and evidence, not number of new commands.
Any discovered missing authorization/admission capability belongs in its named
Controller package, with the dependent command withheld until that boundary
exists. After W19, use the shared contracts and tested scenarios to scope the
web design.

### Work record for each package

Use the existing [status record](cli-operator-status.md) for progress and link
the scoped change from it. Each package needs the following information before
it can be marked verified:

| Field | Required content |
| --- | --- |
| Outcome and owner | The operator task being completed and one accountable implementer/reviewer; assign actual people when scheduling. |
| Starting evidence | Exact source revision, dependency states, existing behavior, and any carried-forward failure. |
| Scope | Parser leaves, production owners, canonical contracts, generated consumers, documentation, and meaningful tests changed together. |
| Regression | The wrong behavior the check detects and its observed failure before a bug fix; for a new capability, the concrete boundary being proved. |
| Demonstration | Human invocation, script/JSON invocation, one representative failure, and recovery to the original intent where applicable. |
| Validation | Environment, exact commands, pass/fail results, and which gate each result establishes. Redact credentials from retained evidence. |
| Release impact | Whether curated build inputs or fresh schema changed; required generated artifacts; publication/deployment implications. |
| Remaining work | Explicit unresolved cases, owning package, and whether they block this package or a later milestone. |

Implementation, verification, publication, and deployment are distinct states.
A patch or green unit test does not close a package whose acceptance requires a
real PostgreSQL race or process restart. An unrelated pre-existing failure is
recorded with evidence rather than silently repaired or counted as a pass.

### Decisions that must close before their dependent change

| Decision | Owner | Required reviewable result |
| --- | --- | --- |
| Full roster admission and asset reservation | W09 | Name the existing serialization owner, lock order, frozen decision fields, and reference protocol. Demonstrate simultaneous roster change and asset removal are refused or serialized before dispatch. |
| Shared-child cancellation | W11–W13 | Name the intent owner, consumer references, stale-attempt fence, and terminal condition for each domain. Demonstrate one cancelled consumer cannot stop another's work. |
| Recovery after an uncertain response | W04/W08/W10/W16 | Declare the original request identity, authorized lookup, identical-replay rule, and operator action when status remains unknown. One accepted effect must not produce two operations. |
| Terminal and file data handling | W02/W04/W16 | Separate human escaping, canonical machine data, deliberate secret delivery, and verified file publication. Demonstrate malformed input, path replacement, and interrupted output at the owning seam. |
| Concurrent storage work | W05/W08/W09/W11/W12/W17 | Recheck the storage plan's implemented owner at the package's base revision. Update its connected readers and tests together; never introduce dual authority to work around a changed seam. |

These are bounded implementation decisions, not reasons to reopen the product
design or introduce a new framework. If a decision changes visible scope,
update its package and contract ledger before exposing the command.

### Operator walkthrough and acceptance scorecard

Use the [facilitator protocol and U1–U8 scorecard](cli-operator-walkthrough.md)
to prepare and record the independent walkthrough. Its current status is unrun.

Run this at M5 using a disposable environment and the packaged executable.
Include a regular operator and a CLI-literate operator unfamiliar with the
implementation where available. Give participants the shipped help/runbook and
task outcomes, not a prewritten command sequence. An implementer's own demo
proves execution; it is not evidence of unassisted discovery.

| Task | Observable pass condition | Packages |
| --- | --- | --- |
| U1 — Orient and connect | Find help offline, identify a deliberately invalid connection, then reach a named authorized fleet read without exposing a credential. | W01/W03/W04 |
| U2 — Choose a runnable candidate | Distinguish fleet fit from cache readiness, find a later-page candidate, and state the exact next action for a missing asset. | W02/W05/W08 |
| U3 — Save intent | Create/edit/export/import a profile with installed-only state and metadata; explain why saving has not changed the running fleet. | W07 |
| U4 — Review and load | Identify affected and idle Sparks, accept the shown effects, then handle a stale review without executing different effects. Repeat with noninteractive flags. | W09/W10 |
| U5 — Leave and reconnect | Interrupt local following, reconnect in a fresh shell to the same identity, and distinguish lost observation from remote failure. | W06/W14 |
| U6 — Recover or cancel | Explain a durable blocker, use only the advertised authorized action, and verify cancellation preserves usable assets and reports partial effects. | W11–W14 |
| U7 — Use results | Find a current serving endpoint and separately retrieve verified artifact-job files; distinguish unavailable output from an empty successful result. | W15/W16 |
| U8 — Maintain and automate | Understand cleanup scope and one-at-a-time upgrades; use JSON in a pipe with predictable exits and no prompt. | W01/W17/W18 |

For each task record completion without hints, documentation lookups, wrong
commands, unclear terms, manual JSON inspection required, and elapsed operator
time. Separate operator time from model download/build/execution time. These
measurements diagnose friction; they do not impose arbitrary transfer or
execution deadlines.

All core tasks must be independently completable from the shipped interface
and documentation. Any wrong-target mutation, secret exposure, silent state
loss, duplicate execution, misleading success, or unreviewed effect blocks the
handoff. A failure to discover or complete a core task also blocks it. Retest
the affected task after correction. Cosmetic spacing or optional convenience
improvements may remain recorded follow-ups. A small walkthrough supplies
qualitative usability evidence, not a statistical claim about all operators.

### Final CLI handoff decision

The handoff requires closed W00–W19 CLI/Controller acceptance gates: current
contracts, connected recovery/concurrency evidence, installed executable checks,
and the operator walkthrough. Retain exact revisions, sanitized transcripts,
review/progress/error examples, and the command-to-route inventory so the web
work starts from demonstrated behavior.

Publication, deployed Controller behavior, and physical Spark acceptance retain
their separate gates in section 6. Record missing external inputs explicitly;
the web may be designed against verified Controller contracts without claiming
that an untested hardware run succeeded. Every deployment or physical execution
claim still requires its own matching evidence. This is the completion rule for
CLI-first delivery; cosmetic perfection is not an open-ended prerequisite.
