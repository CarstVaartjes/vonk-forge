# Observe `vonk-forge`

Prometheus has no published port. Platform metrics use stable generated node IDs
and bounded enum labels; they never include prompts, responses, credentials,
hostnames, private addresses, raw serials, request IDs, or job IDs.

## Inference statistics

LiteLLM exports bounded Prometheus metrics (requests, input and output tokens,
latency, time to first token, failures) per model alias and gateway key alias.
Labels never carry prompts, raw keys or end-user identities. `/metrics` is
reachable only by Prometheus over the internal `litellm-metrics` network and
Caddy answers 404 for
it. Prometheus keeps one year (`365d`, capped at 40GB; the oldest blocks go
first if the cap is reached). Changing the retention flags keeps the existing
TSDB. Time to first token covers streaming requests only; the error rate is
failed over total deployment attempts, so it can include retried attempts.

Operational JSON logs are rotated by Docker's local driver. Remote output is
redacted and truncated before persistence. Full sanitized job evidence is
content-addressed and available only to operator/administrator API roles.

## Worker memory

The worker publishes no port. A sampler thread writes its own memory report
every 15 seconds to `diagnostics/worker-memory.json` on the shared control state
volume, and the API exports it on `/metrics`:

| Metric | Meaning |
| --- | --- |
| `vonk_worker_rss_bytes`, `_peak_rss_bytes`, `_children_rss_bytes` | Resident memory of the worker process, its peak, and its child processes (skopeo). |
| `vonk_worker_cgroup_bytes{kind}` | Container memory split into `anon`, `file`, `shmem`, `kernel`, `current`. |
| `vonk_worker_python_allocated_blocks`, `vonk_worker_threads` | Python heap size proxy and thread count. |
| `vonk_worker_component_entries{component}` | Entries in each named long-lived cache or queue (`WorkerMemoryComponent`). |
| `vonk_worker_trace_state{state}`, `vonk_worker_traced_bytes`, `vonk_worker_trace_growth_bytes{rank,location}` | Last allocation trace: the 25 code locations that gained the most Python heap. |
| `vonk_worker_memory_report_age_seconds` | Age of the report; no worker series are exported once it is older than two minutes. |

Read RSS against the other series. High `anon` with a high
`vonk_worker_traced_bytes` and a named `location` is a retained Python object.
High RSS with a low traced figure is allocator fragmentation or native memory.
High `file` is page cache and high `shmem` is the tmpfs `/tmp`; neither is a
worker heap leak. Child memory is counted separately in
`vonk_worker_children_rss_bytes`.

Allocation tracing is off until RSS passes 2 GiB (it re-arms after another
1 GiB of growth). It samples for two minutes, publishes the result, logs
`worker.memory_trace_captured`, and stops. To trace on demand, create
`diagnostics/worker-memory.trace-request` in the control state volume
(`docker exec <worker> touch /state/diagnostics/worker-memory.trace-request`);
the worker consumes the file and traces once.

## Routes stuck in maintenance

Inspect the current recipe operation and affected-node Fleet connection,
inventory, and telemetry state. Keep routes withdrawn until the pinned recipe
revision, current API settings, recipe operation leases, and acceptance checks
all pass. Do not manually point LiteLLM at an unaccepted GPU node endpoint.

## Stale fleet evidence

Open `/api/fleet` and inspect the node's connection state, certificate
validity, admission inventory freshness, and telemetry freshness. Missing,
delayed, and stale evidence remain distinct; an online connection alone does
not establish readiness. A hostname or address change must be updated through
the current Fleet API settings; it must not create a new node identity.

## Invalid node certificate

Inspect the certificate state and expiry metric for the affected node. Renew or
re-enroll through the authenticated enrollment flow, then verify that the Fleet
connection state returns to online and that current inventory and telemetry are
available.

## Control job failures

Filter operations and Audit by action, inspect sanitized evidence, and verify the
recipe revision and build evidence are still current. Re-plan after correcting
persisted state; never retry a revoked or stale plan.

## Stale backup

Check `vonk_control_backup_successful`, the backup age, and the isolated restore
verification age. The backup loop writes its success marker only after the
PostgreSQL dump is restored successfully and the paired CA archive is copied to
the configured destination. A missing first backup fires
`ControlBackupNeverSucceeded`; follow [backup and recovery](../postgres-backups.md)
to inspect logs, storage capacity, and recovery files.

## Database unavailable

Check the private PostgreSQL healthcheck, secret-file mounts, storage capacity,
and migration version. Do not recreate desired profiles/models in PostgreSQL;
restore operational data. PostgreSQL remains the sole runtime authority and the
control plane mounts no Git repository.

## Worker lease starvation

`RunnableControlJobStarved` fires when the oldest queued control job that could
run now has waited more than five minutes. It is grouped by job kind, so a
running job of another kind no longer hides the starved one. A job deferred by a
future `observation_due_at` is an intentional wait and deliberately does not age
into the alert; if the alert fires, the wait is not the explanation.

Confirm the worker container is healthy and holds the online shared lock. Check
expired attempts and database connectivity. Stale fences must never be reused;
allow the durable queue to issue a new attempt.

## Stalled agent operation

`AgentOperationStalled` fires when `vonk_stalled_operations` reports a running
operation that declared measurable progress and has not advanced for at least
the stall window. A transfer phase is stall-able whenever bytes remain; a
non-transfer phase only when the operation itself declared an incomplete byte or
item total, so an unbounded build and an intentional `needs-operator` wait
never alert. Inspect the operation's progress and last accepted contact, then
reconcile the actual effect before retrying; an expired lease alone does not
prove the host action ended.
