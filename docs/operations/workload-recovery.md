# Workload recovery

This runbook describes implemented workload recovery. The
[coordination architecture](../architecture-overview.md#coordination-and-deadlock-prevention)
sets the required lock and scheduling boundaries. Recovery does not make
arbitrary jobs or upgrades safe to replay.

An accepted workload request remains the owner of its preparation, installation,
runtime and route work until it completes, is cancelled, or is superseded by a
newer request on the same Sparks. Temporary failures keep that request's identity
and completed work. Operators should follow its existing operation instead of
submitting the same load again to recover a lost connection.

## Agent and Controller restarts

The Controller renews active route leases on a dedicated worker thread, so a
slow image preparation cannot starve LiteLLM routes. Runtime-image preparation
runs in a bounded background executor; the durable Run/Switch operation waits,
reports sampled transfer bytes, and checks its existing filesystem checkpoint,
which is resumed after a worker restart. OCI helper subprocesses have a one-hour
timeout and three bounded attempts with backoff. Their redacted stderr tail is
included in the operation reason and worker logs.

The worker records heartbeats by process instance, allowing overlapping
instances to remain independently healthy during a restart. A watchdog exits
the worker with a non-zero status if its scheduler loop does not complete a
turn for 180 seconds; Compose then restarts the worker under its existing
restart policy.

The agent keeps completed results in its local journal until the Controller
acknowledges them. A restart resends those results without executing their effects
again. If the process died before it recorded a result, it reports an interrupted
effect; that report does not claim the action failed or succeeded.

The Controller schedules a bounded retry of an interrupted content-addressed
transfer or read-only preflight. It retains the exact operation, payload and
progress, and issues a fresh attempt under current authorization. Finished cache
objects are reused and partial files resume at their retained offset. A pending
retry remains visible as pending work, with a reason and next retry time.

Safe retries of current intent retain a bounded retry rate without a lifetime
attempt ceiling. A delayed preflight, runtime, cancellation, or route observation
keeps its exact dependency and slows its checks after the normal observation
window. Neither an expired lease nor an overdue observation proves that an
effect stopped. Current authority, cancellation, and newer intent are checked
before further work.

Installation, Start, Stop and uninstall reconcile the exact stored receipt or
runtime/filesystem effect before performing unfinished work. A running exact
runtime is observed instead of started again. A completed installation is checked
and reused instead of copied again. Completed removal is accepted only when the
exact managed object is proved absent.

For a persistent single-Spark run, a fresh agent observation of the current
run generation that reports `process_running=false` lets the Controller withdraw the route and
queue the canonical exact Stop, followed by a new generation of the same
accepted Start. The model, image, installation, plan and workload-intent
ordinal remain bound to the original accepted Start. Recovery can repeat after
later reboots only through the latest verified successful recovery Start and
its exact completed Stop. A newer workload intent or cancellation fences this
path, and an uncertain Stop keeps the run's resource claims active.

Singleton recovery allows five attempts in a window, with exponential delays
between attempts. After the fifth attempt it exposes a degraded reason and
waits five minutes, then resets the window and resumes exact inspection
automatically. Retry state and next-check time are durable. The coordinator also
uses the exact Stop/Start path for eligible persistent multi-Spark runs; it
retains the accepted topology and authorization and does not infer absence from
a lost rank.

The Controller does not infer process absence from an expired lease, stale
observation, or offline Spark. If the observed absence becomes stale, it waits
for a fresh observation of the still-current run generation. Missing or stale
Controller-observed presence is also a bounded wait; a refreshed absence
observation and presence resume the same run. These waits
keep the route withdrawn and do not release claims. Singleton reboot recovery
fails closed when the exact accepted compiled lifecycle plan differs from the
installed plan.

One-shot jobs remain fail-closed after a lost result because the job may already
have produced external effects. Verify those effects before submitting a new authorized run.
Builds and package upgrades do not inherit automatic workload replay. Invalid
ownership, revoked authorization, malformed contracts, integrity failures and
denied access remain explicit blockers.

Final run verification waits for exact evidence for at most 15 minutes after
the accepted start deadline. If it expires, the Controller fails the switch
operation with a visible reason, withdraws the route, marks the run degraded,
and hands it to exact workload recovery. Recovery still obtains signed
observations and reconciles Stop before any replacement Start.

The standing-profile worker checks accepted running assignments even when the
enrolled roster is unchanged. Missing or degraded assignments are reconciled
through profile application, run recovery, and route recovery as appropriate;
this does not change the accepted profile or grant new authority.

## Cancellation and replacement

A new explicit request on selected Sparks supersedes older overlapping intent.
Queued old work is cancelled; issued old work receives cancellation and exact
cleanup where necessary. The replacement proceeds after the old effects are
resolved. Agent restart or delayed success cannot reactivate the old workload.

Cancellation of a known invalidated order is an informational no-op. It does not
fail the replacement or accept an old result as current success. Unrelated
workloads and shared immutable caches remain outside the cleanup scope.

Retirement fences an exhausted order but retains uncertain runtime and disk
reservations. The Controller follows the ordinary exact stop/uninstall path,
retries classified temporary cleanup failures, and releases capacity only after
cleanup succeeds. An open launch budget refuses retirement. A denied or invalid
cleanup remains an explicit blocker with capacity retained.

## Missing cache recovery

Automatic profile cache recovery retains the accepted model and image identities
and workload-intent ordinal. It can create a linked application receipt after a
pre-effect cache loss, without raising that intent above a newer request. An
explicit operator retry retains its cancellation-fencing behavior.

Missing exact bytes expose a Prepare cache dependency and a next check. Restoring
the same bytes permits recovery; a rebuild with a different image or archive
digest requires an explicit new load. Recovery checks the binding again before
dispatching the child operation. The authorized preparation path can reuse an
existing verified build for a currently authorized recipe revision with the same
executable build inputs.

## Route and CLI recovery

A temporary route-publication failure keeps the runtime running while route
publication retries. If activation succeeded but its acknowledgement was lost,
recovery adopts that generation instead of starting another runtime or route.

The CLI retains the request key before submission. When a response is lost, it
looks up the same durable request. A temporary observation failure shows that the
CLI is reconnecting; a local watch timeout is not a claim that the Controller's
operation failed. Offline build information and signed update checks are
described in [CLI updates](../operators/cli-updates.md).

## Run capacity ownership

Ports, the multi-Spark rendezvous port and memory are held only by a run a
plan can still stop: planned, starting, running (including one under recovery),
stopping, or lost. A load that needs such a run's Sparks plans its exact Stop,
which releases the claims (a lost run planned for a Stop is not an
unreconciled-rank blocker). The recovery coordinator releases, on every tick,
the claims of any other run (failed, stopped, or missing), so such a run can
never block a load. Whatever it may still occupy on a Spark is what that
Spark's next inventory reports. A run whose stored plan the Controller can
no longer read (an older contract), or whose Sparks a newer workload intent now
owns, is settled as failed with its reason instead of being recovered.

## Runs whose local metadata is unreadable

A retained run whose managed metadata cannot be read (an older agent's format,
a retired field, no run generation) is settled from its run id and the
Controller alone. The agent asks for the run's disposition. The Controller
names every run it does not want (never owned, failed, or stopped) unowned, and
the agent retires that run's lifecycle claim. A known running run comes back with its accepted
generation; when the root helper proves the exact `vonk-<run_id>` container is
absent or stopped, the agent reports that absence at that generation and the
Controller recovers the run through its normal singleton recovery. A running
container is never reported from unreadable metadata. Each skip is logged once
per process.

## Validation boundaries

Connected Linux tests exercise process death, journal replay, fresh Controller
claims and transfer/installation reuse. Installer tests use a real Linux terminal
and uncached sudo authentication. Repository tests and signed package publication
do not prove a deployed Controller or physical Spark acceptance. Measure actual
NAS/Spark throughput and verify model loading, fabric and inference after the
corresponding deployment.
