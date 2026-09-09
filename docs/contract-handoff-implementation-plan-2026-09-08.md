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

## Consolidated integration — 8 September

PR #629 is merged into `main` at `b99ef271`; all required PR checks passed.
The completed contract branches are included in that merge. Combined Linux
verification covered 488 Rust cases across the workspace run and the necessary
unprivileged setup reruns. The connected suite initially reported 656 passes,
six failures and 17 skips; all six failures passed focused reruns after fixture
and local probe-wrapper corrections. These are separate runs, not a claimed
single all-green connected-suite execution.

Both LTX adapters now consume the shared compiled-plan and job-input models.
The complete recipe catalog contains 85 recipes and 92 models. Recipe PRs #77
and #78 are merged; release `v1.0.6` is published from `6001adcb`, with its
publication workflow passing. The release uses an annotated Git tag; no
cryptographic release signature has been established.

Generated clients, schemas, protocol/public-contract wheels, locks and
supply-chain evidence were refreshed before the platform merge. A second
client generation left all 811 inspected files unchanged. Public-contract
packaging pins its build backend so production and local wheel bytes agree.

The routing follow-up is integrated in the PR #630 branch. Controller and
supervisor share finite transition budgets and a typed acknowledgement; initial
bootstrap also observes the first real activation correctly. The engineering
replay at `44839a5e`, with the corrected synthetic request fixture, reached
inference through the proxy, then stopped the runtime and withdrew its route.
Uninstall failed because the unprivileged agent could not remove helper-owned
runtime-cache directories. This is not a completed clean lifecycle replay.

Recipe PR #79 removes obsolete package-test skips. PR #80 makes the synthetic
acceptance server consume a nested Pydantic request model, accept declared
defaults and extensions, and serve both JSON and SSE. Its offline ARM64 image
passed probes as UID 10001 with a read-only root and no capabilities. These
fixture probes do not replace the final proxy-to-runtime replay.

A separate current source-build defect is fixed in `f12a34b8`: the producer
copied the child job state `running` into a receipt that requires the build
state `building`. The producer now constructs the canonical phase model from
the persisted build. A real build-service/queue/executor regression passes its
output to the actual receipt consumer for dispatch, retry and completion, and
rejects incomplete success evidence. The focused suite passed 36 tests. A fast
build can finish before the invalid intermediate state is observed, so an
earlier successful replay did not exclude this defect.

Remaining work has explicit owners:

- Helper owner: finish the signed installation-scoped runtime-cache cleanup
  across the shared contract, Controller, generated Rust and helper. Verify
  private directories, exact installation authority, outside-path isolation
  and idempotent retries; preserve shared model/image caches.
- Canary owner: rebuild production images and ARM64 agent/helper from the
  final packaged checkpoint. Run a clean source-image build, both JSON and SSE
  inference through the proxy, stop, uninstall, certificate renewal and cleanup.
- Parent: integrate tested follow-ups, regenerate derived contracts and
  packaged artifacts, and merge after green PR CI. Verify signed development
  publication, refresh the NAS bundle, upgrade Sparks through the Controller,
  and verify physical serving separately.

Signed platform publication, Controller deployment and physical Spark
acceptance remain separate evidence boundaries. The engineering replay does
not establish signed-candidate or physical hardware acceptance.

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

The original work packages started from refreshed `origin/main` (`1b178ebe`)
and whole-merged integration checkpoint `7bca86ee`. They are now consolidated
in `b99ef271`. Follow-up work uses the latest `origin/main` and existing owned
worktrees where possible. No cherry-picks or stranded completed branches.

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
