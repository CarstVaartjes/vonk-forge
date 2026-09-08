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

Model and Recipe are the two **authoring** contracts. Operations, progress,
telemetry, and device messages also need wire contracts; they do not become
additional recipe documents for users to maintain.

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
Remaining adoption and connected checks are tracked in
`docs/contract-handoff-implementation-plan-2026-09-08.md`; generated types beside
handwritten active DTOs do not complete the chain.
Distribution, enrollment, certificate rotation,
bootstrap, inventory, build/import, package and host-helper grants, compiled
launch plans, and telemetry use shared Pydantic wire models. Enrollment returns the issued
certificate directly; there is no pending-approval response or polling fallback.
Bootstrap also has one response: the helper authority key is required, and the
address and hostname list are explicit even when they are `null` and `[]`.
There is no setup-schema selector or older bootstrap variant.

The work-claim request and runtime identity are defined in
`agent_protocol/claims.py`. Protocol 3, capabilities, node identity, lease,
wait time, and the enrolled agent's observation key are required. The Rust
HTTP transport and the connected test use the same request serializer and
capability declaration. Enrollment keeps its bounded raw-body security
handling, while OpenAPI exposes the exact `EnrollmentSubmitRequest` used to
validate that body.

`scripts/generate-control-clients` derives the Controller OpenAPI document and
Python/TypeScript clients from the actual API. Never fix drift by hand-editing
generated clients or weakening their schema. Rust wire compatibility requires
tests that serialize actual producer values and pass them to the other
language's real parser and validator, in both directions.

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

## Required launch checks

The `Controller and Spark wire contract` CI job checks both sides of the
launch boundary. It runs when the Controller, agent, public-contract lock,
recipe revision, runtime compiler, or harness configuration changes.

- `scripts/tests/check_recipe_launch_contracts.py` loads every published Model
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
  actual API-issued grant through the Rust verifier and receipt signer and
  verifies that receipt through the Python contract. The complete persisted
  signed-observation workflow has its own integration check; a signature
  round trip alone does not establish run readiness. No old heartbeat response
  shape is accepted.
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

The 7 September 2026 application inventory contains 136 routes. Its 115
response models cover JSON responses; the other 21 routes handle empty
responses, raw uploads/downloads, signed files, metrics, or event streams.
OpenAPI is generated from the actual application and its nested Pydantic graph.

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
| Cache manifests, download/repair/eviction payloads and results | `model_cache_contract.py`, selected by operation kind |

A required nullable value remains present; unused optional fields are omitted.
Malformed stored documents produce a controlled error instead of becoming empty
state. Engine-owned extension values retain their declared flexibility. Unused
parallel copies have no persistence contract: remove their column and writer.

Structure validation does not replace transaction semantics. Cache workers
refresh the database row when acquiring its lock, so a prior cooldown scan
cannot hide another worker's newly committed claim. The PostgreSQL regression
forces that interleaving and verifies that workers claim different operations.

## Discovered HTTP completeness gate

`scripts/tests/check_api_contract_completeness.py` constructs both supported
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
uv run --project control --frozen --with-editable . pytest -q control/tests \
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
