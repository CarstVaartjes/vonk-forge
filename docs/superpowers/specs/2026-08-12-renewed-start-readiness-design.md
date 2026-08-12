# Renewed Start Readiness Design

## Status

Approved for implementation on 2026-08-12 under the operator's standing
instruction to use best judgment and continue the physical acceptance rollout.

## Problem

The controller renews an active agent operation through heartbeat directives,
and the Rust agent persists those renewed deadlines. The executor nevertheless
receives only the original claim. Recipe start passes that original deadline to
the workload readiness loop.

The physical DS4 acceptance run exposed the mismatch. Exact verification of
the installed 94 GB artifact set consumed about 49 seconds of the initial
60-second lease. The container then had about 12 seconds to become ready even
though heartbeats had extended the authoritative lease. The start failed and
the controller correctly stopped the partial run and withdrew its route.

The acceptance runner also uses a deterministic request key for the initial
run. Replaying it returns the audited failed operation, so a repaired agent
cannot resume that evidence file without creating a fresh run.

## Constraints

- Preserve exact artifact verification before every start.
- Preserve short renewable leases, fencing, cancellation, and fail-closed
  behavior when controller heartbeats stop.
- Do not retry a failed start operation. Start is a stateful multi-node action,
  and its existing automatic cleanup is the recovery boundary.
- Preserve the original failed operation and run in controller audit history.
- Recovery must be bounded to one replacement run with a deterministic request
  key.
- Persist acceptance evidence only after all target nodes provide exact,
  successful readiness evidence.

## Considered Approaches

### Selected: propagate the renewable deadline

Create a Tokio watch channel when an operation begins. The executor receives a
receiver initialized with the claim deadline. After each accepted heartbeat,
the heartbeat task publishes the controller directive's new deadline. Workload
readiness reads the current deadline on each polling iteration.

This keeps the controller authoritative, naturally supports slow model starts,
and still expires after the last granted deadline if heartbeats fail.

### Rejected: increase the initial start lease

A larger fixed lease would mask the observed run but would still duplicate the
controller's renewable lease state. It would also require guessing a safe upper
bound for future models and engines.

### Rejected: skip or cache artifact verification

Skipping the exact pre-start verification weakens the installed-artifact trust
boundary. A cache would require a new invalidation and filesystem-integrity
design and is not needed to fix deadline propagation.

## Agent Design

`Executor::execute` accepts a watch receiver containing the current authorized
deadline. `run_once_with_heartbeat_interval` creates the channel before it
starts the heartbeat task and passes clones to the heartbeat task and executor.
The heartbeat task sends a deadline only after the directive has passed state
validation and persistence.

`health::wait_ready` receives the watch receiver and evaluates its current
deadline before every HTTP readiness attempt. If no newer directive arrives,
the final accepted deadline expires normally. Existing heartbeat errors remain
terminal and durable; the channel does not invent or locally extend authority.

Only recipe start consumes the changing deadline today. Other operations gain
the executor interface without changing their behavior.

## Acceptance Recovery Design

The development slice runner continues to submit the initial run with its
existing deterministic request key. If that operation is terminally failed,
waiting-for-operator, or expired, the runner:

1. polls the failed run until the controller reports `state=stopped` and
   `route_state=withdrawn`;
2. requests a fresh run-plan preview for the same installation;
3. creates one replacement run with deterministic key
   `running:start-retry`;
4. requires that replacement operation to succeed without another retry; and
5. records the failed operation ID, failed run ID, replacement operation ID,
   and replacement run ID in the private acceptance evidence.

If cleanup does not converge or the replacement fails, the runner exits and
does not mark `running` complete. An initially successful run remains unchanged.

## Testing

- A Rust executor-loop test must prove an executor observes a deadline newer
  than the original claim after heartbeat renewal.
- A health test must prove readiness can succeed after the original deadline
  when the watch channel carries an accepted extension.
- Existing heartbeat-failure tests must continue proving fail-closed durable
  terminal behavior.
- A development-runner test must prove one failed start is followed by cleanup,
  a fresh preview, and one successful replacement using the exact deterministic
  key while retaining failed-attempt evidence.
- A second runner test must prove a failed replacement remains terminal and is
  never retried again.
- The Rust workspace tests, script tests, lint/format checks, and GitHub Actions
  gates must pass before deployment.

## Operational Validation

After publication, update the two Spark agents through the signed development
APT channel, canarying Spark 2 before Spark 1. Resume the existing private
single-node evidence file. The failed start must remain visible, the replacement
run must pass DS4 readiness and inference, and subsequent restart, stop, and
uninstall checkpoints must complete. Then execute the two-node failure and
recovery slice before considering physical acceptance complete.
