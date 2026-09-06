# Contracts across Python, Rust, and clients

The rule is **strict structure, extensible content**. Each shared document has
one authoritative definition. Consumers must preserve its meaning, not just
accept a similar-looking dictionary.

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

## Python

Import the canonical Pydantic model when consuming a shared document. Validate
at ingress, retain the typed value through the operation, and serialize through
that model at egress. API response validation matters as much as request
validation. Do not recreate a subset of Model or Recipe in a route, worker,
validator, or CLI.

Ordinary internal records and database tables can use dataclasses and ORM
models. A JSON contract document loaded from a database must still be parsed
with its canonical model before use. A database row is not proof that the
document satisfies the contract.

## Rust and generated clients

Rust uses `serde` structs and enums, with explicit semantic validation where
types alone are insufficient. The current Rust protocol definitions are
handwritten; they are not generated from Pydantic. Python protocol code also
contains dataclass contracts. Those facts must remain visible until the
implementations are consolidated; adding Pydantic elsewhere does not fix drift.

`scripts/generate-control-clients` derives the Controller OpenAPI document and
Python/TypeScript clients from the actual API. Never fix drift by hand-editing
generated clients or weakening their schema. Rust wire compatibility requires
tests that serialize actual producer values and pass them to the other
language's real parser and validator, in both directions.

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
