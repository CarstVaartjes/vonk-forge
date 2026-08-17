# Route Serving Lease Authority Design

## Status and scope

This design closes the remaining Task 8 serving-boundary defect. It changes
only route admission and the LiteLLM network boundary. The execution-harness
catalog remains v1, migration `0027_execution_harness_catalog` remains a fresh
pre-production schema fence, and no legacy data migration or compatibility
reader is introduced.

## Root cause

The current supervisor treats `Popen.kill()` as the route lease authority. A
timer attempts to kill LiteLLM at expiry and clears its acknowledgement, but an
`OSError` is suppressed. The already-listening child can therefore continue to
accept requests after the route lease expires. Timer scheduling also makes the
deadline a cleanup approximation rather than a request-admission rule.

The same mechanism stores one expiry when the child starts. A later generation
with identical LiteLLM configuration is acknowledged without replacing that
expiry, so a legitimate lease renewal can still be killed at the old deadline.

Process termination is useful cleanup, but it cannot be the security boundary.

## Considered approaches

1. **Caddy request-time authorization backed by the supervisor — selected.**
   Caddy already owns every supported browser and internal inference edge. A
   small supervisor endpoint can evaluate the current lease for every request;
   Caddy forwards only on a `2xx` result. This is a standard `forward_auth`
   deployment and fails closed when the authority is unavailable.
2. **Make the Python supervisor a full reverse proxy.** This would let it own
   port 4000 directly, but correctly proxying streaming HTTP, disconnects,
   request bodies, and LiteLLM UI behavior would recreate mature Caddy behavior
   in application code.
3. **Exit the container when kill fails.** This improves cleanup but remains a
   scheduler- and container-runtime-dependent approximation. It does not make
   the lease an admission rule and does not solve renewal by itself.

## Architecture

### Lease authority

Both the production and packaged-development LiteLLM supervisors host a small
HTTP authority on port `4001`. It is not published to the host. Its only
decision endpoint is `GET /vonk/route-lease`:

- `204` when the loaded child is healthy and either uses the immutable empty
  bootstrap configuration or has an active route lease whose `expires_at` is
  strictly later than the authority's current UTC time;
- `503` for startup, reload, unhealthy child, malformed state, expiry, or an
  explicitly denied generation;
- `404` for every other path and method.

The authority stores one immutable state snapshot behind a lock. An active
snapshot binds generation, activation digest, LiteLLM digest, and expiry. It is
denied before stopping or replacing a child. A same-configuration renewal
atomically replaces the snapshot and rearms cleanup before acknowledgement, so
the superseded deadline has no authority.

The exact guarantee is request admission: a request whose Caddy authorization
check begins at or after `expires_at` is never forwarded to LiteLLM. Requests
admitted before expiry may finish; the existing timer still attempts to kill
the child at expiry to terminate ongoing work and reclaim resources, but that
best-effort cleanup is no longer the admission boundary.

### Caddy boundary

The production and packaged-development Caddyfiles define one shared
`litellm_route_lease` snippet using:

```caddyfile
forward_auth litellm:4001 {
	uri /vonk/route-lease
}
```

The snippet runs before every `/v1/*` and `/litellm/*` reverse proxy. Caddy's
documented behavior continues only for a `2xx` authorization result; a `503`
is returned unchanged, and an unavailable authority fails with a proxy error
without contacting LiteLLM.

Caddy also owns an internal `:8081` listener that accepts only `/v1/*`, applies
the same lease authority, and forwards to LiteLLM. Hermes uses
`http://caddy:8081/v1`. Development's optional loopback inference port maps to
Caddy `8081`, not LiteLLM `4000`, preserving the SSH-tunnel interface without a
bypass.

### Network isolation

A dedicated internal `litellm-edge` Docker network is shared only by Caddy and
LiteLLM. LiteLLM is removed from generic ingress and Hermes client networks.
Caddy joins the Hermes inference network and bridges authorized requests to
`litellm-edge`; Hermes can no longer resolve or dial `litellm:4000` directly.
Database and bounded upstream-egress networks remain unchanged.

This makes Caddy plus the supervisor authority the sole supported data path,
not merely the path selected by configuration.

## State transitions

1. Supervisor starts the authority in denied state.
2. Supervisor validates the selected bundle and starts LiteLLM.
3. After health succeeds, it installs either bootstrap-allowed state or the
   exact active lease, then writes acknowledgement for active state.
4. For an unchanged-config lease renewal, it atomically installs the new
   snapshot, rearms cleanup, and writes the new acknowledgement without
   restarting LiteLLM.
5. At expiry, request-time checks deny immediately. Timer cleanup clears the
   acknowledgement and attempts to terminate the child.
6. Before reload, health failure, shutdown, or invalid candidate handling, the
   authority denies first; process cleanup follows.
7. If the authority or supervisor disappears, Caddy cannot authorize and fails
   closed.

## Testing

- Unit tests exercise exact-before/exact-at expiry, invalid state, bootstrap,
  denial-before-cleanup, and same-config renewal in both supervisor copies.
- A real Caddy container test keeps an upstream HTTP child alive deliberately,
  verifies `200` before expiry, verifies non-`2xx` at and after expiry, and
  verifies failure closure when the authority disappears.
- A renewal test proves the same child remains admitted after the old deadline
  and is denied at the renewed deadline.
- Compose tests prove LiteLLM publishes no port, Caddy owns the development
  loopback inference mapping, Hermes targets Caddy, and only Caddy shares the
  internal `litellm-edge` network with LiteLLM.
- Existing recovery, supervisor, Caddy route, development Compose, runbook,
  supply-chain, and Task 8 suites remain green.

Physical ARM64/GPU/two-Spark/RoCE acceptance remains Task 9 and begins only
after this boundary passes independent review.
