# Contracts across Python, Rust, and clients

The rule is **strict structure, extensible content**. Each shared document has
one authoritative definition. Consumers must preserve its meaning, not just
accept a similar-looking dictionary.

This is a greenfield contract. Do not retain old field aliases, alternate
document parsers, old-response fallbacks, positional compatibility constructors,
or default values that conceal malformed input. Fix the producer and consumer
together. Transport retries and partial progress updates remain supported by
their current contracts; neither requires accepting an older document format.

## Ownership

| Document | Authoritative definition | Consumers |
| --- | --- | --- |
| Published Model and Recipe | `vonk_forge_contracts.ModelDefinition` and `RecipeDefinition`, in `vonk-forge-recipes/contracts/src` | Catalog importer, Controller, compiler, authoring tools |
| Controller API requests and responses | Controller Pydantic request/response models, including the `*_contract.py` modules | FastAPI, generated OpenAPI, web and CLI clients |
| Controller–Spark messages | Shared `agent_protocol` wire contract | Controller and Rust `vonk-agent-protocol` |
| Run artifact verification | `ArtifactVerificationResult` in `run_switch_contract.py` | Cached/distributed artifact verification producers and Run/Switch consumer |
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

Ordinary internal records and database tables can use dataclasses and ORM
models. A JSON contract document loaded from a database must still be parsed
with its canonical model before use. A database row is not proof that the
document satisfies the contract.

## Rust and generated clients

Rust uses `serde` structs and enums, with explicit semantic validation where
types alone are insufficient. The current Rust protocol definitions are
handwritten; they are not generated from Pydantic. Job envelopes and signed
observations use the shared model graph and connected wire checks. The retired
operation graph removal is tracked in the launch progress document.
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

An artifact verification result must include `verified_build_id`. A source
build supplies the exact Controller build UUID; a published image supplies
explicit `null`. Omitting the field is malformed, and a different build UUID
cannot satisfy the requested run. The producer constructs
`ArtifactVerificationResult`, and the consumer validates the serialized result
through the same model before advancing the operation.

A passing model validation test proves document structure. A passing connected
lifecycle test proves the tested orchestration. Neither alone proves that every
model works on physical Spark hardware.
