# Repository-backed platform administration

This runbook covers the existing Git/TUF-backed platform and workload-release
path. Git remains authoritative for fleet nodes, topology, platform policy,
and the release projection described below. PostgreSQL is authoritative for
local recipe catalog entries, immutable recipe revisions, imports,
installations, placements, and runs; those records do not require a commit,
branch, or pull request and remain available when the remote is unreachable.
Use the catalog/API runbooks for recipe authoring and WorkloadRun import. Never
turn a recipe operation into a Git change merely to satisfy this runbook.

## Inspect and propose

Use either the web application or `vonkctl admin`. Both call `/api/v1` and
produce the same canonical proposal bytes. Every proposal pins a full 40-hex
base commit, operates only on allowlisted typed documents, and shows validation,
affected targets, and a diff before submission.

```bash
vonkctl admin fleet --json
vonkctl nodes status --json
vonkctl admin proposal --file change.json --json
```

Before the first real release, an administrator may explicitly submit a signed,
audited direct commit. Enabling `release-pr-only` at the first release is a
one-way transition. From then on, submission creates `vonk-control/<digest>` and
a pull request; it never force-pushes or deploys an unreviewed branch.

## Reconcile

Only a platform/release commit reachable from the protected deployment branch
with every exact required check in the successful state is eligible. Planning pins that commit,
sorted node targets, placements, routes, immutable releases, and all input
digests. Execution rechecks eligibility immediately before any node mutation.
The worker also reconstructs that exact plan from the checked-out
`inventory/reconciliation.json`; caller-supplied content cannot create or alter
a reconciliation job through the generic jobs endpoint. Every route target
must independently exist with lifecycle `ready` in the checked-out
`inventory/fleet.toml`.

Affected routes enter maintenance before work begins. Node leases are acquired
in sorted stable-ID order. Workloads must pass health and acceptance before
routes publish atomically. A failed apply, verification, stale lease, revoked
check, or changed digest fails closed: affected routes remain withdrawn and the
job/audit records explain the bounded failure.

Each `routes` entry in the commit-pinned reconciliation document names an
alias, the certificate-bound target identity, and a repository workload. It
does not contain an address or port:

```json
{
  "routes": {
    "deepseek": {
      "node_id": "spk_0123456789abcdef0123456789abcdef",
      "workload": "deepseek-agent-single",
      "requests_per_minute": 30,
      "tokens_per_minute": 10000
    }
  }
}
```

The worker resolves the port from that exact commit's
`config/workloads/<workload>.toml`, resolves the address from fresh
certificate-authenticated presence, and probes `/v1/models` with the
file-backed upstream credential. It then writes a generated JSON-as-YAML config
to the dedicated `litellm-routes` volume. LiteLLM's in-container supervisor
restarts the proxy only when that atomic file changes. The config is paired
with a SHA-256-bound lease whose expiry cannot exceed the oldest source
observation's 150-second lifetime. The supervisor accepts only a lease issued
after its own process started. Every 60 seconds the worker repeats presence
resolution and the probe; an expired lease or agent observation, dead worker,
failed probe, changed checkout, or invalid definition selects the empty
bootstrap config. On a DHCP address change, maintenance is live before the
replacement address is probed.

Never use `vonk-control-offline` for ordinary repository administration. Its
exclusive lock and stopped-service proof are only for bootstrap and recovery.
