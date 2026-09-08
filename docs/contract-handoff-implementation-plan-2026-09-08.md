# Contract handoff implementation plan

Status: implementation in progress. This plan closes every finding in the
[8 September audit](contract-handoff-audit-2026-09-08.md), including the user's
clarification that Rust wire structures must be generated with typify.

## Integration checkpoint — 8 September

The four contract branches are consolidated in the platform integration branch,
including the final transport fixes (`f0308ead`). The combined locked Rust
workspace compiled all 40 test executables; the focused protocol, helper,
transport, identity and setup suites passed 168 tests in OrbStack ARM64.
Python canonical normalization passed 409 focused tests at its checkpoint.
These results describe those checkpoints, not a final release candidate.

Native datetime parity (`efc72d1a`) is now integrated: 39 Python tests,
including 14 actual Python/Rust serialization cases, and 10 focused Rust tests
passed. Formatted strings retain their original representation.

Controller API/SSE serialization is integrated (`e2adb102`), including nested
models, typed mappings, required nulls and injected response metadata. The
production queue now participates in both passing connected job regressions
(`5540fb2f`). Lease-only heartbeats and the generated helper response are
integrated (`997b142a`, `1e13c5cb`, `eda339cd`).

The partial-receipt audit found no defect: those internal fragments preserve
field presence so an absent measurement does not become an observed zero.
Strict persistence and public model serialization retain their respective roles.

Remaining work has explicit owners:

- Runtime owner: verify the final combined Linux protocol/helper/agent suites.
- Parent: finish both LTX adapters using shared input and compiled-plan models,
  refresh their bundled protocol wheel and recipe packages, and validate the
  complete catalog against the final platform.
- Parent: regenerate clients, schemas, wheels, locks and supply-chain evidence
  after source changes settle; verify a second generation is unchanged.
- Canary owner: replay the local connected lifecycle after combined artifacts
  are available. The earlier Controller-startup timeout is not a passing run.

Final integration, green PR CI, publication, deployment and physical Spark
acceptance remain outstanding. Do not mark the audit complete solely because
individual branch tests pass.

## Contract decisions

1. Pydantic owns shared document structure. Export JSON Schema from those
   models; use typify to generate Rust wire structures. Keep semantic and
   execution/security validation in handwritten code attached to generated
   types. Do not maintain parallel field declarations or hand-edit outputs.
2. Preserve required-versus-nullable fields, scalar types, union variants and
   unknown-field policy through generation. If generated Rust types alone do
   not enforce a schema constraint, enforce the exported schema at the wire
   boundary. Test real Python and Rust serializers in both directions.
   Optional nullable fields with default `None` accept missing or null and are
   omitted on output. Both sides normalize through the authoritative model
   before hashing/signing/verifying. Keep required nulls, false/zero/empty
   values and engine-defined nulls. Do not force optional fields to required
   to conceal generation drift; preserve formatted string bytes.
3. A job carries the Controller-compiled invocation, including its effective
   settings. Rust executes the signed canonical plan; it never recompiles
   engine settings or ignores an accepted parameter. Bind stable installed
   recipe/model/image/security identity separately from invocation argv and
   its resulting execution identity.
4. Telemetry consumes retained canonical runtime identity. The compiler must
   carry explicit engine/metrics metadata needed by the collector. Do not
   guess the engine from an executable or invent public endpoints for workers.
5. Job input metadata and user input files have an explicit contract. Adapters
   consume the manifest's declared inputs, not arbitrary directory enumeration.
6. Failure evidence remains in its canonical source model through persistence
   and Activity. Compose agent, Controller and availability evidence instead
   of squeezing all three through an incompatible subset. Preserve useful
   recovery actions, retry timing and capacity information; sanitize secrets.
7. Generated browser types cover ordinary JSON and fixed SSE envelopes. Raw
   downloads retain their exact bytes and declare their actual media type.
8. No legacy readers, coercive persistence defaults, migration paths or
   duplicate engine-argument implementations are part of the solution.

## Work packages and exclusive ownership

All new platform branches start from refreshed `origin/main` (`1b178ebe`),
then whole-merge integration checkpoint `7bca86ee` so completed fixes are
present. No cherry-picks. Each agent has one isolated branch and worktree.

| Package | Owner | Branch | Scope and acceptance |
| --- | --- | --- | --- |
| Job invocation and shared runtime metadata | `active_path_policy_audit` | `fix/contract-job-invocations` | Audit 2. Controller compilation, job schema, runtime handoff. API-selected seed must reach actual adapter invocation; wrong image/model/security identity rejected. Owns shared compiled-plan schema changes requested by telemetry. |
| Rust generation | `cache_mount_contract_audit` | `fix/pydantic-typify-wire` | Deterministic Pydantic export, pinned typify generator, generated Rust adoption, stale-output CI check. No competing active wire DTOs. Coordinate adoption of runtime files after job owner's field freeze. |
| Runtime telemetry | `design_review` | `fix/canonical-runtime-telemetry` | Audit 1. Collector and telemetry tests. Production writer output must produce correct run/engine metrics at actual published address; no worker public scrape. Requests shared schema edits through job owner. |
| Failure persistence and Activity | `python_contract_scan` | `fix/canonical-failure-evidence` | Audits 4–6. Real persisted agent/cache failures survive API projections; malformed stored structures rejected. Includes queued retry/recovery parity. |
| Generated web clients and SSE | `approved_controller_packaging` | `fix/generated-web-stream-contracts` | Audits 7 and 8 SSE. Generated upgrade/audit aliases and calls; remove unused DTOs; canonical SSE envelopes emitted and consumed by browser. |
| Recipe adapters, download schemas and integration | Parent | Integration branch plus isolated recipe branch | Audit 3; audit 8 byte responses; failure UI adaptation; regeneration, wheel/lock/SBOM refresh, connected regressions, review, merge and cleanup. |
| Connected canary and observation grace | `publication_start_diagnosis` | Existing `fix/helper-installation-write-boundary` | Fix newly reproduced singleton START deadline producer; strict worker deadline handling remains. Replay real local Controller/agent serving lifecycle and retain truthful evidence. |

The descriptive agent names are existing session identifiers, not additional
scope. Prior completed agent assignments do not authorize unrelated edits.

## Coordination and integration order

- Job owner establishes the new invocation shape and telemetry fields with
  telemetry/generation owners. Generator infrastructure can proceed in
  parallel; adoption of those Rust runtime declarations waits for their shape
  to settle. One owner edits each shared source file at a time.
- Failure and SSE/web branches are independent. Parent owns the final generated
  OpenAPI, Python and TypeScript outputs, protocol wheels/locks and supply-chain
  manifest so agents do not create conflicting generated snapshots.
- Parent reviews each branch's final diff and behavioral evidence, merges the
  entire branch, then reruns affected connected checks against the integrated
  tree. No completed changes remain stranded in partial worktrees.
- Build the recipe catalog after adapter source changes; refresh the platform
  recipe revision only after the recipe branch is published. Structural
  validation covers every recipe against the integrated platform, not a subset.
- Regenerate every derived contract artifact from the final authoritative
  models and prove a second generation is clean. Check generated Rust adoption
  coverage explicitly, rather than claiming completion because a generator
  exists beside handwritten DTOs.

## Validation matrix

| Handoff | Required regression |
| --- | --- |
| Pydantic → schema → typify → Rust | Required-nullable omitted versus null, exact scalar types, union variants, unknown-field policy, current producer values and Rust serialized values. |
| Canonical serialization → digest/signature verification | Optional default-null missing and null yield identical canonical bytes; output omits both. Required nulls and engine nulls remain. Actual Python-signed documents verify in Rust, and Rust output verifies in Python; formatted strings retain their declared representation. |
| Job API → compiler → agent → invocation | Installed seed differs from selected seed; actual invoked argv reflects selection, including serialized compound values; immutable installed identity stays bound. |
| START → retained runtime → telemetry | Current writer, real retained document, expected engine/rank and actual metrics address. Include worker/no endpoint and unsupported metrics. |
| Staged job inputs → LTX adapter | Declared prompt plus agent manifest succeeds; absent/extra/unsafe or malformed declared input fails appropriately. Both active adapters covered. |
| Worker failure → persistence → Activity | Reason-only, long valid summary, dotted code, actionable HF access, rate-limit cooldown and capacity failure retain evidence. |
| Persisted malformed cache failure | Invalid boolean, missing required identity and malformed timing/capacity cannot become valid defaults. |
| API/SSE → generated browser types | Nullable upgrade/audit fields, actual snapshot/change envelopes and recovery evidence rendering. |
| Raw downloads → OpenAPI | Correct tar/octet-stream/text success declarations; signed TUF JSON bytes unchanged. |
| Singleton START → initial observation | Fresh grace initialized at successful producer, normal initial receipt accepted; genuinely missing/expired deadline remains failure. |

Use OrbStack for Linux/container behavior and PostgreSQL-backed tests. Run
focused suites during implementation, then the integrated Controller, agent,
protocol/helper, generated-client and recipe checks relevant to the changes.
Do not replace runtime behavior with assertions about documentation text or
matching handwritten fixture shapes.

## Done means

All audit items have an integrated fix, regression evidence, and recorded
disposition. Typify-generated structures are actually used at shared Rust wire
boundaries; there are no parallel active definitions. PR CI is green on the
final commit. Recipe publication, signed platform publication, Controller
deployment and physical Spark acceptance are reported separately; a local
engineering replay with patched binaries is not signed-candidate acceptance.
