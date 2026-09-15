# Approach to resilient Controller integrations

Status: approved direction; implementation and acceptance remain pending.

This is a high-level approach, not a task breakdown. The
[architecture overview](../architecture-overview.md) owns the strict state,
coordination, and recovery boundaries. Existing cache-restore and workload
recovery are the starting point. A documentation change does not establish
runtime conformance or deployed acceptance.

## Intended outcome

Every part of Vonk Forge works toward the latest authorized desired state.
Temporary failure, restart, delayed evidence, or a stale local view must not
lose accepted intent, discard useful completed work, or permanently block a
fresh authorized request. Once dependencies recover, reconciliation continues
without manual editing of database rows or status files.

Eventual consistency applies to observed progress, artifact availability,
execution, and derived views catching up with accepted intent. Permissions,
revocations, ownership, exact plan identity, and publication admission remain
strictly checked. A stale observation never authorizes an action.

## 1. Establish one owner for every fact

Keep PostgreSQL for transactional control decisions: identities, permissions,
catalog approval, saved profiles, desired state, request identity, job ownership,
cancellation, reservations, route decisions, audit, and retained telemetry.
Managed storage owns artifact bytes, verification manifests, and local
transfer/build checkpoints. Spark agents own their execution journals and
report authenticated evidence of effects. Routes and UI summaries are derived
from these owners.

Make each integration explicit about what it owns, what it observes, and which
owner resolves a disagreement. Remove duplicate administration. An index or
cached view must be reconstructible without inventing authority. The existing
PostgreSQL service, including LiteLLM's separate database, remains in scope as
infrastructure; replacing it is not part of this direction.

## 2. Make every handoff recoverable

Connect accepted intent, preparation, distribution, runtime effects, and route
publication through exact identities and durable acknowledgements. A retry
continues the same request; a new explicit request has a new identity and can
supersede older intent. Receivers recognize replay and inspect existing effects
before repeating work. Completion is acknowledged only after its evidence is
durable; lost acknowledgements can be recovered without repeating the effect.

Use reconciliation at each handoff instead of assuming that a database commit,
filesystem publication, agent action, and route activation happen atomically.
Each part must be able to discover what completed, what remains, and whether
its intent is still current after interruption. No component may silently
substitute a different model, image, plan, or authorization during recovery.

## 3. Prevent circular waits and contain failures

Enforce the architecture's common lock order, short transactions, nonblocking
claims, parent/child ownership rules, and bounded resource acquisition before
changing persistence ownership. Waiting work releases resources needed by its
dependencies. Every wait has a visible cause, an owner, a resume condition, and
a deadline that survives restart. Timeouts supplement these boundaries; they
cannot replace a design that prevents cycles.

Keep failures local to the affected request, object, source, or target.
Independent eligible work continues. Recovery uses bounded concurrency and
backoff, preserving partial transfers and verified results. Invalid contracts,
revoked authority, denied access, and integrity failures remain actionable
conditions; correcting them permits authorized recovery without weakening the
checks. Cancellation and newer intent survive delayed results and takeover.

## 4. Converge through the normal operating path

Use the same preparation and execution paths for initial work, retry, and
recovery. Reuse valid artifacts, resume compatible partial work, and safely
replace damaged temporary work. Failed refreshes and rebuilds preserve the
last verified result. Exact changed build outputs require a new accepted
binding rather than silently changing a running or already bound plan.

Reconcile API/worker intent, local storage, Spark results, and route publication
independently but under their shared request and attempt identities. Progress
views observe this work without blocking it. Cleanup coordinates with current
references and active work; missing evidence causes a defer, never inferred
permission to delete. A restore preserves surviving data and re-establishes
control authority before adopting artifacts or reclaiming them.

## 5. Change ownership in complete, bounded steps

Start by auditing cross-component waits and enforcing the coordination
boundaries. Then move artifact availability and local checkpoints from SQL to
managed storage, first for models and then images/builds, connecting their
admission, observation, cancellation, cleanup, and restore behavior as each
boundary changes.

Each step must leave one working execution path and one owner for its facts.
Update producers and consumers together and remove the obsolete persistence
responsibility. Preserve current security and recovery guarantees, useful
cached assets, and exact identities. Do not add dual readers/writers, legacy
schema compatibility, another scheduler, or new infrastructure to bridge the
change. Arbitrary jobs, hooks, and package upgrades do not acquire automatic
replay merely because artifact preparation supports recovery.

## Evidence of success

Validate complete interactions under contention, process death, lost responses,
restarts, cancellation, storage loss, and dependency recovery. Demonstrate that
no allowed wait cycle remains, stale attempts cannot regain authority,
completed work survives, unrelated work advances, and the system eventually
converges after recoverable faults clear. Include the real PostgreSQL, OS-lock,
worker, agent, and publication boundaries wherever those guarantees depend on
them; mocked local success cannot establish integration resilience.

Record repository/CI evidence separately from deployed Controller recovery and
physical Spark acceptance. Update operator guidance when behavior ships. This
approach requires observable recovery and bounded waiting, rather than a
blanket promise that untested code can never deadlock.
