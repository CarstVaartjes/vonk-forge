# Observe `vonk-forge`

Grafana is available only through Caddy at `/grafana/`. Prometheus has no
published port. Platform metrics use stable generated node IDs and bounded enum
labels; they never include prompts, responses, credentials, hostnames, private
addresses, raw serials, request IDs, or job IDs.

Operational JSON logs are rotated by Docker's local driver. Remote output is
redacted and truncated before persistence. Full sanitized job evidence is
content-addressed and available only to operator/administrator API roles.

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

Run the encrypted backup command, then restore it on a disposable host. The age
metric advances only after encryption and manifest creation succeed.

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
item total, so an unbounded build and an intentional `waiting-for-operator` wait
never alert. Inspect the operation's progress and last accepted contact, then
reconcile the actual effect before retrying; an expired lease alone does not
prove the host action ended.
