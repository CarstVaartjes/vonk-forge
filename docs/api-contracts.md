# Contracts across Python, Rust, and clients

The rule is **strict structure, extensible content**. Each shared document has
one authoritative definition. Consumers must preserve its meaning, not just
accept a similar-looking dictionary.

This is a greenfield contract. Do not retain old field aliases, alternate
document parsers, old-response fallbacks, positional compatibility constructors,
or default values that conceal malformed input. Fix the producer and consumer
together. Transport retries and partial progress updates remain supported by
their current contracts; neither requires accepting an older document format.
Declared optional fields and defaults are part of the current contract, not
legacy compatibility.

## Ownership

The complete inventory of data classes, their source models and their generated
artifacts is [data-contracts.md](data-contracts.md).

| Document | Authoritative definition | Consumers |
| --- | --- | --- |
| Published Model and Recipe | `vonk_forge_contracts.ModelDefinition` and `RecipeDefinition`, in `vonk-forge-recipes/contracts/src` | Catalog importer, Controller, compiler, authoring tools |
| Controller API requests and responses | Controller Pydantic request/response models, including the `*_contract.py` modules | FastAPI, generated OpenAPI, web and CLI clients |
| Controller–Spark messages | Shared `agent_protocol` wire contract | Controller and Rust `vonk-agent-protocol` |
| Compiled artifact-job contract | `CompiledArtifactContract` in `compiled_artifact_contract.py` | Compiler, stored job, runtime handoff, artifact-job API |
| Lifecycle operation results | `RecipeLifecycleResult` in `recipe_lifecycle_contract.py`, composed from shared protocol evidence | Lifecycle producers, stored results and recipe API |
| Model-cache operation results | `ModelCacheDownloadResult` and `ModelCacheEvictionResult` | Cache workers, stored results, cache API and Run/Switch receipts |
| Fleet and Run/Switch progress/results | `fleet_profile_contract.py` and `run_switch_contract.py` | Orchestration, restart/replay reads, Library projections and APIs |
| Run artifact verification | `ArtifactVerificationResult` in `run_switch_contract.py` | Cached/distributed artifact verification producers and Run/Switch consumer |
| Route activation marker | `vonk_agent_protocol.route_activation.ActivationMarker` | Controller publisher and the exact shared model packaged in LiteLLM |
| Controller image-cache receipt | `RuntimeImageReceipt` in `runtime_image_preparation.py` | Image preparation, persisted receipt reader, availability worker and execution-plan compiler |
| Database rows | SQLAlchemy models in `control/src/vonk_control/models.py` | Controller API and worker processes |
| Documents stored in JSON columns | The contract bound to each `JSON` column in `control/src/vonk_control/stored_columns.py` (read and written through `stored_json`) | Every Controller reader and writer of that column, the published OpenAPI (`x-vonk-json-columns`) and the generated clients |
| Global container-runtime policy and problem schemas | `vonk-forge-web` `schemas/`, copied into `schemas/global/` at the commit in `schemas/global/contract.lock.json` | Rust agent OCI policy (`vonk-agent/src/oci.rs`) |

To take a newer `vonk-forge-web` revision of the global schemas, run
`scripts/update-global-contracts --commit <full-sha>` with a clean
`vonk-forge-web` checkout next to this repository (or pass `--repo`). It
copies the schemas byte for byte and rewrites the lock with their hashes;
review and commit both.

Model and Recipe are the two **authoring** contracts. Operations, progress,
telemetry, and device messages also need wire contracts; they do not become
additional recipe documents for users to maintain.

### JSON columns

Every `JSON` column in `models.py` has exactly one contract in
`stored_columns.py`, typed at every level: no `Any`, `object`, `JsonValue`,
bare `dict`/`list` or mapping of those. A level that is genuinely someone
else's document is a named `ExternalPassthrough` subclass whose written reason
says whose it is; nothing else may be untyped. A table that stores several
document families (a job's `kind`) binds one contract per kind.

Writers store `model_dump(mode="json")` of the contract model; the ORM write
guard checks every changed JSON column against its contract (the test suite
refuses a violation, the Controller reports it once and stores it, because
bookkeeping never blocks work). Readers go through `stored_json.read_column`:
a document written by an older build is adopted (retired fields dropped), and
a damaged one is a typed unknown (`Residue`) naming the column and row, never
an exception. `control/tests/test_json_column_contracts.py` fails when a column
has no contract or a contract has an untyped level.

Storage ownership is separate from schema ownership. Follow the
[architecture boundary](architecture-overview.md#state-ownership): canonical
typed artifact records belong in managed storage, while SQL keeps coordinated intent,
authorization, and exact references. A shared DTO is not permission to
persist two authoritative copies of its availability or checkpoint fields.
Regenerate connected clients/wire schemas when contracts change; validate
filesystem records with the same canonical JSON semantics as other producers.

## Cache-first profile lifecycle

The Controller/NAS cache is the trusted preparation authority for profiles. A
profile choice binds the exact model artifact set and required runtime image;
profile creation and updates must not select an uncached or merely Spark-local
asset. If an existing choice is no longer fully cached and verified, apply is
blocked with typed, actionable reasons and offers the cache-preparation
operation. Apply must not silently fetch from upstream, choose another
revision, or recover the authority from a Spark copy.

After the cache gate succeeds, the Controller distributes the exact digest- and
size-bound assets to all selected Sparks in parallel. Each target verifies its
destination and image identity; a verified local copy is skipped. The
Controller then stops and replaces conflicting workloads and reports durable
per-Spark transfer progress and serving readiness. Spark-local copies are
execution evidence only, never an alternate persisted authority.

### Whole-fleet membership

Each profile review and load uses the current enrolled, non-revoked fleet, with
unassigned Sparks idle. The preview binds current membership and planned
effects. If membership changes before acceptance, reject that preview and
require a new review.

Target ongoing behavior uses one durable selected-profile record in PostgreSQL
pointing to the immutable accepted application snapshot as whole-fleet desired
state. Profile saves are drafts and a load application is one-shot; neither the
latest application nor audit history can stand in for the selected-profile
fact. The Controller worker reconciles the accepted snapshot after enrollment
or revocation changes membership; no new operator decision is required. A new
Spark enters idle. A removed Spark leaves the scope, so ignore its independent
assignment without changing the snapshot. If removal leaves a multi-Spark
model without a required member, stop and confirm its remaining reachable
ranks, withdraw its route, report the missing member, and continue unrelated
assignments. A removed Spark's old run is not proof that it stopped or that its
capacity is free. If the same node ID rejoins, reconcile its actual state
against the accepted snapshot before scheduling work there. Report only
confirmed cleanup.

The selected-profile authority and membership worker also check accepted
running assignments for live-state drift and use the existing run and route
recovery paths.

NAS garbage collection may remove local model objects that are no longer
referenced by a saved profile, active workload, or preparation operation. It
must not remove referenced cache objects, and Spark copies do not pin or replace
NAS ownership. Cache eviction results therefore describe Controller/NAS
authority changes; target-local cleanup is not a cache-authority operation.

Only the current Model and Recipe authoring format is supported. The retired
recipe parser, recipe-v1 schema asset, and flat install/start fixtures are
removed. Version numbers belong to each document: current job envelopes and
stop commands can still use schema 1 without accepting old Recipe documents.

## Python

Import the canonical Pydantic model when consuming a shared document. Validate
at ingress, retain the typed value through the operation, and serialize through
that model at egress. API response validation matters as much as request
validation. Do not recreate a subset of Model or Recipe in a route, worker,
validator, or CLI.

Required fields, scalar types, nesting, discriminators, patterns, and numeric
bounds belong in the Pydantic field definitions so the generated JSON Schema
exposes them. Use model validators for relationships between fields and
execution/security rules. Wrapping a handwritten parser in a mostly untyped
Pydantic class does not create an authoritative structural contract.

Fixtures, acceptance servers and health probes must follow the same rule.
A hand-written expected dictionary is an assertion about test content, not a
replacement request/response schema. Validate through the shared model first,
then check the meaningful values for the test. External protocol fixtures must
accept declared optional defaults and supported extensions, and exercise both
streaming and non-streaming when supported. Structural rejection and security
validation must not depend on whether a client spelled out a default value.

Ordinary internal records and database tables can use dataclasses and ORM
models. A JSON contract document loaded from a database must still be parsed
with its canonical model before use. A database row is not proof that the
document satisfies the contract.

Use JSON validation semantics for wire documents and persisted JSON, even when
the database driver has already decoded them into dictionaries and lists.
Pydantic's strict Python-object validation has different rules for tuples,
UUIDs and datetimes; applying it to decoded JSON can reject the model's own
serialized output. Pass the JSON representation to `model_validate_json`
instead of relaxing types with `strict=False`. Connected persistence tests
must serialize the producer, store and load the document, then run the real
consumer.

Persisted progress is a contract too. Validate the complete stored document
before interpreting a missing child or adapter state. Only a declared optional
field may be omitted; nullability independently controls whether `null` is
valid. Malformed JSON must not silently become an empty or new operation.
Completed phase receipts have phase-specific required fields,
and reuse the canonical model-cache and runtime-image receipt types.

## Optional fields and canonical serialization

Accept omission and explicit `null` equivalently for optional nullable fields
whose declared default is `None`. **Omit those unused fields on output.** This
is the standard for API, persisted wire, and signed/hashed contract documents.
Do not change an optional field to required simply because a generator omitted
it during serialization.

Controller-owned recipe parents, recovery continuations and Stop authorization
contexts use `controller_recipe_document` to store a full JSON dump, including
all defaults and explicit nulls. Composition omits unused fields only at the
context's top level; recursive `exclude_none=True` can erase required nullable
fields inside a recorded compiled plan. Receipts and outbound agent payloads
keep canonical wire encoding. Parent digest checks normalize through the typed
stored contract, and embedded agent payload comparisons use canonical encoding.

| Declared meaning | Accepted input | Canonical output |
| --- | --- | --- |
| Optional nullable field, default `None` | Missing or `null` | Field omitted |
| Required nullable field | A value or explicit `null` | Field retained; missing is rejected |
| Optional field with a non-null default | Missing applies its declared default | Preserve the resulting value; `null` follows the field's declared rules |
| Meaningful `false`, `0`, empty string/list/object | Valid value of the declared type | Preserve the value |
| Engine-owned JSON content | Values allowed by its extension contract | Preserve content, including meaningful nested `null` |

Use model-aware normalization: validate the selected canonical model, apply
its declared defaults, omit only its unused optional-null fields, and then
serialize deterministically. Producers hash or sign that canonical document;
consumers apply the identical policy before checking its digest or signature.
When a payload's model is selected by its operation kind, select that model
before normalization. Do not normalize arbitrary dictionaries by deleting all
nulls, and do not weaken verification to hide a mismatch.

Schema type and serialization are separate concerns. A formatted string must
remain a string: changing `+00:00` to `Z` changes its bytes even if both denote
the same time. Any normalization of a true datetime field must be defined by
the authoritative contract and shared by both languages. The generator must
derive field/default/omission behavior from that same source, not a handwritten
list of special cases.

Connected tests must use actual producer output and the real consumer. Cover
optional missing versus null producing identical canonical bytes and digest,
required-null preservation, defaults, false/zero values, engine-owned nulls,
formatted strings, and real signature verification. A schema-acceptance test
or equality between two manually written fixtures is insufficient.

## Rust and generated clients

Rust wire structs and enums are generated from the authoritative Pydantic
models by `scripts/generate-agent-wire`, using pinned typify 0.7.0. The exporter
checks in the exact validation schema; typify generates the declarations used
by production protocol and HTTP consumers. Handwritten code retains semantic,
execution, and signature validation rather than defining competing wire fields.
Remaining adoption and connected checks are not yet complete; generated types
beside handwritten active DTOs do not complete the chain.
Distribution, enrollment, certificate rotation,
bootstrap, inventory, build/import, package and host-helper grants, compiled
launch plans, and telemetry use shared Pydantic wire models. Enrollment returns the issued
certificate directly; there is no pending-approval response or polling fallback.
Bootstrap also has one response: the helper authority key is required, and the
address and hostname list are explicit even when they are `null` and `[]`.
There is no setup-schema selector or older bootstrap variant.

Certificate activation atomically carries a live operation's credential binding
from its valid active source certificate to the replacement. Its attempt, fence,
and lease deadline do not change. Expired, superseded, completed, or revoked
operation authority is never restored, and the retired certificate remains denied.
The agent drains outstanding HTTP requests before activation and swaps the shared
transport before admitting new requests. An idle claim poll returns promptly
when a valid replacement is staged, leaving time to activate before expiry.
While a long upload drains, waiting for that swap must still allow heartbeats
to renew its lease. A terminal heartbeat
failure, panic, or task cancellation stops the executing process even when its
executor is blocked in synchronous process polling.

Removing a recipe cache requests cancellation of any source build. A build
that never started releases its reservation immediately. A claimed build keeps
its reservation until the Controller receives a typed `recipe.build.cleanup.v1`
receipt for that exact build and operation. Cleanup stops only its transient
user service and verifies that it is inactive or absent; failed inspection or
stop commands remain failures. The worker resumes cleanup after a restart,
including builds already marked removed. Late build completion cannot publish
the removed image or bypass cleanup. Model caches and unrelated builds remain
intact.

The work-claim request and runtime identity are defined in
`agent_protocol/src/vonk_agent_protocol/claims.py`. `protocol_version` is the
single Controller-agent version gate: a Controller refuses any other version
and names the fix (reinstall the Spark agent). mTLS identifies the node; the
claim, heartbeat, directive, result and helper-grant messages carry only the
attempt fence, from which the Controller resolves job, operation and attempt.
The claim also reports the runtime identity, wait time and, when present, the
host runtime's preflight fingerprint. The Rust HTTP transport and the connected
test use the same request serializer. Enrollment keeps its bounded raw-body
security handling, while OpenAPI exposes the exact `EnrollmentSubmitRequest`
used to validate that body.

Work claiming takes due operations in creation order; authority, lease and
concurrent-mutation checks still apply.

`scripts/generate-control-clients` derives the Controller OpenAPI document and
Python/TypeScript clients from the actual API. Never fix drift by hand-editing
generated clients or weakening their schema. Rust wire compatibility requires
tests that serialize actual producer values and pass them to the other
language's real parser and validator, in both directions.

### Browser numeric values and runtime acceptance

The same canonical OpenAPI input generates browser DTOs and Ajv standalone
validators. Both the generic browser request and the openapi-fetch response
middleware validate raw JSON before exposing a DTO. Request bodies and path
and query parameters use the same generated contracts. SSE frames validate
their canonical event component before updating the retained Fleet snapshot.
A malformed frame or response is not evidence of a completed remote operation.

The browser decoder uses native JSON.parse source-context revivers (ES2025)
to retain numeric tokens in the pinned lossless-json numeric wrapper. Node 26
and the hosted Chromium consumer exercise this capability. A browser without
numeric source context fails with an explicit update-browser cause, rather than
misreporting a valid network document as invalid. Own JSON keys, including
`__proto__`, remain data through parsing and normalization. Strict
integer fields accept integer tokens, not `1.0` or `1e0`. Integers outside the
JavaScript safe range remain `ExactNumber` values, an alias of that library's
single `LosslessNumber` representation. Bounds are compared exactly without
expanding exponent tokens. Arithmetic, selection identity, copy and JSON export
preserve integer values; display ratios alone use an explicit approximation.
An unsafe JavaScript Number cannot silently become an exact integer request:
its float token fails a strict integer field; use an exact integer value instead.

Canonical float branches use finite IEEE materialization, including permitted
underflow and signed zero, before checking their float bounds. The successful
compiled union branch also owns normalization. When a canonical scalar union
distinguishes integers from floats, an integral float keeps its float token
through export and request serialization using the same numeric wrapper;
`1000.0` cannot silently become the integer `1000`. A mathematical JSON Schema
integer is a different rule from the production strict integer token rule;
the pinned official semantics corpus exercises that distinction explicitly.
Neither path permits non-finite float materialization. Numeric token wrappers
cannot satisfy object branches or evade ordinary object property constraints.
The nominal serializer treats an ordinary `isLosslessNumber` property as data,
not as permission to emit an unchecked numeric token.

Ordinary Controller and standalone CLI JSON decoding uses last-key wins for
duplicate object keys, and the browser follows that boundary. Stricter owning
ingress, such as browser login and Spark wire parsing, retains its explicit
duplicate-key rejection. Schema validation does not change that policy.

Numeric maxima come from their actual owners: physical log-drop counters use
the complete u64 range, PostgreSQL integer and bigint generations retain their
storage domains, and Linux process IDs retain their execution domain. Other
intentional unbounded integers stay unbounded, including engine values and
logical wire counters. Rust's generated unbounded integer representation
retains exact arbitrary-precision JSON values instead of inventing an i64 cap.
Parser resource constraints, including Python's decimal integer conversion
limit, remain separately reported consumer behavior rather than hidden schema
bounds. No JavaScript safe-integer maximum is added to the canonical schema.

The library producer packs pages into its existing shared byte budget. That
request/library budget does not establish a universal API response ceiling;
the browser does not impose one on unrelated responses. Transport limits must
come from the owning route or a reviewed shared producer policy.

The hosted consumer corpus uses actual ASGI output, installed raw and generated
CLI HTTP consumers, both browser HTTP paths, and Rust HTTP and durable restart
readers. Identical diagnostic leaf bytes travel inside each consumer's real
envelope. Component-only, Python-only and Python/Rust-only cases are identified
explicitly; an unexecuted route or a model-only semantic constraint is not
reported as cross-language network proof. Generated outputs carry their exact
source SHA and file digests, and the required drift check includes browser
runtime validators as well as DTO declarations.

Test the actual FastAPI serialization schema as well as Pydantic validation.
A custom serializer can accidentally erase a nested model from OpenAPI even
when its Python validator remains strict. `test_api_contract_graph.py` permits
open objects only at explicitly documented engine-value and authority-document
extension points; it rejects opaque fixed nested documents.

Rust generation must preserve field presence, nullability, scalar types, and
tagged unions from this same Pydantic graph. Generation does not replace
semantic validation or connected wire tests: test the actual producers and
consumers, including omitted required fields, explicit nulls, and numeric
boundaries. Optional-field presence follows the canonical omission policy
above; absent and explicit-null forms must not create different identities.

Every generated model validates its input against the exact exported schema
before Serde constructs the value, including direct nested deserialization.
The deterministic adapter preserves required nullable fields, rejects unknown
fields and incorrect integer tokens, materializes declared defaults, and checks
bounded scalars. Primitive extension unions retain JSON values so integers do
not silently pass through floating-point alternatives. Formatted Pydantic
strings retain their bytes; a date-time validation format does not normalize a
digest-bound string. Pydantic inheritance also generates identity projections.

Outgoing directly constructed models use `canonical_generated_json` at HTTP
boundaries. The generation check runs in the required Controller/Spark wire CI
lane; stale schema or Rust output fails that check. Connected producer/consumer
checks additionally verify semantic validation and signed or hashed bytes.

## Lifecycle vocabulary and the outcome envelope

The closed words of the lifecycle core and of an agent's result have one
definition: `agent_protocol/src/vonk_agent_protocol/lifecycle_vocabulary.py`
(states, effects, outcome kinds, event kinds, operator actions, wait reasons,
failure codes, the security-refusal and invalid-request reason codes, the error
categories, and the typed outcome categories). `scripts/generate-agent-wire`
carries them into `wire.json` and the Rust declarations, and
`scripts/generate-control-clients` into the OpenAPI document, the generated
TypeScript types and the runtime constants in
`control/web/src/api/vocabulary.generated.ts`. `vonk_control/lifecycle/types.py`,
the adapters, `failure_classification` and the syntax guards import them;
none keeps a second list. Add a word to the contract and regenerate; never spell
one by hand (the added-line vocabulary guard in
[testing and CI](testing-and-ci.md) fails the change).

The stored `state` of a lifecycle subject speaks the nine words of
`LifecycleState` (`queued`, `running`, `observing`, `backoff`, `succeeded`,
`failed`, `cancelled`, `superseded`, `needs-operator`). The retired spellings
(`waiting-for-operator`, `cancelling`, `waiting`, `partial`, `expired`) live only
in the contract's alias table, `STATE_ALIASES` (and `JOB_KIND_ALIASES` for the one
kind of generic job that spells a word differently), which says what each one means
per subject. Every reader of a stored state goes through
`vonk_agent_protocol.adopt_state(subject, stored)` (selections through
`stored_words`), so a row written before the rename is adopted when it is read, and
the Controller rewrites such rows once at startup (`legacy_states.py`);
`input_state(word)` (and the generated `STATE_INPUT_ALIASES`) lets an API filter or
CLI argument still send a retired word for one release.

Three things that used to be spelled in a state word are separate typed fields: an
artifact job's preparation stage (`preparation`: `draft`, `ready`; its state is
absent until it is submitted), a cancel being driven (`cancel_requested_at`; the
state stays the core's), and why an attempt is observed (`observation_cause`:
`reported-unknown` or `lease-lapsed`). A recipe update batch that ended with some
children done is `failed` with `partial: true`. The agent wire keeps its four result
words and is mapped to the stored words at one place.

## Reason, blocker, warning and attention codes

Every code the Controller shows as *why* something is waiting, refused, blocked or
degraded is a member of a closed enum in
`agent_protocol/src/vonk_agent_protocol/reason_codes.py`, grouped by the domain
that raises it (`ModelCacheCode`, `RecipeImageCode`, `RuntimeImageCode`,
`ProfileReasonCode`, `RunSwitchCode`, `ProjectionCode`, `InstallAdmissionCode`,
`RunAdmissionCode`, ...). The values are the words already stored and shown; the
module closes the set and adds none. `ReasonCodeVocabulary` publishes the enums
into `wire.json` (Rust), the OpenAPI document and the generated TypeScript
constants, and `make_blocker`, `ProjectionReason`, the error classes, the
`code=` / `*_code=` fields and the Run/Switch message prefixes take a member, never
a string. Stored text stays text (fail-open): a row or a relayed code this release
does not know is read and shown as it is, and a respelled code is adopted through
`RETIRED_CODE_SPELLINGS` (`adopt_reason_code`). A code that wraps another domain's
(`run-switch.` plus a resource, reconcile, stop or uninstall code) is looked up with
`run_switch_code`, and a code built from a closed pair (`resource.context_unknown`)
with `resource_term_code`; both are total over their members.

The agent's own words are closed the same way. A runtime preflight finding's `code`
is a `RuntimePreflightFindingCode` member (`preflight_finding.available`,
`preflight_finding.proc_mount_denied`, `preflight_finding.helper_operation_io`, ...),
and the code the privileged helper names in an error reply and in failure evidence
(`helper_error_code`) is a `HelperErrorCode` member. The Rust agent and helper build
both only from the generated enums, and `rust/crates/vonk-agent/tests/protocol_literals.rs`
fails a string spelled by hand, a second struct literal of the finding, and a word
that only the helper or protocol crates spell. The finding `code` stays a
pattern-bound string on the wire so that an older agent's free text still reads: the
Controller adopts it through the one adapter, `adopt_preflight_finding_code`
(`RuntimePreflightFinding.finding_code`; `RETIRED_FINDING_CODE_SPELLINGS` holds the
bare and kebab-case spellings agents wrote before the enum). A word no member spells
reads as `preflight_finding.unclassified` and is shown as reported; the finding is
never refused. It is scoped to the finding (not folded into `RETIRED_CODE_SPELLINGS`)
because the old bare word `helper_grant_invalid` is also a member of another domain.

How far work has got is closed the same way. `ProgressPhase` (`downloading`,
`building`, `reconciling-installation`, `transfer`, `completed`, ...) is the one set of
words that `OperationProgress.phase` and `OperationMemberProgress.phase` say, and both
the Spark agent and the Controller write only its members. `FailureStage` is the step
a failed or unconfirmed operation stopped at (`OutcomeEvidence.stage`), and
`HostHelperResponseStatus` is the verdict word of a privileged-helper reply; all three
are published through `LifecycleVocabulary` and generated into Rust and TypeScript. The
wire fields stay strings so that an older agent's free text still reads: the Controller
reads a phase through the one adapter, `adopt_progress_phase` (`RETIRED_PROGRESS_PHASE_SPELLINGS`
maps `download`, `verify`, `cleanup`, `prepare`, `upload`, `transferring`, `distribution`,
`update` and `complete` to their members), and a word no member spells is shown as
reported and counts as neither a transfer nor a wait. The agent's source-policy recheck
reports `SourcePolicyCode` members (the Controller's findings, not its own spellings),
and the kebab-case podman build diagnostic is derived from the finding code
(`PodmanBuildDiagnostic::finding_code`), so one fact has one spelling; the `diagnostic`
sentence stays text.

The added-line vocabulary guard rejects new occurrences: a literal equal to a member, and
any string constant in a code position (see [testing and CI](testing-and-ci.md)),
fails. A new code is added to its domain enum first.
The stored records that are not lifecycle subjects have their own closed state
machines in `agent_protocol/src/vonk_agent_protocol/state_machines.py`: the
installation and its ranks (`InstallationState`, `InstallationNodeState`), the
distribution assignment (`DistributionAssignmentState`), the run and its route
(`RunState`, `RouteState`, `RoutePublicationState`, `GatewayRouteState`), the
fleet-profile endpoint and observed assignment (`EndpointState`,
`ObservedAssignmentState`, `DesiredAssignmentState`), and the certificate,
enrollment grant, model file, asset availability, catalog sync, reservation and
placement words. They are carried by the same `LifecycleVocabulary` and so reach
the Rust declarations, the OpenAPI document and the generated TypeScript. The
Controller's CHECK constraints are generated from them (`machine_check`), models
and projections validate through `vonk_control.machine_states` (which adopts an
old spelling through `MACHINE_ALIASES`, empty until a word is renamed), and the
added-line vocabulary guard rejects new hand-spelled contract words.

Every agent operation result is one `OperationOutcome`
(`agent_protocol/src/vonk_agent_protocol/outcome.py`), tagged by `kind`:

- `done` carries the operation's typed success body;
- `failed` is a *definite* failure with a closed `FailureCode`, an optional
  `failure_kind` (the retry class), `retry_after_seconds`, typed
  `OutcomeEvidence` and, for a one-shot job whose process ran, its receipt. A
  confirmed cancellation is the `operation_cancelled` code;
- `unknown` says the effect could not be established. It carries a closed
  `WaitReason` and the same typed evidence (bounded diagnostic logs, the helper's
  error and exit code, the stage). The Controller observes the effect; it never
  parks the work behind a person because of this word.

`AgentResult.state` keeps its four wire words, and the outcome decides which is
truthful (`done` is `succeeded`, a confirmed cancellation is `cancelled`, any
other `failed` is `failed`, `unknown` is `waiting-for-operator` on the wire); a
report whose word disagrees with its outcome is refused on both sides. The
Controller projects a typed outcome back to the body shape every stored-row reader
already understands, and stores the unknown report as an `observing` attempt with
the cause `reported-unknown`.

The Controller reads an agent report through one function,
`vonk_control/agent_outcome.agent_outcome`. A typed report is the outcome; an
untyped body from an agent built before the typed contract goes through the
legacy half of the same function, which is the only code that still interprets
an untyped agent body. It exists for one release so agents already on the Sparks
keep working; delete it together with the legacy untyped members of
`AgentResultPayload` once no deployable agent package predates the typed outcome.

The Rust agent builds every operation result from the generated types.
`rust/crates/vonk-agent/src/outcome.rs` holds the one in-process type an executor
returns (`ExecutionResult`: `Done` with the typed success body, `Failed` with a
`Failure` draft, `Unknown` with a closed `WaitReason`), and
`ExecutionResult::finish` is the only place that turns it into the protocol
message: sanitized text, a stable code per operation, bounded diagnostics. There
is no `json!` result body in the agent, and
`rust/crates/vonk-agent/tests/protocol_literals.rs` fails the build if one
appears or if the agent spells a vocabulary word instead of using the generated
enum. A Controller older than the typed outcome refuses a typed result, which the
agent keeps and retries; agent upgrades are issued by the Controller, so the
Controller is always at least as new as the agent it upgrades.

An effect the agent cannot confirm is the `Unknown` arm, and it is never a bare
wait: `ExecutionResult::unknown` takes an `UnknownEvidence` (the stage that
stopped and a bounded cause or helper code), which reaches the Controller as the
outcome's `evidence`. Work that is safe to repeat is repeated locally first (the
model-custody step of a start, three bounded retries on transient storage
errors). A retained run that is not exactly the start's is healed or refused by
the exact stop, which removes a container only when every identity label equals
the authorized order's: what is proven the order's is removed and the start goes
on; a container that is not proven (another party's, or another generation's,
which the Controller stops through its own recovery) is left untouched and the
start ends as `retained_container_foreign` with `failure_kind: resource-prerequisite`,
naming the container. The Controller reads that as a prerequisite, re-issues the
start with backoff and keeps the load open until the name is free; it is never an
invalid contract and never removes anything the agent does not own.
`rust/crates/vonk-agent/tests/wait_actions.rs` requires an advertised action
for every wait reason the agent constructs.

The three error categories, `SecurityRefusal`, `InvalidRequest` and
`UnknownError`, are the only things a lifecycle adapter may raise or report, each
with its own closed reason set; the blocker allowlist's `security-edge` and
`input-validation` families are the first two, and `already-retried` and
`bookkeeping-debt` are the third (`error_category_of`).

Inside the Controller the same three are raised as `SecurityRefusalError`,
`InvalidRequestError` and `UnknownOutcomeError` (all `CategorizedError`, in
`vonk_agent_protocol.outcome`; these are Python exceptions and are not part of the
wire schema). An existing error type joins a category by inheriting the base beside
its current one (`class AuthError(SecurityRefusalError, ValueError)`), which
changes no `except` clause; the optional keyword-only `reason=` names a closed
reason and `typed_error()` returns the wire `SecurityRefusal` / `InvalidRequest` /
`UnknownError` for it. A raise in a lifecycle or operation path must use one of
these types; the guard is described in [testing and CI](testing-and-ci.md).

## Required launch checks

The `Controller and Spark wire contract` CI job checks both sides of the
launch boundary. It runs when the Controller, agent, public-contract lock,
recipe revision, runtime compiler, or harness configuration changes.

- `control/tests/test_recipe_launch_contracts.py` loads every published Model
  and Recipe through their canonical Pydantic classes, compiles every recipe
  role with the production compiler, and validates the final launch document
  through the shared `CompiledExecutionPlan`. Cache receipts are synthetic;
  model files and images are not downloaded by this structural check.
- `scripts/tests/run_agent_wire_contracts.py` builds the Rust probes from
  the checked-out source and runs every `test_*_wire_bridge.py` in the required
  Linux lane. The tests queue requests through the actual Controller,
  parse them through the real Rust claim and launch validators, produce results
  through the agent's shared result builders, and consume those results back
  into persisted Controller state. It covers single-node starts and distributed
  rank-launch/collective-readiness starts, distribution manifests, heartbeat
  directives, bootstrap, enrollment, certificate renewal, build/import evidence,
  inventory, artifact jobs, and telemetry. The host-helper bridge passes an
  actual API-issued grant through the Rust verifier. The complete persisted
  run-observation workflow has its own integration check. No old heartbeat
  response shape is accepted.
  The same required job runs the complete `agent_protocol/tests` suite,
  including schema-derived required-field, type, nullable, unknown-field and
  vocabulary checks through the Rust parser. These cover the declared fields
  in the tested endpoint, job, image-source and distributed variants; custom
  cross-field rules still need behavioral cases.

A failing check blocks the CI gate. A new required field must be carried through
its producer, parser, stored document, and response before the change can pass.
An explicit `null` and an omitted required-nullable field are different wire
values; both languages must enforce that distinction.

The complete Controller suite also imports the current catalog into disposable
PostgreSQL and checks typed Library responses and offline package reuse. These
checks use `VONK_RECIPE_LIBRARY_ROOT`, the same checkout used by the compiler;
they do not skip because a temporary receipt from an earlier run is missing.

## Strict structure, extensible content

- Require the declared fields, types, nesting, and message variants. Reject
  misspelled fields outside explicitly declared extension maps. Do not silently
  turn a malformed value into a valid-looking default.
- Keep model families, versions, creators, and engine-owned argument names
  open where their fields declare an extensible string or map. A new family or
  engine option does not require editing a Python enum.
- Preserve engine arguments and values through compilation. Known-option
  metadata improves the UI; it is not an exhaustive argument allowlist.
- Enforce execution security at the execution boundary: safe paths, declared
  writable mounts, workload isolation, and authenticated privileged actions.
- Describe failures with the field or operation that failed. Distinguish invalid
  structure, provider authentication, transport failure, and an engine rejecting
  an option. Keep secrets out of errors.

Telemetry preserves complete valid samples. The agent batches toward 1 MiB,
sends a larger sample on its own, and retries without dropping or reordering
metrics. The authenticated endpoint and shared parser use the same 16 MiB
transport memory ceiling; there is no separate serialized metrics-size limit.

## Verify the handoff

Exercise API and worker instances with separate process-local state, real
serialized identifiers, persisted progress, and the actual runtime importer.
Do not substitute matching hand-written fixtures for the producer's output.
For example, Docker's imported image ID, an archive config ID, and a registry
manifest digest describe different objects and must not be assumed equal.

The image-cache receipt is one strict Pydantic document. Its producer writes
every field, including an explicit `null` build identity for registry images.
The reader rejects missing fields; the compiler consumes the same typed
receipt. Its explicit projection into the separate compiled-launch document
replaces the former duplicate receipt class, field alias and dictionary
fallbacks.

An artifact verification result must include `verified_build_id`. A source
build supplies the exact Controller build UUID; a published image supplies
explicit `null`. Omitting the field is malformed, and a different build UUID
cannot satisfy the requested run. The producer constructs
`ArtifactVerificationResult`, and the consumer validates the serialized result
through the same model before advancing the operation.

A passing model validation test proves document structure. A passing connected
lifecycle test proves the tested orchestration. Neither alone proves that every
model works on physical Spark hardware.


## Nested contract coverage

OpenAPI is generated from the actual application and its nested Pydantic graph
rather than maintained by hand, so the generated document is the authority for
exact route and model counts. Every application route appears in it; the routes
without a response model handle empty responses, raw uploads/downloads, signed
files, metrics, or event streams.

The fixed documents previously exposed as dictionaries now use concrete models:

- Install plans expose `CompiledExecutionPlan` for each Spark; uninstall plans
  expose the public `RecipeDefinition`.
- Artifact jobs share `CompiledArtifactContract` across compilation, persisted
  reads, runtime handoff and HTTP responses.
- Lifecycle results validate the operation kind and compose shared protocol
  evidence, including tensor-parallel starts.
- Fleet, Library and Run/Switch progress and receipts validate at persisted
  reads/writes and API projections. Each phase receipt has its own required
  structure and must match the phase being executed.
- Model-cache results share canonical download/eviction contracts; update
  identities use the public `ModelReference`.
- Alternate JSON errors serialize their documented models. Validation problems
  contain a bounded `detail` and typed `issues`; request inputs and exception
  context are not copied into error responses.

The graph regression checks actual serialization schemas, including browser
and agent routes. It allows open objects only for engine-defined parameters,
engine measurements and path-selected authority documents. A separate schema
comparison proves authored job inputs and compiled wire inputs have the same
nested structure and constraints. Rust wire tests check both serialization
and semantic validation; schema equality alone does not prove runtime behavior.

These checks establish source and interface consistency. Publication, Controller
deployment and physical Spark execution remain separate verification steps.

### Persisted execution and cache documents

PostgreSQL JSON columns store current documents, not alternate API formats.
Their owners validate the complete document in JSON mode before writing it and
before a later operation consumes it:

| Stored document | Authoritative contract |
| --- | --- |
| Installation and run plans, node admission details, run endpoints | `recipe_execution_contract.py` |
| Build requests | Protocol `RecipeBuildRequest`, reused by `recipe_execution_contract.py` |
| Build policy reports | `StoredBuildPolicyReport` with nested `StoredPolicyFinding` |
| Catalog model/recipe documents | Public `ModelDefinition` and `RecipeDefinition` |
| Catalog projections | `catalog_revision_contract.py`, composed from public model/topology and protocol build-option types |
| Cache manifests, preparation/repair/eviction payloads and results | `model_cache_contract.py`, selected by operation kind |

A required nullable value remains present; unused optional fields are omitted.
Malformed stored documents produce a controlled error instead of becoming empty
state. Engine-owned extension values retain their declared flexibility. Spark
copies are derived target evidence, not parallel cache authorities; persist
their verification and readiness through the current lifecycle receipts rather
than creating a second cache document.

Uninstall validates the same typed installation document and immutable artifact
identities, but does not require its stored placement to be launchable. Only
that Controller reader supplies a process-local storage-validation context;
JSON cannot select it. Schema, path, artifact-byte, topology and security checks
remain enforced. Install, start and ordinary persisted-plan reads retain full
launch validation, and teardown never rewrites a stored plan to make it pass.
Unknown reclaimable bytes are reported as unknown with a warning; they do not
block removal of an exactly identified, stopped installation. A retry retains
completed per-node removal receipts and queues only the unfinished nodes. Active
runs, active cleanup operations and changed immutable membership still block it.

Structure validation does not replace transaction semantics. Cache workers
refresh the database row when acquiring its lock, so a prior cooldown scan
cannot hide another worker's newly committed claim. The PostgreSQL regression
forces that interleaving and verifies that workers claim different operations.

## Discovered HTTP completeness gate

`control/tests/test_api_contract_completeness.py` constructs both supported
browser-auth configurations and discovers mounted FastAPI routes, including
child applications. Every operation must have an OpenAPI declaration; hidden
or opaque transports fail rather than disappearing from the inventory. The
report records canonical model owners, path/query/header/cookie parameters,
request media and successful response media. Agent artifact streams and metrics
belong to this full transport inventory; the admin client schema still excludes
agent routes and metrics.

Ordinary JSON bodies use FastAPI's typed bindings. A bounded raw JSON reader
uses `raw_json_body` with its existing canonical model, and its declaration must
match that model. Raw uploads declare bytes, without changing their runtime
limits or parsing. Mutating handlers that accept a raw Request but no body
explicitly declare `x-vonk-request-body: none`; this prevents a delegated reader
from silently avoiding body classification. Exact-byte responses declare their
streaming transport. No-content responses remain explicitly bodyless.

The wire exporter derives API-owned models from these actual agent bindings,
including declared error responses, instead of maintaining a separate model
name list. Fully qualified owners and referenced definitions are checked against
the generated export. Protocol module discovery still supplies internal models.
The required Controller/Spark CI lane runs discovery, schema freshness and
mutation tests that add hidden routes, omit exports and misdeclare raw bodies.

This first gate proves declaration coverage and model ownership. It does not
prove that every handler emitted a valid successful response, every client used
its generated parser, or every database/file handoff preserved meaning. The
next gate must collect real ASGI producer/consumer witnesses and join them back
to this discovered operation inventory, then apply independent SQLAlchemy and
runtime-I/O discovery to persistence. Engine-owned extension values and signed
passthrough bytes keep their declared semantics.

## Executed response witnesses

Controller tests can collect the actual successful bytes emitted by production
ASGI routes, including response middleware, with:

```sh
uv run --project control --frozen pytest -q control/tests \
  --api-response-witness=/tmp/controller-response-witnesses.json
```

Run this collector without xdist. It reports the discovered operation inventory,
the executed method/path/status/media combinations and their test IDs, plus every
operation lacking a successful response witness in that test selection. Missing
operations remain explicit; there is no maintained exception list or claim that
declarations constitute execution. A failing test run is recorded in the report.
The recorder's deliberate mutation tests are excluded from application evidence.

Validation uses the response's declared JSON Schema and actual media type.
Structured bodies and SSE data frames are validated after each test so schema
generation cannot change endpoint timeouts. Binary transfers preserve bytes;
the observer checks the media declaration and bodyless status rules, while
individual upload/download tests compare actual bytes and digests. Structured
capture is bounded to 16 MiB per response. Reports contain counts and test IDs,
not payload values, credentials, or payload hashes. Mounted applications are
validated once against the owning application's schema.

These are response-producer witnesses. They do not establish downstream parsing,
authorization correctness, business transitions, all streaming timing behavior,
or persistence coverage. Existing tests vary in their application and service
fixture setup; a handler witness does not imply the full deployed application
configuration was exercised. The source-bundle client handoff separately exercises
the generated admin upload client, actual route/storage service, generated
response parser and exact-byte download. Artifact transport tests additionally
consume actual JSON with the generated client and verify declared optional
null/omission equivalence. Other consumer and storage edges still require their
own connected evidence; raw engine extensions are not exhaustively enumerated.
