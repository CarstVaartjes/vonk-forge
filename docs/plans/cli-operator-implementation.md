# CLI operator experience: detailed implementation plan

Status: implementation authorized and underway. Package evidence and remaining
work are tracked in the [implementation status](cli-operator-status.md).

Prepared 2026-09-22 against repository commit
`49389f3dced50d2ecf7680e256dbe1827817eaee`. This expands the
[CLI operator experience design](cli-operator-experience.md) into executable
work packages. That document owns the product rationale and example output;
this document owns delivery order, code boundaries, contract changes, and
acceptance evidence. Proposed commands below are not current operator guidance.

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
| W12 | Recipe-operation cancellation | W08 | P5 | Cancelling shared work needed by another request |
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

**Tests:** `test_availability_presentation.py`, CLI/error tests, and a few
targeted width fixtures. Exercise ANSI/OSC injection, wide Unicode names,
long exact selectors, unknown totals, stale telemetry, and real zero values.
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
5. Resolve exact names/IDs/accepted selectors. Ambiguity returns full
   candidates and mutates nothing. A title match never overrides canonical
   identity, and selection cannot silently choose a different model variant.

**Tests:** `test_fleet_projection.py`, `test_operator_projection_api.py`,
`test_library_canonical_projection.py`, `test_library_sort_recency.py`, and
CLI tests. Include a candidate on page two, cyclic cursor, changed catalog
head, same title twice, one two-Spark placement, missing assets, stale capacity,
and unavailable projection owner. Reuse policy tests rather than restating
admission conditions in CLI fixtures.

**Done:** one can choose a candidate and understand its precise blocker from
human output; JSON retains exact authoritative evidence and page scope.

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
   `--request-key UUID` as an alternative to ID. Authorize lookup exactly as
   the operation, returning no cross-principal or cross-intent information.
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

### W09 — Bind Controller admission to the reviewed decision

**Touch:** `FleetProfileLoadRequest`, `FleetProfilePreview`, application
progress/intended contracts, `fleet_profile_api.py`, and `fleet_profiles.py`
(`load`, `preview`, `_queue_application`, `application_by_request_key`). Update
all generated clients and existing load call sites in the same PR.

1. Require `expected_plan_digest` and a client-generated `request_key` in the
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

**Tests:** two requests share one artifact; cancel one during build or model
download; duplicate cancellation; parent crash after child commit; stale
publication; cache removal racing cancellation; unchanged unrelated request.
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
   Without alias, list this profile's published assignments; with alias,
   validate membership before using `/api/endpoints/{alias}`.
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

All entries are proposed unless marked existing. Final paths and operation IDs
are registered in the full OpenAPI and generated together; no hidden routes.

| Capability | Canonical owner and transport | Required semantic closure |
| --- | --- | --- |
| Lossless profile definition | W07; new `GET /api/profile/{number}/definition` | Derived canonical authoring definition and live revision; no new store |
| Exact reviewed load | W09; existing preview/load routes | Required review digest and request key; remove unused body `dry_run`; atomic admission and replay |
| Exact profile observation | W06; existing application-ID/request-key routes | Verify selected profile membership; follow ID after one latest lookup |
| Cache request reconciliation | W08; new noun `/requests/{request_key}` reads | Authorized original intent lookup; same-key payload conflict |
| Cache cancel | W11/W12; new noun `/operations/{id}/cancel` | Durable intent, shared-work ownership, stale-result fencing, preserved artifacts |
| Profile cancel | W13; new `/api/profile/applications/{id}/cancel` | Existing child/intent reconciliation; no rollback promise |
| Fleet fit/readiness | W05; existing library list/detail routes | Named authoritative assessments and unknown/unavailable state |
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

| Reference | Applied packages | Validation |
| --- | --- | --- |
| [Command Line Interface Guidelines](https://clig.dev/) | W01–W04, W18 | Offline help; useful errors; composable streams; explicit noninteractive behavior |
| [GitHub CLI formatting](https://cli.github.com/manual/gh_help_formatting) and [environment](https://cli.github.com/manual/gh_help_environment) | W01/W02 | Human/JSON separation, terminal controls, redirected output |
| [GitHub CLI watch](https://cli.github.com/manual/gh_run_watch) and [exit codes](https://cli.github.com/manual/gh_help_exit-codes) | W01/W06/W14 | Named durable work, outcome-aware waits, documented Vonk-specific exits |
| [Terraform plan](https://developer.hashicorp.com/terraform/cli/commands/plan) and [workflow](https://developer.hashicorp.com/terraform/cli/run) | W09/W10 | Reviewed effects bind exact submission; stale decision refuses |
| [kubectl wait](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_wait/) | W06 | Explicit condition and bounded observation distinct from execution |
| [Docker attach](https://docs.docker.com/reference/cli/docker/container/attach/) | W06/W11–W13 | Observation/signal/cancellation semantics are explicit; Vonk Ctrl-C is local |
| [Tailscale CLI](https://tailscale.com/docs/reference/tailscale-cli) | W03/W05 | Readable device state with structured diagnostics; reachability is not workload readiness |
| [GitHub CLI completion](https://cli.github.com/manual/gh_completion) | W03/W18 | Offline shell-specific completion generated from the installed surface |

Do not copy exit numbers, authentication choices, infrastructure, generic
query languages, or signal-forwarding behavior merely because another CLI
uses them. The repository's security, ownership, and recovery rules govern
Vonk's implementation.

## 8. Starting implementation and reporting progress

Start with W00, then W01. Track each package with its owner, scoped branch/PR,
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
