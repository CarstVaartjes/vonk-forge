# Operate a PostgreSQL-authoritative GPU node platform

Onboard each GPU node independently with `node-install`; never place an address,
name, or assumed fleet size in code. After acceptance, emit its canonical fleet
record and submit it through the admin CLI or web UX. Model versions and recipes
follow the same immutable preview, evidence, and persisted proposal path.

The control worker reconciles only an accepted authority revision. It withdraws the
affected route first, leases stable node IDs in sorted order, applies exact
persisted revisions, verifies health, then atomically publishes Caddy/LiteLLM
state. A failure or withdrawal remains HTTP 503 maintenance.

Run the current control, recipe-route, and agent acceptance suites before
release. Physical Spark acceptance is recorded separately and is never inferred
from a simulator or a browser preview. The Library action preview remains the
operator gate for install, load, stop, and uninstall.

## Agent-derived availability and address changes

The dashboard reports `agent_state`, `agent_last_seen_at`, and `agent_online`
for each accepted node. Online means an active, non-revoked agent has made
an authenticated claim within 150 seconds. Raw observed management addresses
are intentionally omitted from the dashboard.

Installed agents find the control plane through the configured LAN DNS name and
initiate outbound mTLS long polling. The control plane does not scan the subnet.
It learns the direct peer address from the trusted Caddy boundary, validates it
against the management and direct-fabric CIDR policy, and associates the
observation with the certificate-bound `spk_` identity.

When DHCP changes an address, the next authenticated claim supplies the new
observation. Route reconciliation enters maintenance before validating and
publishing that replacement, so the prior address is not retained on failure.
DHCP reservations remain recommended for operational stability, but neither
PostgreSQL Fleet metadata nor Compose needs a hard-coded address for each GPU node.

## Hermes local-agent selection

Hermes always requests the single alias `hermes-agent` from LiteLLM. The worker
loads candidate order and maturity at the same immutable authority revision as active
reconciliation. It constructs endpoints from authenticated presence and
persisted workload ports; route payloads cannot supply addresses. Only
accepted, fresh, already-running local workloads are included. A failing
primary is withdrawn before a later reconciliation can publish a healthy
secondary. With no eligible candidate, the Hermes alias is omitted while
unrelated routes remain available. No cloud fallback exists.
