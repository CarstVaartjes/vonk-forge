# Seven retired agent operations: removal review

Snapshot reviewed: platform commit `6651e420b`. This document is a review
packet only. It does not apply the blocked deletion.

## Decision requested

Remove these seven Python-era operation names from the Controller and shared
protocol:

- `node.probe`
- `release.install`
- `workload.prepare`
- `workload.start`
- `workload.stop`
- `workload.health`
- `workload.verify`

The sole current agent is Rust. It neither advertises nor accepts these names.
No other agent implementation exists in this repository. The only producer is
the old schema-1 reconciliation graph. That graph has no enabled public creation
route: `POST /api/v1/jobs` is hidden and disabled by default, and rejects
reconciliation requests even when generic jobs are enabled. Current Fleet,
Run/Switch, and Recipe lifecycle services create the operations the Rust agent
does accept.

The removal is breaking for any persisted old reconciliation graph. This is a
greenfield deployment, so the proposed implementation rejects or removes that
old state instead of adding a reader, alias, migration shim, or second enum.

## Complete old path and current owner

| Concern | Old producer, router, or consumer | Current owner after removal |
| --- | --- | --- |
| Operation vocabulary and payload/result shapes | `agent_protocol/src/vonk_agent_protocol/contracts.py`: `AgentOperation`, seven payload classes, node-probe result classes, payload/result registries, and schema mutation code | The same registry contains only the Rust-advertised upgrade, artifact, build/import, install/start/stop/uninstall/cleanup, inventory, package, and job operations. Every retained operation has one Pydantic payload and result type. |
| Graph authoring and persisted barrier validation | `control/src/vonk_control/orchestration.py`: `ReconciliationOrchestrator`, graph parsing, `_IMPLEMENTED_OPERATIONS`, and `validate_persisted_resolved_plan` stop/install/gate barriers | `RunSwitchOperationService` produces a digest-bound current plan. `RecipeOperationService` rechecks that digest and locks the affected run or installation before enqueueing each current operation. |
| Old graph scheduler and result consumer | `control/src/vonk_control/agent_reconciliation.py`: primary and compensation dispatch, evidence checks, publication ownership, cancellation, and result-driven graph advancement | `control/src/vonk_control/recipe_operation_worker.py`, `recipe_operations.py`, `run_switch_operations.py`, and `fleet_profiles.py` advance current install/start/stop and Run/Switch state. Exact recipe result consumers update `InstallationNode` and `RunNode`. |
| Agent queue admission and claim | `control/src/vonk_control/agent_jobs.py`: seven capability sets, automatic-reclaim set, `node.probe` result conversion, reconciliation authority checks, and the `Job.reconciliation_id` target-lock branch | The generic queue, attempt fence, lease, parent aggregation, and current operation result consumers remain. Current recipe mutations lock `RecipeInstallation`/`RecipeRun`, bind `mapping_generation` and `plan_digest`, and use resource reservations. |
| Public or internal routing | `control/src/vonk_control/api.py`: disabled `POST /api/v1/jobs`, reconciliation result binding, reconciliation authority callback, and worker-authority route installation | Keep `GET /api/v1/jobs/{id}`, progress, log, resume, and cancellation routes used by current operations. Current typed Fleet, Run/Switch, Recipe, and Agent routes remain. Remove the disabled generic POST and its request-only DTO/client registration. |
| Old worker process | `control/src/vonk_control/worker.py`: `AgentReconciliationService` construction/tick and `HttpWorkerAuthority`; `control/src/vonk_control/worker_authority.py`: internal reconciliation authority protocol | `RecipeOperationWorker` and Controller-owned route publication advance current work. The old worker authority has no non-reconciliation caller and can be removed with its internal routes and tests. |
| Node health projection | `agent_jobs.py`: successful `node.probe` parses memory/storage/accelerator evidence, optionally requires zero active NVIDIA compute processes, and records `Observation(kind="health")`; `dashboard.py`, `operation_api.py`, and `api.py` expose that explicit probe state | Authenticated inventory and strict telemetry own current node facts. `run_admission.py` rejects stale inventory and insufficient post-start memory. Fleet readiness comes from exact signed per-rank observations. See the explicit safety difference below. |
| Route evidence | `agent_reconciliation.py` constructs `workload.verify` operation IDs; old `route_runtime.py` publication expects those IDs | `recipe_routes.py` validates current run/mapping/rank observations, requires every rank's `observation_endpoint_ready`, locks the singleton route owner, and calls `route_runtime.publish_compiled`. |
| Metrics and UI vocabulary | `control/src/vonk_control/metrics.py`, `dashboard.py`, `operation_api.py`, generated OpenAPI/clients, and web client code still name old operations/probe fields | Generate clients from the current typed API after the old routes/fields are removed. Current inventory, telemetry, lifecycle, and readiness fields remain. |

## Safety differences that require an informed decision

### Foreign NVIDIA-process gate

The old `node.probe` success branch can reject a result unless
`active_nvidia_compute_processes == 0`. Current Run admission checks fresh
authenticated inventory, declared memory, existing reservations, and the
post-start memory floor. It does not contain an equivalent categorical
"zero foreign NVIDIA process" rule.

Removing the old branch therefore removes that exact policy from source. It
does not disable a working enforcement path today: the Rust agent rejects
`node.probe`, so the Controller cannot obtain this result from the current
agent. If zero foreign processes is still desired policy, it should be added as
a typed current inventory/admission rule with an actual Rust producer and a
producer-to-consumer test. It should not remain as an unreachable legacy
operation.

### Reconciliation target lock

`AgentJobService._claim_once` performs the rejected target-lock query only when
the parent `Job.reconciliation_id` is non-null. That identity belongs to the old
graph. Current Recipe jobs are fenced independently: the service locks the
specific installation or run row, revalidates the accepted plan digest and
mapping generation, holds typed resource reservations, and serializes route
publication on `RoutePublicationOwner`.

The removal must delete the old graph callers and `Job.reconciliation_id` claim
branch together. It must retain the generic claim lock, operation attempt
fence, recipe row locks, plan/generation checks, resource reservations, and
route-owner lock. The patch must include concurrency regressions showing that
two current Run/Switch operations cannot both acquire one target and that a
stale plan cannot enqueue.

### Persisted stop/install/node-gate barriers

`validate_persisted_resolved_plan` validates sequencing inside the old graph.
It is called only by `ReconciliationOrchestrator` and
`AgentReconciliationService`. Deleting only its barrier blocks would be unsafe;
the coherent change deletes the producer, parser, scheduler, and consumer of
that graph as one unit.

The replacement is the current Run/Switch planner plus apply-time revalidation:
stop conflicts before start, require an installed current plan, reserve memory
and ports, run distributed rank launch before collective readiness, and publish
routes only after exact signed readiness. Those controls stay in their current
services and tests.

### Route authority persistence

Do not delete the `Reconciliation` row type or current route-publication
authority merely because their names predate Recipe routes. `recipe_routes.py`
uses a synthetic `Reconciliation` row for the current compiled route authority.
Keep `Reconciliation`, `RoutePublicationOwner`, current `RoutePublication`
records, the owner lock, activation marker validation, and
`publish_compiled`/compiled withdrawal.

Old `ReconciliationOperation`, compensation/cancellation graph state, old
`RouteBundleRequest`/`AcceptedEndpointEvidence`, and old `publish`/`withdraw`
paths can be deleted only after their callers are removed. Historical migration
bytes may remain inert; current runtime code must not read or write the retired
graph.

## Exact coordinated patch boundary

### Delete

- `control/src/vonk_control/orchestration.py`
- `control/src/vonk_control/agent_reconciliation.py`
- `control/src/vonk_control/worker_authority.py`, after removing its API and
  worker constructors
- Old graph suites: `control/tests/test_orchestration.py`,
  `test_agent_reconciliation.py`, `test_agent_reconciliation_postgres.py`, and
  `test_worker_authority.py`
- Reconciliation-specific cases from
  `control/tests/test_agent_queue_reconciliation.py`
- The hidden disabled `POST /api/v1/jobs` handler, its request-only DTO, docs,
  generated client operation, and tests. Retain current job reads, progress,
  logs, resume, and cancel.

### Remove scoped regions and replace tests

- `agent_protocol/src/vonk_agent_protocol/contracts.py`: seven enum members,
  payload/result types, registries, exports, and schema mutation branches.
- `control/src/vonk_control/agent_jobs.py`: seven capability/control/reclaim
  entries, node-probe conversion, and reconciliation-only authority/target-lock
  code. Retain queue fencing, leases, current result validation, parent
  aggregation, and notifications.
- `control/src/vonk_control/api.py`: reconciliation imports, consumer binding,
  authority callback/service/routes, and disabled generic job POST.
- `control/src/vonk_control/worker.py`: reconciliation tick/service and old HTTP
  authority construction. Retain `RecipeOperationWorker`.
- `control/src/vonk_control/route_runtime.py`: only old evidence types and
  workload-verify publication/withdrawal. Retain compiled route support and
  locks.
- `control/src/vonk_control/dashboard.py`, `operation_api.py`, and `metrics.py`:
  old node-probe fields and seven labels. Project current inventory/telemetry/
  readiness instead of aliases.
- Current tests that use a retired operation merely as a queue fixture must use
  a real current typed operation such as `recipe.stop`; keep the queue behavior
  assertion.

### Retain

- `AgentJobService` generic queue, claim, lease, attempt fence, result ingress,
  current consumers, and failure sanitization.
- Recipe install/start/stop/uninstall/model-cleanup, Run/Switch, Fleet profile,
  recovery, inventory, telemetry, and exact observation services.
- `Reconciliation` and current compiled route-authority persistence until that
  deliberately reused substrate is separately replaced.
- Recipe row locks, plan and mapping-generation fences, resource reservations,
  route-owner lock, activation acknowledgement, and signed readiness checks.

## Required proof before merge

1. Send all seven strings through the real `AgentJobService.enqueue_in_session`
   boundary. Each must fail before insertion and notification.
2. Insert a stale unsupported operation directly as database state and prove a
   current agent cannot claim it.
3. Use the built Rust capabilities command and assert exact equality with the
   Python current operation set. Remove the seven-operation exception from
   `tests/acceptance/test_rust_agent_parity.py`.
4. Run the current connected producer-to-Rust-to-Controller bridges for
   install/start, exact signed observation, stop, uninstall/model cleanup,
   upgrade, and artifact distribution.
5. Preserve meaningful queue tests by replacing retired fixture operations with
   current typed operations; verify lease expiry, attempt fences, stale result
   rejection, parent aggregation, and notification behavior.
6. Prove stale inventory and insufficient-memory Run admission still fail.
   State explicitly that zero foreign NVIDIA processes is not currently an
   equivalent gate; either accept its removal as unreachable legacy behavior or
   implement a separately reviewed current typed policy.
7. Prove current concurrency: conflicting Run/Switch applies serialize, stale
   plan and mapping generation fail, and route publication remains protected by
   its singleton owner lock.
8. Run a source/schema scan proving none of the seven strings, old graph route,
   or old DTOs remain outside explicit rejection tests and historical evidence.
9. Regenerate and verify OpenAPI, Python clients, TypeScript types, wheels, and
   supply-chain manifests only after the source composition is complete.

The intermediate commit `5b80a7074` is not mergeable. It removes the shared
enum while leaving graph modules active with empty operation sets, and it maps
`waiting-for-operator` failures to `status="failed"` even though the current
Rust producer omits that status. The final implementation must be one composed
deletion, preserve waiting-state semantics, and pass the proof above.
