# Agent-Based Control Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the control plane decompose repository plans into node-agent operations and eliminate routine SSH execution from the production worker.

**Architecture:** Reconciliation is a persisted orchestration state machine. It validates an eligible Git commit, withdraws affected routes, emits dependency-ordered node operations, consumes fenced results, performs acceptance, and atomically publishes routes.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy/PostgreSQL, existing placement/profile contracts, pytest

## Global Constraints

- Production routine operations use only `AgentJobService`; no subprocess, SSH, SCP, or direct agent connection is available to the worker.
- Plans pin commit, targets, placements, releases, routes, input digests, operation graph, agent protocol range, and plan digest.
- A failed/unavailable/revoked/incompatible node leaves affected routes withdrawn.
- Agent operations are safe under lease expiry through explicit inspection, compensation, or operator-wait states.
- Legacy SSH transport is accessible only through an explicitly selected compatibility entry point outside production settings.

### Clean-slate boundary (2026-08-18)

The Fleet/Library cleanup supersedes the old package/deployment control-plane
follow-on. Control-plane reconciliation now accepts only retained Fleet,
Library, and Spark-agent workload operations. Legacy package/deployment
operation identifiers, rollout payloads, and deployment materialization records
are deliberately rejected instead of translated or compatibility-wrapped.

---

### Task 1: Persisted reconciliation operation graph

**Files:**
- Create: `control/src/vonk_control/orchestration.py`
- Modify: `control/src/vonk_control/models.py`
- Create: `control/migrations/versions/0005_reconciliation_graph.py`
- Test: `control/tests/test_orchestration.py`

**Interfaces:**
- Produces `OperationNode`, `OperationGraph`, `ReconciliationOrchestrator.plan`, `advance`, `cancel`.
- Graph nodes contain operation ID, node ID, kind, dependencies, compensation kind, and exact payload digest.

- [ ] **Step 1: Write failing deterministic graph tests**

```python
def test_workers_start_before_entrypoint_and_stop_after_it(planner) -> None:
    graph = planner.plan(distributed_plan())
    assert graph.dependencies("head:start") == ("worker:start",)
    assert graph.dependencies("worker:stop") == ("head:stop",)
    assert graph.digest == planner.plan(distributed_plan()).digest
```

- [ ] **Step 2: Run and observe missing graph**

Run: `uv run --project control pytest control/tests/test_orchestration.py -v`
Expected: FAIL importing orchestration.

- [ ] **Step 3: Implement graph contracts and persistence**

Persist graph JSON/digest, current phase, route-withdrawal generation, and
terminal reason on `reconciliations`; add dependency rows or canonical JSON as
one immutable graph document. Reject cycles, unknown targets, duplicate
operations, cross-workload ordering errors, and operations absent from the
agent registry. Deterministically sort independent nodes by canonical ID.

- [ ] **Step 4: Run graph and migration tests**

Run: `uv run --project control pytest control/tests/test_orchestration.py control/tests/test_agent_migrations.py -v`
Expected: PASS.

- [ ] **Step 5: Commit graph**

```bash
git add control/src/vonk_control/orchestration.py control/src/vonk_control/models.py control/migrations/versions/0005_reconciliation_graph.py control/tests/test_orchestration.py
git commit -m "feat: persist agent reconciliation graphs"
```

### Task 2: Repository-to-agent plan resolver

**Files:**
- Create: `control/src/vonk_control/desired_state.py`
- Modify: `control/src/vonk_control/reconcile.py`
- Test: `control/tests/test_desired_state.py`
- Test: `control/tests/test_reconcile.py`

**Interfaces:**
- Produces `DesiredStateResolver.resolve(commit, profile_id, observations) -> ReconciliationPlan`.
- Consumes fleet/topology V2, workload/profile definitions, placement planner, immutable release manifests, and ready agent capabilities.

- [ ] **Step 1: Write failing one/two/sixteen-node resolution tests**

Test exact document hashes, missing references, stale observations, insufficient
capacity, incompatible agent versions, unsupported operations, and stable
placement under reordered repository documents.

- [ ] **Step 2: Run and observe current static reconciliation document limitation**

Run: `uv run --project control pytest control/tests/test_desired_state.py control/tests/test_reconcile.py -v`
Expected: FAIL because production planning only reads `inventory/reconciliation.json`.

- [ ] **Step 3: Implement derived desired-state planning**

Read exact commit objects through `RepositoryService`; validate V2 schemas and
cross-references; convert DB observations to `NodeObservation`; run
`PlacementPlanner`; resolve release and route inputs; require connected agent
protocol/capability compatibility; emit a complete operation graph. Keep static
reconciliation documents only as a test fixture/explicit compatibility format.

- [ ] **Step 4: Run resolver, placement, and repository tests**

Run: `uv run --project control pytest control/tests/test_desired_state.py control/tests/test_reconcile.py control/tests/test_repository.py -v && uv run pytest tests/cluster_profiles/test_placement.py -v`
Expected: PASS.

- [ ] **Step 5: Commit resolver**

```bash
git add control/src/vonk_control/desired_state.py control/src/vonk_control/reconcile.py control/tests/test_desired_state.py control/tests/test_reconcile.py
git commit -m "feat: derive agent plans from repository state"
```

### Task 3: Fail-closed route and operation orchestration

**Files:**
- Modify: `control/src/vonk_control/orchestration.py`
- Modify: `control/src/vonk_control/routes.py`
- Modify: `control/src/vonk_control/litellm.py`
- Test: `control/tests/test_agent_reconciliation.py`

**Interfaces:**
- Orchestrator enqueues only dependency-ready operations and advances from persisted state after each terminal result.
- Route publisher consumes accepted endpoint evidence only after graph verification succeeds.

- [ ] **Step 1: Write failing lifecycle/fault tests**

Test route withdrawal precedes first mutation; release install precedes prepare;
worker start precedes head; all health/verify results precede publication;
disconnect/revocation/stale fence/bad evidence leave maintenance; retry after
restart does not duplicate completed mutation.

- [ ] **Step 2: Run and observe missing agent-driven lifecycle**

Run: `uv run --project control pytest control/tests/test_agent_reconciliation.py -v`
Expected: FAIL because current runtime handler shells to `vonkctl`.

- [ ] **Step 3: Implement persisted advancement and compensation**

Within transactions, find dependency-ready nodes, enqueue one node operation
each, and record IDs. When results arrive, validate evidence digest and advance.
On start/verify failure enqueue stop compensation for successfully started
members; on uncertain mutation enter `waiting-for-operator` after inspection.
Publish `RouteCandidate` and LiteLLM policy atomically only after complete
acceptance. Record bounded audit/reconciliation summary.

- [ ] **Step 4: Run lifecycle and route tests**

Run: `uv run --project control pytest control/tests/test_agent_reconciliation.py control/tests/test_routes.py control/tests/test_litellm.py -v`
Expected: PASS.

- [ ] **Step 5: Commit orchestration**

```bash
git add control/src/vonk_control/orchestration.py control/src/vonk_control/routes.py control/src/vonk_control/litellm.py control/tests/test_agent_reconciliation.py
git commit -m "feat: reconcile cluster through outbound agents"
```

### Task 4: Remove SSH from production worker wiring

**Files:**
- Modify: `control/src/vonk_control/worker.py`
- Delete: routine subprocess behavior from `control/src/vonk_control/runtime.py`
- Create: `control/src/vonk_control/legacy_runtime.py`
- Modify: `control/src/vonk_control/settings.py`
- Test: `control/tests/test_production_worker.py`
- Test: `control/tests/security/test_no_routine_ssh.py`

**Interfaces:**
- Production worker registry contains orchestration/maintenance tasks only and emits agent operations.

- [x] **Step 1: Write failing production-boundary tests**

Patch `subprocess.run/Popen` and transport constructors to raise if production
worker handles probe/reconcile. Assert operations are inserted in the database
instead. Assert production settings reject the legacy selector and no automatic
fallback occurs when agents are offline.

- [x] **Step 2: Run and confirm current SSH subprocess handler fails test**

Run: `uv run --project control pytest control/tests/test_production_worker.py control/tests/security/test_no_routine_ssh.py -v`
Expected: FAIL because `RuntimeHandlers` invokes repository scripts.

- [x] **Step 3: Wire agent orchestrator and isolate legacy implementation**

Move direct handlers to `legacy_runtime.py`; do not import it from production
API/worker modules. Production `Worker` advances persisted reconciliations and
performs housekeeping only. Agent HTTP claims execute node work. Make settings
reject compatibility transport in `production` deployment mode.

- [x] **Step 4: Run worker, job, and security suites**

Run: `uv run --project control pytest control/tests/test_production_worker.py control/tests/test_worker.py control/tests/test_jobs.py control/tests/security/test_no_routine_ssh.py -v`
Expected: PASS.

- [x] **Step 5: Commit transport cutover**

```bash
git add control/src/vonk_control/worker.py control/src/vonk_control/runtime.py control/src/vonk_control/legacy_runtime.py control/src/vonk_control/settings.py control/tests/test_production_worker.py control/tests/security/test_no_routine_ssh.py
git commit -m "refactor: remove routine SSH from control worker"
```

### Task 5: Metrics and operational visibility

**Files:**
- Modify: `control/src/vonk_control/metrics.py`
- Modify: `control/src/vonk_control/dashboard.py`
- Modify: `deploy/compose/prometheus/alerts.yaml`
- Modify: `deploy/compose/grafana/dashboards/fleet.json`
- Test: `control/tests/test_agent_metrics.py`
- Test: `deploy/compose/tests/test_observability.py`

**Interfaces:**
- Adds bounded labels for agent state/version compatibility, certificate expiry, operation counts, lease age, rollout state, and last-seen age.

- [ ] **Step 1: Write failing cardinality and alert tests**

Assert labels use node ID, operation enum, state, and version bucket only; never
job IDs, certificates, addresses, errors, actors, or payload content. Require
alerts for stale agents, expiring certificates, repeated failures, and rollout
pause with runbook links.

- [ ] **Step 2: Run and observe absent metrics**

Run: `uv run --project control pytest control/tests/test_agent_metrics.py -v && uv run pytest deploy/compose/tests/test_observability.py -v`
Expected: FAIL new assertions.

- [ ] **Step 3: Implement metrics/dashboard projections and alerts**

Read operational tables through aggregate queries, normalize version to
supported/old/new/incompatible, and keep errors only in redacted job logs.
Update fleet response with last seen, certificate expiry, and compatibility.
Host/GPU measurements are consumed from the standard node/DCGM exporter series
remote-written by Alloy; do not add a second custom metrics collector to the
agent protocol.

- [ ] **Step 4: Run Phase 4 verification**

Run: `uv run --project control pytest control/tests/test_desired_state.py control/tests/test_agent_reconciliation.py control/tests/test_production_worker.py control/tests/test_agent_metrics.py -q && uv run pytest deploy/compose/tests/test_observability.py -q && git diff --check`
Expected: all pass.

- [ ] **Step 5: Commit visibility**

```bash
git add control/src/vonk_control/metrics.py control/src/vonk_control/dashboard.py deploy/compose/prometheus/alerts.yaml deploy/compose/grafana/dashboards/fleet.json control/tests/test_agent_metrics.py deploy/compose/tests/test_observability.py
git commit -m "feat: observe outbound GPU node agents"
```

---

## Final Task 3 review repair wave (2026-08-05)

**Goal:** Close the final reconciliation, publication, supervisor, lifecycle, and
production-authority findings without implementing Task 4 SSH removal.

**Architecture:** PostgreSQL remains the authority for operation and publication
state. A locked singleton publication-owner row selects the newest accepted plan;
per-reconciliation rows carry durable withdrawal/publication/cancellation intent,
and a bounded filesystem request/ack channel proves the live LiteLLM process has
stopped or started the exact marker before database phases advance. Agent claims,
results, sweeps, and publication all revalidate the pinned plan and linked Job.

**Global constraints:** Preserve sorted-target-first locking and
Node -> Certificate -> Presence -> Operation -> Attempt. Use actual PostgreSQL
critical-section tests for concurrency. Presence supplies address/freshness only.
Do not weaken persisted-plan/evidence validation and do not remove SSH or worker
cluster egress in this task.

### Repair 1: Canonical plan bytes and authoritative replay linkage

**Files:** `control/src/vonk_control/route_runtime.py`,
`control/src/vonk_control/desired_state.py`, and their focused tests.

- [ ] Add a planner-to-`AtomicRouteBundlePublisher` regression whose quota digest
  comes only from `DesiredStateResolver`; run it and record the exact digest RED.
- [ ] Make quota verification use the same `canonical_message()` bytes as planning;
  retain newline-terminated canonical files only at the filesystem boundary.
- [ ] Add corruption/replay regressions for exact `Job.reconciliation_id`, including
  missing JSON hint, mismatched hint, duplicate/corrupt link; remove payload-based
  reconciliation authority and run RED then GREEN.

### Repair 2: Transactional queue authority, quiescence, expiry, and fairness

**Files:** `control/src/vonk_control/agent_jobs.py`,
`control/src/vonk_control/agent_reconciliation.py`, `control/src/vonk_control/worker.py`,
and queue/reconciliation PostgreSQL tests.

- [ ] Add deterministic PostgreSQL multi-operation RED cases for agent-declared
  uncertainty and unsafe expiry with queued/running primary and compensation
  siblings; assert no later claim/result can mutate them.
- [ ] Join every reconciliation claim to its authoritative Job/reconciliation phase,
  validate the pinned protocol/capability/commit/node contract, and transactionally
  quiesce every role/attempt before operator wait.
- [ ] Add an autonomous expired-attempt sweep invoked by maintenance ticks, with a
  no-follow-up-claim RED/GREEN case.
- [ ] Add bounded database repoll to long claims and a cross-service-instance test;
  make `tick()` return `False` on no transition and alternate reconciliation/generic
  work so neither side starves.
- [ ] Add secret-like agent/operator reason regressions and redact before every
  durable terminal field.

### Repair 3: Singleton publication ownership and continuous eligibility

**Files:** migration `0009`, models, reconciliation service, production assembly,
and PostgreSQL publication/eligibility tests.

- [ ] Add a locked singleton current-owner row and distinct R1/R2 RED races/restarts:
  old completed renewal, newer noncompleted maintenance, newer completion, and old
  cancellation must never overwrite the newer owner.
- [ ] Revalidate active/revoked state, exact plan protocol and operation capability,
  repository node compatibility, current commit eligibility, presence address, and
  freshness at claim/result/publication and on every completed-owner maintenance
  pass. Bind production callbacks through `RepositoryService` and `GitPolicy`.
- [ ] Add post-completion revocation/address/incompatibility/ineligible-commit RED
  cases that withdraw immediately rather than waiting for lease renewal.

### Repair 4: Durable live LiteLLM request/ack and cancellation intent

**Files:** route runtime, reconciliation persistence/service,
`deploy/compose/litellm/config_supervisor.py`, and supervisor/runtime tests.

- [ ] Add RED crash/restart cases around request staging, marker replacement,
  supervisor stop/start, exact ack, and database acknowledgement.
- [ ] Persist cancellation/withdrawal intent before any marker side effect; resume it
  after crashes and never republish a cancelled completed plan.
- [ ] Require an exact bounded supervisor ack before leaving withdrawal-pending or
  completing publication. Failed validation/start/timeout/crash stays maintenance;
  the live supervisor enforces lease expiry at or before the deadline.
- [ ] Add an RBAC/audited/idempotent production cancellation route that calls only
  `AgentReconciliationService.request_cancel`; deprecate the legacy orchestrator
  cancellation entry point for production callers.

### Repair 5: Production lifecycle acceptance

**Files:** `scripts/accept-platform-lifecycle` and
`tests/e2e/test_platform_lifecycle.py`.

- [ ] Make lifecycle E2E RED on the manual orchestration/legacy publisher path.
- [ ] Drive both initial and A-to-B loops through bound result consumption,
  dependency-wave ticks, real claims/results, authenticated address presence, and
  `AtomicRouteBundlePublisher` plus supervisor acknowledgement. Keep durable replay,
  inference, and withdrawal evidence; remove manual phase/Job mutations.

### Repair 6: Compose least privilege

**Files:** Compose, route initializer, supervisor entrypoint, and Compose tests.

- [ ] Add rendered-config RED assertions for LiteLLM UID/GID, `cap_drop: ALL`,
  `no-new-privileges`, read-only root, bounded writable tmp/ack path, and route volume
  ownership; retain secrets and network segmentation.
- [ ] Implement matching initializer ownership and non-root service settings, then
  run supervisor and rendered Compose tests GREEN.

### Repair 7: Verification and evidence

- [ ] Run focused RED/GREEN commands after each repair, then full control tests,
  actual PostgreSQL races/migration cycles, supervisor/Compose tests, lifecycle E2E,
  shared agent tests if contracts changed, Ruff 0.16.1, compileall, Bash syntax,
  JSON/YAML/schema validation, and `git diff --check`.
- [ ] Append exact evidence and the final finding-by-finding self-review to the Task 3
  report and feature-branch progress ledger; stage only Task 3 files and commit the
  cohesive repair wave without claiming review readiness.
