# Bootstrap the control plane

For a NAS production deployment, begin with the authoritative
[Docker control-host deployment guide](../../deploy/compose/README.md). It
covers root-owned host state, site-local configuration and secrets,
Tailscale-only access, the production step-ca overlay, first selection, and
rollback. This runbook provides control-plane context, but the deployment guide
owns the executable bootstrap sequence so operators have one source of truth.

The control plane runs on any Docker Compose-capable Linux machine. The first
host may be a NAS, but the configuration has no NAS vendor dependency.

Choose a stable LAN address for the Docker service host and set it as
`NAS_LAN_IP` (the variable name is retained for compatibility; the host need
not be a NAS). Create local-only DNS records
`enroll.vonk-forge.lan`, `agents.vonk-forge.lan`, and
`registry.vonk-forge.lan`, all resolving to that address. Set
`VONK_MANAGEMENT_CIDRS` to the actual canonical GPU node management network(s), and
permit TCP 8443 to the service-host address only from those networks or the
reserved GPU node leases. Do not expose LAN ports for control, inference, Grafana,
or Hermes.
GPU node reservations are recommended, but no GPU node IP belongs in Compose or fleet
identity: authenticated agent presence supplies the current validated address.

1. Prepare `/srv/vonk-forge/control-host` and `/srv/vonk-forge/site` with their
   documented root ownership and copy the non-secret settings from
   `deploy/compose/.env.example` to `/srv/vonk-forge/site/.env`. The selected
   platform target, not this file, supplies image and deployment assets.
2. Create the database URL, PostgreSQL password, token-signing-key, Tailscale
   OAuth, and Hermes API-key files outside Git. Restrict them
   to the service administrator. Follow the [Tailscale](tailscale.md) and
   [Hermes Agent](hermes-agent.md) runbooks for their exact preparation.
   Generate the Caddy/control proxy-auth secret as an unpadded base64url token
   of at least 32 characters (an optional final CR/LF is accepted):

   ```bash
   umask 077
   openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' > /srv/vonk-forge/secrets/agent-proxy-auth
   openssl rand -base64 32 | tr '+/' '-_' | tr -d '=' > /srv/vonk-forge/secrets/worker-api-token
   ```

   Spaces, internal line breaks, padding, and other punctuation are rejected
   by both Caddy and the control API.
3. Run the exact root-owned updater commands in
   [Install and first selection](../../deploy/compose/README.md#install-and-first-selection).
   Preview `upgrade --target-name` first, then apply the same exact target. The
   updater validates the TUF-authorized manifest, verified generation, OCI
   bundle, images, site state, backup, migration, and readiness while holding
   one operation lock. Production never executes Compose from the repository checkout,
   and operators must not reproduce the updater sequence manually.
4. After the guide's successful first selection, check `/api/v1/healthz`
   through the `svc:vonk-forge` Tailscale Service.

   The base file deliberately has no CA provider selection, so it is not a
   runnable production configuration. A development-only generation may use
   `compose.builtin-ca.yaml`; production selects `compose.step-ca.yaml`.
   Select exactly one provider overlay. Combining both overlays is rejected by
   the control API at startup regardless of their order.

The API and worker are separate targets built from the same release commit and
remain separate services. The API image contains Git/OpenSSH for signed
repository administration. The worker image contains neither Git nor OpenSSH,
mounts no repository or Git key, and has no GPU node-facing network.
PostgreSQL, Caddy, LiteLLM, Prometheus, Grafana, Tailscale, and Hermes Agent are
independent containers in this one project. Only Caddy publishes a host port,
and that is the `10.0.0.2:8443` GPU node backend. The Tailscale gateway publishes
no Docker port and advertises separate `vonk-forge`, Hermes dashboard, and Hermes
API Services.

Caddy receives tailnet web traffic on the private `tailnet-web-edge` network
and authorizes inference against the current route-serving lease. It reaches
LiteLLM only over the dedicated internal `litellm-edge` network. LiteLLM then
reaches only the accepted, fresh agent-derived GPU node endpoint via
`cluster-egress`; Docker routes that connection out through the NAS LAN. Model
and tensor runtimes remain on the Vonk Forge GPU nodes, and direct-fabric
traffic never passes through the NAS.

Hermes reaches `caddy:8081/v1` over `hermes-inference`; Caddy applies the same
lease check and then proxies to LiteLLM over `litellm-edge`. Only Caddy and
LiteLLM share `litellm-edge`, so Hermes cannot resolve or dial LiteLLM directly
and no ingress-network direct path is supported. Hermes uses the fixed
`hermes-agent` alias. Apply and verify `bin/harden-hermes-egress` after Docker
creates the bridge so terminal tools cannot connect directly to GPU node
management/fabric networks or sibling control-plane networks.

The checked-in LiteLLM file is a fail-closed empty bootstrap. The API retains
Git authority and evaluates current-head, eligibility, and commit-pinned Hermes
policy for the repository-less worker over a dedicated internal two-party
network. Requests and short-lived responses are nonce-bound and HMAC-authenticated
with the independent `worker-api-token`; Caddy denies every `/internal/*` path.
After a successful commit-pinned reconciliation, the worker derives the live
config from stable
`spk_` identity, fresh authenticated presence, repository workload ports, and a
successful upstream probe. The worker writes only to the dedicated
`litellm-routes` volume; LiteLLM mounts it read-only and reloads by supervised
process restart. Each generated config has a hash-bound expiry lease. The
supervisor rejects leases from before its own startup and falls back to the
empty bootstrap when a lease expires, so a dead worker or restored route volume
cannot keep an upstream published indefinitely. The worker refreshes the
generation every 60 seconds, so a DHCP change follows the next authenticated
observation and stale presence withdraws the route within the 150-second
window.
