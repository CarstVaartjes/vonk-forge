# Fault resilience and self-healing handover

Reviewed 2026-09-21. The objective is automatic convergence to the latest
authorized workload request after a recoverable fault clears, preserving
completed work and exposing the reason and next attempt while waiting.

This is the current action list for the resilience review. The operator-local
`~/vonk-forge-status-2026-09-18.md` is the historical incident record, including
its additions through 2026-09-21. Its deployment steps, recipe-revision
workarounds, Idle/reapply instructions, and blanket budget policy are
superseded as current guidance. Preserve its dated observations as evidence;
do not replay its recovery commands from the history alone.

The [engineering principles](../engineering-principles.md),
[coordination architecture](../architecture-overview.md#coordination-and-deadlock-prevention),
and [storage implementation plan](../plans/resilient-artifact-storage.md)
remain the owners of the architectural rules. This handover records status and
acceptance work; it does not introduce a separate recovery path.

## Observed baseline

The following is a snapshot, not a claim about the current state when this page
is read. Refresh it through the [Controller client](../runbooks/vonkctl.md)
before deciding on an operation.

| Boundary | Evidence captured for this review | What remains unproved |
| --- | --- | --- |
| Repository | Review baseline `4d79def445ce76080927d0871f8d58bd79550e57`, containing platform PRs #866, #867, and #873. | The additional resilience findings below are not closed by those merges alone. |
| CI and publication | [Run 35564523171](https://github.com/CarstVaartjes/vonk-forge/actions/runs/35564523171) passed the NAS and synthetic ARM64 lanes and promotion. | Synthetic acceptance is not physical GLM or fabric qualification. |
| Controller deployment | Read-only admin API observation at **2026-09-21 11:33:52 UTC** reported source `4d79def445ce76080927d0871f8d58bd79550e57`; the live resume API supports `retire`. | Deployment does not prove safe retirement or automatic recovery under injected faults. |
| Physical workload | Both Sparks were active. Profile 3 application `2b5bea43-4682-4cf8-8a0a-f9882f4aa72d`, started at 11:01 UTC, remained running. Artifact distribution had succeeded; both `recipe.install` operations had been running since 11:27 UTC. Neither Spark reported a loaded model. | Ready serving, inference, GLM quality, and physical recovery were not observed. |

The old redeployment prerequisite is satisfied at this snapshot. Redeployment
is not an outstanding action merely because an older section of the incident
record says it is. Subsequent code changes require their own CI, publication,
deployment, and physical evidence.

The archive's loader and kernel observations are historical results for the
specific images and attempts it names. Do not project their success onto the
current application or infer a new failure from a stage that is still running.

## Current action list

Keep progress in this list and the evidence record below; do not append another
competing "remaining work" section. Close an item only against its named
boundary, with the exact code and test/deployment evidence.

| Action | Completion condition | Status at this review |
| --- | --- | --- |
| Recover accepted intent automatically | A temporary dependency failure resumes the same authorized request after recovery, without retirement, Idle/reapply, or another recipe revision. Waiting exposes its cause, owner, dependency, next attempt, and deadline. Completed assets and effects are reused. | Implementation and failure/recovery verification required. |
| Make retirement and supersession safe | Observe or clean up the exact old runtime effects before releasing their reservations or admitting conflicting replacement work. Expired leases and terminal database labels alone cannot prove absence. Delayed results cannot revive retired intent. | Implementation and concurrent recovery verification required. |
| Align build reuse with installation admission | An editorial recipe successor with identical executable inputs can reuse an exact verified build under current revision authorization. Preparation, compilation, and install admission agree. Changed executable inputs or incompatible receipts cannot inherit the old result. | Implementation and connected producer/consumer verification required. |
| Apply the budget policy below | Estimated demand can produce a useful warning without removing real capacity, isolation, authorization, integrity, or exact-plan checks. Any automated smaller request or alternative runtime is explicitly permitted and bound in the accepted plan. | Audit and focused boundary verification required. |
| Prove the recovery matrix below | Record failure injection, restart, response loss, cancellation/supersession, storage loss, and eventual recovery through the real owners. Run physical qualification separately after the corresponding deployment. | No new fault injection or physical acceptance established by this review. |
| Maintain this handover | Retain one current action list, refresh dated observations, and keep repository, CI/publication, Controller, and physical results separate. | This document replaces the archive's conflicting current instructions. |

Follow the existing [workload recovery path](workload-recovery.md) and expose a
typed blocker where recovery lacks evidence or authority. Retirement is an
audited operator disposition, not a prerequisite for ordinary recovery.
Publishing a new recipe revision solely to escape poisoned state is not the
completion criterion for build reuse.

## Budget and retry policy

Classify the rule by the resource or authority it protects, not by whether its
message contains the word "budget".

| Kind of rule | Required behavior |
| --- | --- |
| Estimate of workload demand or a heuristic forecast | Report the estimate, observed capacity, unit, source, and uncertainty. Warn or choose an already authorized recovery path; an estimate alone must not permanently poison an otherwise admissible request. |
| Enforced resource or isolation limit | Retain actual allocator, device/kernel, container, storage, reservation, and concurrency boundaries. A temporary shortage waits with a bounded retry policy; an invalid request receives an actionable refusal. Do not turn a kernel safety condition into a warning merely because it mentions a budget. |
| Authorization, contract, integrity, or exact identity | Fail closed. No retry grants extra permission, ignores corruption, revives revoked authority, or silently changes the model, recipe image, topology, context, or plan digest. |
| Time, attempts, and retry rate | Bound each attempt and waiting dependency; persist deadlines across restart and show the next check. Exhausting one attempt's budget must not permanently ban a fresh authorized request. Never reset a deadline indefinitely to make stuck work look healthy. |

The current [engineering principles](../engineering-principles.md#every-frontier-recipe-must-remain-runnable)
require admission to account for the recipe's declared system-memory reserve,
workload demand, and existing reservations. Changing an estimate's severity must
preserve the real resource and ownership guarantees of that contract. Record
the measured value and limit when a bound fires so the operator can distinguish
a forecast from an actual shortage.

A retry uses the existing exact request and accepted inputs. A smaller context,
different memory setting, alternative engine, or replacement image is allowed
automatically only when the accepted plan explicitly authorizes and identifies
that alternative. Otherwise it is a new proposed plan for an explicit operator
decision. A changed rebuilt digest cannot silently replace an image already
bound to a profile or running workload.

## Recovery acceptance matrix

These are required acceptance targets, not passing results. For each scenario,
record the request, parent and child operations, attempt fences, exact artifact
and plan identities, injected fault, recovery time, configured retry ceiling,
persisted deadline, and observed outcome. Keep logs redacted and bounded.

| Fault at a real boundary | Required observable outcome |
| --- | --- |
| Kill a transfer worker after a durable partial checkpoint; restart it | The original request resumes compatible partial bytes and reuses finished objects. Partial bytes never appear ready. Publication is atomic and a stale attempt cannot publish after takeover. |
| Restart the Controller while waiting for an agent result | The same accepted intent and child identities survive. Reconciliation observes exact effects before issuing unfinished work. Waiting releases execution slots and transactions needed by the dependency. |
| Kill or restart the agent after an effect but before its acknowledgement | Journal replay or exact effect reconciliation adopts the existing install/runtime/removal. It neither launches a duplicate container nor falsely declares an unknown effect absent. |
| Drop the API submission response or the route activation acknowledgement | The retained request key recovers the same operation; an existing exact route generation is adopted. A local client watch timeout does not fail the durable application. |
| Cancel or supersede a parked/running application, then deliver its late success | Newer authorized intent wins. Exact old effects are confirmed stopped/removed before conflicting capacity is released; no stale result can republish the old route. |
| Temporarily lose artifact storage, a source, or a needed service | The reason, dependency owner, next attempt, and deadline remain visible. The attempt releases contended resources. Once the dependency returns, eligible current work resumes without a new recipe revision or manual state edits. |
| Remove a managed cache object while retaining its historical success record | Missing bytes are an actionable repair blocker. Repair uses the existing authorized preparation path, preserves other verified assets, and cannot silently replace a bound image with a different digest. |
| Race duplicate preparation, cleanup, and replacement requests | One writer owns an artifact; current fences decide which result may attach. Cleanup protects authoritative references and active work. An unavailable reference scan defers deletion. |
| Introduce one busy artifact or malformed historical record | That object's operation reports its own problem while unrelated eligible work advances. Exact reads remain strict; malformed data never becomes valid through defaulting. |
| Revoke permission, deny storage access, or fail an integrity check during recovery | Recovery remains blocked with its specific reason. No automatic retry broadens grants or treats denied access as absence; correction is followed by current-authority validation. |

For every retry test, restore the dependency and prove convergence within the
configured next-attempt window plus the recorded step deadline. For expiry
tests, prove a bounded, named outcome and that restart does not reset that
deadline. No test may pass solely because a status field eventually says
`succeeded`: check the exact artifacts, runtime effects, reservations, and route
belonging to current intent. Verify unchanged verified objects are reused
without another transfer or ordinary full-file hash.

Use real PostgreSQL and independent processes for lock, lease, takeover,
retirement, and cleanup races. Use the Linux/OrbStack lanes for process,
container, journal, and filesystem behavior. The
[test guide](../testing-and-ci.md) and
[local Linux lane](../local-linux-lane.md) define how to run them. Mocked locks,
SQLite, or unit status transitions alone do not prove these boundaries.
Physical GLM loading, NVIDIA behavior, fabric, inference, and recovery on the
Sparks remain a separate qualification after deployment; a synthetic ARM64 pass
does not close that step.

## Evidence record maintenance

Attach evidence to the corresponding action above: timestamp in UTC, full
platform and recipe-library commits, recipe content digest, model and image
identities, test command/result or CI URL, and the boundary established. For a
deployment, include the signed generation and immutable image identities; use
the build commit in release `version`, not a refreshed `source_sha` alone, to
decide which code the artifacts contain. For a physical run, include the
application and per-node operation identities and readiness/inference results.

Record unavailable inputs and unrun scenarios explicitly. Do not inject faults,
retire a run, unload a profile, evict a cache, or redeploy solely to refresh this
status page. Such work follows its own concrete operating scope. Keep prior
observations dated when replacing the baseline, and never copy credentials or
unredacted API bodies into this document.
