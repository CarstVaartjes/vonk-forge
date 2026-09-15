# Workload recovery

An accepted workload request remains the owner of its preparation, installation,
runtime and route work until it completes, is cancelled, or is superseded by a
newer request on the same Sparks. Temporary failures keep that request's identity
and completed work. Operators should follow its existing operation instead of
submitting the same load again to recover a lost connection.

## Agent and Controller restarts

The agent keeps completed results in its local journal until the Controller
acknowledges them. A restart resends those results without executing their effects
again. If the process died before it recorded a result, it reports an interrupted
effect; that report does not claim the action failed or succeeded.

The Controller schedules a bounded retry of an interrupted content-addressed
transfer or read-only preflight. It retains the exact operation, payload and
progress, and issues a fresh attempt under current authorization. Finished cache
objects are reused and partial files resume at their retained offset. A pending
retry remains visible as pending work, with a reason and next retry time.

Lifecycle recovery requires an agent advertising exact lifecycle resumption.
Installation, Start, Stop and uninstall reconcile the exact stored receipt or
runtime/filesystem effect before performing unfinished work. A running exact
runtime is observed instead of started again. A completed installation is checked
and reused instead of copied again. Completed removal is accepted only when the
exact managed object is proved absent.

An incomplete hook can remain blocked when its external effect cannot be proved.
Builds, arbitrary jobs and package upgrades do not inherit automatic workload
replay. Invalid ownership, revoked authorization, malformed contracts, integrity
failures and denied access also remain explicit blockers. These conditions need
their specific corrective action; repeated retries cannot make them valid.

## Cancellation and replacement

A new explicit request on selected Sparks supersedes older overlapping intent.
Queued old work is cancelled; issued old work receives cancellation and exact
cleanup where necessary. The replacement proceeds after the old effects are
resolved. Agent restart or delayed success cannot reactivate the old workload.

Cancellation of a known invalidated order is an informational no-op. It does not
fail the replacement or accept an old result as current success. Unrelated
workloads and shared immutable caches remain outside the cleanup scope.

## Route and CLI recovery

A temporary route-publication failure keeps the runtime running while route
publication retries. If activation succeeded but its acknowledgement was lost,
recovery adopts that generation instead of starting another runtime or route.

The CLI retains the request key before submission. When a response is lost, it
looks up the same durable request. A temporary observation failure shows that the
CLI is reconnecting; a local watch timeout is not a claim that the Controller's
operation failed. Offline build information and signed update checks are
described in [CLI updates](../operators/cli-updates.md).

## Validation boundaries

Connected Linux tests exercise process death, journal replay, fresh Controller
claims and transfer/installation reuse. Installer tests use a real Linux terminal
and uncached sudo authentication. Repository tests and signed package publication
do not prove a deployed Controller or physical Spark acceptance. Measure actual
NAS/Spark throughput and verify model loading, fabric and inference after the
corresponding deployment.
