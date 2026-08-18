# Observe `vonk-forge`

Grafana is available only through Caddy at `/grafana/`. Prometheus has no
published port. Platform metrics use stable generated node IDs and bounded enum
labels; they never include prompts, responses, credentials, hostnames, private
addresses, raw serials, request IDs, or job IDs.

Operational JSON logs are rotated by Docker's local driver. Remote output is
redacted and truncated before persistence. Full sanitized job evidence is
content-addressed and available only to operator/administrator API roles.

## Routes stuck in maintenance

Inspect the reconciliation job and affected-node health. Keep routes withdrawn
until the pinned commit, releases, leases, and acceptance checks all pass. Do
not manually point LiteLLM at an unaccepted GPU node endpoint.

## Stale node probe

Check worker health, cluster-egress connectivity, the node's stable ID, and its
latest observation timestamp. A hostname or address change must be updated via
the fleet repository proposal; it must not create a new node identity.

## Reconciliation failures

Filter operations and Audit by action, inspect sanitized evidence, and verify the
protected commit is still eligible. Re-plan after correcting repository state;
never retry a revoked or stale plan.

## Stale backup

Run the encrypted backup command, then restore it on a disposable host. The age
metric advances only after encryption and manifest creation succeed.

## Database unavailable

Check the private PostgreSQL healthcheck, secret-file mounts, storage capacity,
and migration version. Do not recreate desired profiles/models in PostgreSQL;
restore operational data and retain Git as authority.

## Worker lease starvation

Confirm the worker container is healthy and holds the online shared lock. Check
expired attempts and database connectivity. Stale fences must never be reused;
allow the durable queue to issue a new attempt.
