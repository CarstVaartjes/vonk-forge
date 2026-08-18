# Repository-backed platform administration

Git remains authoritative for platform source, fleet/topology policy, and
release trust. PostgreSQL is authoritative for the local v1 recipe records,
immutable revisions, installations, placements, and runs. Do not turn a
recipe edit or activation into a Git change.

## Inspect the platform boundary

Use the browser Fleet and Library views for normal operation. The generated
API clients expose the same read-only platform projections for maintainers and
automation, but the supported operator surface remains the authenticated web
workflow.

Platform proposals pin a full commit, typed document changes, affected targets,
and all input digests. A proposal is reviewable before submission and cannot
select a model version, recipe revision, or node by hostname.

## Platform changes

Only a commit reachable from the protected deployment branch and passing every
required check is eligible for a platform change. The worker rechecks that
eligibility immediately before mutation, leases stable node IDs in order, and
fails closed if the commit, evidence, agent identity, or route lease changes.

Model and runtime changes use the v1 Library workflow instead:

1. Create or import a recipe draft in Library.
2. Resolve the exact model-version, harness, runtime, patch, and topology
   identities into an immutable revision.
3. Attach source/build and physical acceptance evidence.
4. Map the revision to compatible enrolled nodes.
5. Preview and apply install/load/start from Library.

The recipe route publisher derives LiteLLM and Caddy state from the published
run. It never reads `config/workloads`, `config/cluster-profiles`, a model
maturity report, or an operator-supplied address. Hermes is available only when
an accepted v1 run owns the exact `hermes-agent` alias.

## Recovery boundary

Do not use `vonk-control-offline` for ordinary recipe or platform changes. Its
exclusive lock and stopped-service proof are reserved for documented bootstrap
and recovery operations. Never use SSH to bypass a queued preview, route lease,
or evidence gate.
