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
| Recover accepted intent automatically | A temporary dependency failure resumes the same authorized intent after recovery, without retirement, Idle/reapply, or another recipe revision. Waiting exposes its cause, owner, dependency, next attempt, and deadline. Completed assets and effects are reused. | Merged in PR #874 and deployed for classified safe-effect retries and typed pre-effect profile cache loss. Exact identities and current workload priority are retained. Combined verification is recorded below; physical qualification remains open. |
| Make retirement and supersession safe | Observe or clean up the exact old runtime effects before releasing their reservations or admitting conflicting replacement work. Expired leases and terminal database labels alone cannot prove absence. Delayed results cannot revive retired intent. | Merged in PR #874 and deployed. Retirement retains capacity and schedules normal exact cleanup; temporary cleanup failure retries the same operation. PostgreSQL regressions cover release, late results, launch budgets, and newer intent. |
| Align build reuse with installation admission | An editorial recipe successor with identical executable inputs can reuse an exact verified build under current revision authorization. Preparation, compilation, and install admission agree. Changed executable inputs or incompatible receipts cannot inherit the old result. | Connected preparation/admission corrections were merged in PR #874 and deployed. Current authorization and present verified bytes remain mandatory. |
| Complete the physical GLM start | Installed bytes are charged once, a stopped workload can restart with its private temporary files present, and exact rank observations reach route publication. | PR #875 is merged, published, and deployed. Profile 3 succeeded on both upgraded Sparks; non-streaming and streaming inference passed, with fresh rank observations six minutes after readiness. The restart fault is covered by real Linux permission tests; physical fault injection remains separate. |
| Apply the budget policy below | Estimated demand can produce a useful warning without removing real capacity, isolation, authorization, integrity, or exact-plan checks. Any automated smaller request or alternative runtime is explicitly permitted and bound in the accepted plan. | The historical blanket warning policy is superseded below. No allocator, kernel, integrity, or authorization check was weakened, and no automatic context/image substitution was introduced. |
| Prove the recovery matrix below | Record failure injection, restart, response loss, cancellation/supersession, storage loss, and eventual recovery through the real owners. Run physical qualification separately after the corresponding deployment. | Local process-death and PostgreSQL fault/recovery tests are recorded below. Deployment and physical fault injection remain open. |
| Maintain this handover | Retain one current action list, refresh dated observations, and keep repository, CI/publication, Controller, and physical results separate. | This document replaces the archive's conflicting current instructions. |

Follow the existing [workload recovery path](workload-recovery.md) and expose a
typed blocker where recovery lacks evidence or authority. Retirement is an
audited operator disposition, not a prerequisite for ordinary recovery.
Publishing a new recipe revision solely to escape poisoned state is not the
completion criterion for build reuse.

## Budget and retry policy

The durable [resource accounting and budget policy](../engineering-principles.md#resource-accounting-and-budget-policy)
owns these rules. It classifies forecasts, actual resource/isolation limits,
authority/integrity checks, and attempt deadlines separately. This handover
records the review's application and evidence, not a second policy definition.

The current [engineering principles](../engineering-principles.md#every-frontier-recipe-must-remain-runnable)
require admission to account for the recipe's declared system-memory reserve,
workload demand, and existing reservations. Changing an estimate's severity must
preserve the real resource and ownership guarantees of that contract. Record
the measured value and limit when a bound fires so the operator can distinguish
a forecast from an actual shortage.

Recovery retains the accepted inputs and current workload intent. An agent-effect
retry retains its operation; profile cache recovery may create a linked
application receipt without advancing the workload-intent ordinal. Explicit
operator retry retains its existing cancellation-fencing behavior. A smaller context,
different memory setting, alternative engine, or replacement image is allowed
automatically only when the accepted plan explicitly authorizes and identifies
that alternative. Otherwise it is a new proposed plan for an explicit operator
decision. A changed rebuilt digest cannot silently replace an image already
bound to a profile or running workload.

Missing exact cache bytes remain an actionable Prepare cache dependency. A rebuilt
image with a different image/archive identity requires an explicit new load;
automatic recovery checks the binding again before child dispatch.

## Recovery acceptance matrix

These remain the acceptance targets; the evidence below identifies the scenarios
already exercised. For each scenario,
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

## Repository verification for this review

The integrated work is on `codex/resilience-integration`, based on platform
`4d79def445ce76080927d0871f8d58bd79550e57`, with recipe-library checkout
`068930061f3b76a519ac3ffcc137ab7a9f067010`. The library checkout was clean.
This original review was merged in [PR #874](https://github.com/CarstVaartjes/vonk-forge/pull/874)
as `9f0cea2edd48493c295afa5f0816cc6a5dbac85a`. Its deployment was subsequently
confirmed; the original test results below remain repository evidence.

Regressions were reproduced before their fixes: permanent retry exhaustion,
premature retirement capacity release, failed temporary cleanup, successor
recipe build rejection, changed-image adoption, mixed-profile recovery refusal,
and starvation of a recovered parked parent.

| Validation boundary | Result |
| --- | --- |
| Full Controller hermetic suite (`control/tests`, excluding lane tests) | 2,297 passed, 3 skipped. |
| Standalone acceptance/contract suite (`tests`, excluding lane tests) | 798 passed, 11 skipped; 45 subtests passed. |
| PostgreSQL recovery/concurrency selection in OrbStack | 48 passed. Includes real result-worker process death, lost committed responses, old exhausted-row recovery, one-winner claims, stale fences, retirement cleanup, denied cleanup, launch budgets, newer intent, and parked-parent fairness. |
| Linux ARM64 Controller/Spark wire lane in OrbStack | 676 passed, 19 skipped; separate publication selection: 1 passed. The lane used the exact library checkout above with `--any-recipe-revision`. |
| Recovery phase suite after integration | 68 passed. Cold compilation and receipt-order disagreement remain faulty for six and four persisted retry cycles respectively before recovery is allowed. |
| Static and generated contracts | Ruff, touched-file formatting, Python types (one existing reviewed exception), browser type/build gate, generated Rust wire check, coordination scanner (zero exceptions), documentation links, and whitespace checks passed. OpenAPI and Python/TypeScript clients regenerated. |
| Supply-chain evidence | Manifest regenerated and verified with no errors. |

The process-death test kills independent result-recording workers after their
PostgreSQL commit; it does not claim to kill a physical Spark or GPU runtime.
Physical stop/load, route readiness, inference, and recovery under injected
hardware faults remain unperformed in this review. No database schema change,
live retirement, cache eviction, profile application, or deployment was made
during that original source review.

## GLM start recovery after deployment (2026-09-21)

The authenticated Controller provenance at 12:57 UTC reported the running image
source `9f0cea2edd48493c295afa5f0816cc6a5dbac85a`. Both Sparks were online, with
GLM 1.6.6 installed and no loaded workload. The retained recipe binds context
196608, GPU memory utilization 0.84, recipe digest
`4e5255a78123f3054a4cbff993996a89262082de5c518c7e02eb906eee3e8665`, and image
`sha256:fa1868a9403baee4527b113fc84a6cfb89cf227595ced71205d1d5e19f7be950`.
These settings and artifact identities are unchanged by the recovery corrections.

The previous application `2b5bea43-4682-4cf8-8a0a-f9882f4aa72d` had an initial
successful start job `f9c6fa06-fd63-4aea-9450-cadc2c360707`: both rank launches
completed at 11:35:18 UTC and collective readiness completed at 11:39:19 UTC.
The Controller stopped the run at its initial observation deadline, 11:41:19 UTC.
Both ranks had unconsumed inspection grants and no accepted observation receipt;
route publication had not been attempted. The subsequent restart job
`d439972d-68bc-4015-817a-0a3a00a61db1` failed during local preparation on both
nodes. Initial readiness alone therefore did not prove published serving.

A fresh profile preview was blocked by `run-switch.insufficient-disk`.
Read-only inspection of the reservation owners confirmed five completed
installations reserving 1,320,491,003,815 bytes per node. Their saved plans account
for 995,491,003,815 bytes of completed downloads, already reflected in the later
filesystem inventory. The shared admission calculation now discounts only those
proved completed downloads. It retains 325,000,000,000 bytes of cache/staging
headroom and fully charges pending or uncertain work. It does not delete any
reservation or reduce the new request's 299,400,776,148-byte allowance.

The restart failure was reproduced through real Linux process permissions:
the unprivileged agent attempted to remove private temporary directories created
by the privileged helper for the runtime user. Cleanup now belongs to the existing
authorized helper after it proves that the exact old container is absent.
Retained or uncertain containers keep their temporary files; persistent results
and runtime caches are preserved. Preparation failures must retain their safe
stage and error category instead of erasing the cause. A private preparation
marker makes cleanup happen once, before the first authorized startup hook;
later hooks and the main process retain files created by earlier hooks.

The recovery start retained the same run ID,
`d6c52f16-4f9b-4d52-8127-b0a550232cc8`, at generation 2. Its deadline was
11:42:19 UTC: only one minute after recovery began, despite the accepted initial
startup budget of 3600 seconds and the observed four-minute successful start.
Recovery derives its bounded loading allowance from the original successful
start job's immutable accepted duration. The overall deadline adds the ordered
stop phases' allowances and remains fixed across stop, start, and route retries.
Absent or malformed startup authority remains an explicit blocker. A fixed
readiness poll allowance is not a model-loading budget.

Continuous observation is a separate boundary. Linux HTTP regressions reproduced
successful exact receipts being discarded when another retained run's inspection
failed or its receipt expired. Valid nonempty partial reports now proceed while
the unrelated error remains visible; a failed collection never proves absence.
Signed-receipt tests through PostgreSQL and the Linux helper also reproduced
valid renewals being treated as late first observations. The initial cutoff is
now enforced when the first authenticated receipt arrives; later renewals use
the continuing freshness checks. First receipts beyond the cutoff still fail.
The erased agent diagnostics do not establish which observation fault caused
this particular live stop. Verification of those corrections and physical
serving must be recorded separately from the reproduced restart failure.

Combined local verification for these corrections: 2,298 Controller fast tests
passed (3 skipped); 798 standalone acceptance/contract tests passed (11 skipped,
45 subtests); 149 lifecycle, disk-admission, and route tests passed,
including real PostgreSQL concurrency and expiry boundaries; 9 signed-observation
wire tests passed using PostgreSQL and Linux helper/Rust probes. The combined
Linux agent/helper suite passed 471 tests (2 designated systemd tests ignored).
The final FIFO-marker rejection and lint follow-up passed 331 affected tests
(1 designated systemd test ignored) and Clippy. These are repository results;
accepted publication, deployment, and physical GLM inference are recorded below.

### Published deployment and physical serving evidence

[PR #875](https://github.com/CarstVaartjes/vonk-forge/pull/875) merged as
`de5e38fcac01309c64ab3ac2e3ade88601cb722f`. All PR checks passed, including the
real ARM64 helper/container-start and package recovery proofs.
[Publication run 35627304450](https://github.com/CarstVaartjes/vonk-forge/actions/runs/35627304450)
passed both NAS lanes and packaged Spark acceptance, then promoted schema-2
generation `2793700f9e46f439d7a6f6b73e790163f2d9c3f44d10efbab3896f1749574a6c`.
Its package version is `0.1.1~dev.609+gde5e38fcac01`; both the build version and
release source bind the repair commit.

The NAS pulled the accepted API and worker images and recreated those two
services through its existing Compose project. No Compose, credential, volume,
or database-schema changes were made. Authenticated Controller provenance at
**2026-09-21 16:54:57 UTC** reports the running Controller source as
`de5e38fcac01309c64ab3ac2e3ade88601cb722f`.

Controller upgrade `eaa4f023-b5e1-47b0-8e8a-86ad6e2dd36b` upgraded the Sparks
one at a time. Both targets succeeded and proved binary
`4cf60adfb0f34acb402a46026b41ab2ac0a874be96a1c0b3d0a201c3e3624f9f` and build
`sha256:09ec1c3ea7131d4dce938229ec9d6cfe1cd85c3f864ae5b24a26226329675e77`,
matching the accepted package's identity. Completion was recorded at
16:55:35 UTC for spark-3542 and 16:55:41 UTC for spark-2297.

Profile 3 preview then admitted the unchanged exact recipe with no blockers,
builds, installations, or replacement stops. Application
`9278ba6a-fa5f-4e59-ac4c-355ffba7c88c` started at 16:56:46 UTC, verified and reused
the cached artifacts, and succeeded at 17:02:57 UTC. It retained installation
`503907a4-58e5-4fef-863f-5701f7a08675` and created run
`c848e6b8-fe97-4fdc-b935-3647c41d16aa`. Start job
`56dc193e-f7c8-47d4-842d-51aaa6232b65` completed both launches at 17:00:35 UTC
and collective readiness at 17:02:11 UTC. The published alias is
`vonk-forge-glm-5-3-flash-exl3-dflash2-vllm-dual`.

Bounded requests through the NAS's Caddy-to-LiteLLM inference route proved:

- Non-streaming: HTTP 200, final content `VONK_READY`, finish reason `stop`,
  3.287 seconds, 20 prompt tokens and 91 completion tokens.
- Streaming: HTTP 200, final content `4` for `2 + 2`, finish reason `stop`,
  18 events and a terminal `[DONE]`, 2.061 seconds.

An earlier 128-token request used its allowance on reasoning and ended without
final content; the successful completion used a bounded 384-token allowance.
No model, context, image, or profile setting changed.

At **2026-09-21 17:08:10 UTC**, both Sparks were online with the same run,
both ranks present and healthy, and the route published. Authenticated rank
observations were respectively eight and six seconds old. This is almost six
minutes after collective readiness, beyond the initial two-minute observation
deadline that stopped the earlier attempt. The successful workload was left
running. This proves physical loading, ongoing observation, and basic inference;
it does not claim a model-quality benchmark or physical fault-injection campaign.

## Evidence record maintenance

Attach evidence to the corresponding action above: timestamp in UTC, full
platform and recipe-library commits, recipe content digest, model and image
identities, test command/result or CI URL, and the boundary established. For a
deployment, include the signed generation and immutable image identities, then
check the build provenance of the particular artifact being deployed. A
refreshed manifest `source_sha` alone is insufficient; a package version can
also predate a separately rebuilt Controller image. For a physical run, include the
application and per-node operation identities and readiness/inference results.

Record unavailable inputs and unrun scenarios explicitly. Do not inject faults,
retire a run, unload a profile, evict a cache, or redeploy solely to refresh this
status page. Such work follows its own concrete operating scope. Keep prior
observations dated when replacing the baseline, and never copy credentials or
unredacted API bodies into this document.
