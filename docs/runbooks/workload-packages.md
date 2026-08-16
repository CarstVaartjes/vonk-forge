# Generic workload package operations

This runbook covers the retained signed package plane for generic node
components. It is deliberately not a model or runtime release procedure.
Catalog and Library are the operator path for model recipes: local PostgreSQL
owns recipe revisions, imports, install plans, placements, and runs, while the
optional public recipe library supplies immutable data to import. Do not use
the package promotion flow to author, install, or activate a model recipe.

A workload package is a signed, content-addressed description of a generic
component that uses the stable node-package ABI. It records immutable source or
OCI inputs, environments, configuration, and validation evidence. The package
plane stays independent of `vonk-forge` platform releases, but it does not
provide a second model, adapter, or runtime authority.

The NAS is the administration and authority host. Its Docker services (the
API/worker, PostgreSQL, Caddy, LiteLLM, Hermes, Prometheus, and Grafana) are
separate services and are updated by the host-local platform updater. GPU nodes
run the outbound mTLS agent and keep large model payloads in their local
content-addressed stores. Payloads are fetched directly from the declared,
authenticated Git, HTTPS, OCI, or other approved provider; the NAS is not a
model-weight relay.

## Release planes and trust boundaries

Keep the two release planes independent:

| Plane | Authority | Updates | Does not update |
| --- | --- | --- | --- |
| Platform | platform TUF and the signed platform manifest | NAS Docker generations, the GPU node agent/supervisor, protocol and privileged helper ABI | recipe revisions, model artifacts, execution harnesses, or ordinary generic packages |
| Generic package | NAS-admin Git/TUF repository and signed package locks | generic package families, images, environments, configuration, and deployment plans | recipe catalog identity, model artifacts, harnesses, the agent, supervisor, platform services, or SSH configuration |

The GPU node agent contains a stable, typed package ABI and safe operation
vocabulary. It must not contain a catalog of model names, recipes, or adapter
versions. The package trust root authorizes immutable release-lock targets; node policy
authorizes only the ABI operations and declared capabilities. The normal path
never uses SSH, `agent.update`, platform TUF, or a control-plane file copy.
SSH remains available for one-time onboarding and explicitly documented
recovery only; recovery commands and their evidence must identify that
exception.

## Before publishing

1. Create a generic package family document with a stable `family_id`, declared
   architecture/OS/capabilities, license and credential requirements, and a
   dependency graph. Do not add a model-specific branch to the agent.
2. Build each component from an immutable source revision or digest. Record
   the exact source, media type, byte size, unpacked size, platform, and
   content digest. Never use a mutable tag as an identity.
3. Produce a canonical release lock that includes the package ABI, compatibility
   constraints, validation steps, provenance, and all component digests.
4. Run local lint, license, provenance, capacity, and architecture checks. Keep
   secrets out of the lock, command line, logs, and evidence.
5. Sign and publish the lock and TUF metadata from the NAS administration
   workflow. The public Git/TUF state is the reviewable source; a local edit is
   not an authorized release.

## Candidate review and promotion

The CLI and web Admin → Workload packages expose the same API. Commands are
plan-first and return a digest that an administrator must review.

```bash
# Discover and inspect signed candidates (no GPU node mutation)
vonkctl admin packages candidates list --family synthetic-stack --json
vonkctl admin packages candidates get --candidate CANDIDATE_UUID --json

# Validate and preview promotion. Keep the returned plan digest.
vonkctl admin packages validation-preview --candidate CANDIDATE_UUID --json
vonkctl admin packages validate --candidate CANDIDATE_UUID \
  --plan-digest VALIDATION_PLAN_DIGEST --json
vonkctl admin packages promote \
  --candidate CANDIDATE_UUID \
  --preview-digest sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --json

# The web UI presents the same candidate lock, evidence, and plan digest.
```

Promotion is rejected when the lock is unsigned, unapproved, malformed,
revoked, incompatible, or not the exact candidate selected by the signed
review. A successful promotion records the immutable release digest, source
commit, validation evidence, approver, and audit/job links. It does not fetch
model weights to the NAS or alter a GPU node.

## Rollout and progress

Select a repository-declared deployment and review the topology-aware plan.
The plan shows canary nodes, batches, offline nodes, storage/download
requirements, compatibility, and the predecessor release.

```bash
vonkctl admin deployments rollout-preview \
  --deployment synthetic-canary --json
vonkctl admin deployments rollout \
  --deployment synthetic-canary \
  --plan-digest sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  --json

vonkctl admin deployments status --deployment synthetic-canary \
  --rollout ROLLOUT_ID --json
vonkctl admin deployments repair-preview --deployment synthetic-canary --json
vonkctl admin deployments repair --deployment synthetic-canary \
  --plan-digest REPAIR_PLAN_DIGEST --json
```

The control plane queues typed package operations over each GPU node's outbound
mTLS channel. A node resolves the signed lock, reserves storage, acquires
components directly from their providers, verifies every digest and size,
materializes a new immutable generation, then performs prepare, activate, and
health checks. Progress is durable and bounded: byte counters, phase,
attempt, cancellation, restart recovery, and operation/fence IDs survive an
agent restart. The currently active generation remains serving until the new
generation passes activation and health.

Canary failure pauses later batches. A rollback selects only the recorded
predecessor release and is itself a fenced, audited package operation. If the
predecessor is unavailable or a rollback cannot be proved safe, the rollout
enters `waiting-for-operator`; it never silently selects an arbitrary older
generation.

## Rollback, repair, and Garbage collection

```bash
vonkctl admin deployments rollback-preview \
  --deployment synthetic-canary --rollout ROLLOUT_ID --json
vonkctl admin deployments rollback \
  --deployment synthetic-canary \
  --rollout ROLLOUT_ID \
  --plan-digest ROLLBACK_PLAN_DIGEST \
  --json

vonkctl admin packages gc-preview --json
vonkctl admin packages gc --plan-digest GC_PLAN_DIGEST --json
```

Rollback is possible offline when the predecessor generation and its verified
objects are present on the GPU node. Stop network access only after recording the
release and generation digests; the agent must not silently re-download or
fall back to a NAS copy. Repair reconstructs missing indexes from verified
objects and refuses path traversal, symlink substitution, digest mismatch, or
an unapproved lock. GC is preview-first, keeps active and recorded rollback
generations, and is resumable after interruption.

## Credentials, licenses, and provider outages

Credentials are references to root-controlled secret stores, never values in
Git or a package lock. The operator grants only the provider and release
needed for the operation. License evidence is reviewed before promotion and
is retained by digest. A missing credential, license, network, or provider
object produces a typed, redacted failure with a retry/compensate/operator
disposition; it does not expose a token or mark a partial download complete.

Downloads use bounded, resumable ranges with durable progress, reservation,
atomic promotion, cancellation, and restart recovery. A failed or cancelled
transfer remains outside the active generation until its digest and size are
verified. Providers are contacted by GPU nodes through their authenticated
outbound route; the NAS control worker does not proxy arbitrary URLs.

## NAS platform updates and GPU node skew

NAS Docker services and GPU node worker code have separate update actions. When
the NAS reports a newer compatible `vonk-forge` platform release than one or more
GPU nodes, the web Admin → Updates page and `vonkctl admin updates skew --json`
show the exact versions, affected nodes, signed target digest, and a
topology-aware fan-out preview. The operator must explicitly confirm the
signed `agent.update` command for each eligible GPU node (canary first, then the
remaining nodes). The command uses the outbound mTLS agent channel and A/B
supervisor; it does not use SSH.

```bash
vonkctl admin updates skew --json
vonkctl admin updates plan --target-version 2.0.0 --json
vonkctl admin updates apply --plan-digest PLAN_DIGEST --json
vonkctl admin updates status --json
```

An older, compatible GPU node agent may continue serving ordinary generic
packages while the operator reviews the skew prompt. A generic package never
triggers this prompt: only a platform capability/protocol/agent update does.
If a platform update is required for a package's genuinely new privileged ABI,
the candidate must state that compatibility requirement and the UI must show
the separate platform action before rollout.

## Recovery-only SSH

SSH is permitted only for one-time bootstrap, certificate/key recovery, or a
host that cannot establish its outbound agent channel. Record the operator,
target node, reason, start/end time, command digest, and recovery evidence.
Do not use SSH to install a workload, copy a model, run a routine update, or
repair a rollout. After recovery, rotate the affected credential/certificate,
restore the outbound channel, and verify the node's platform/workload state
through the control plane.

## Evidence and first-release gate

The first release requires both independent acceptance sets:

- the unknown-family flow creates a family after the installed agent was built,
  publishes signed release 1, activates release 2, rolls back to release 1
  while offline, and rejects unsigned/unapproved input;
- the failure/recovery matrix covers transport, trust, capacity, activation,
  health, cancellation, GC, restart, canary, and concurrent-download cases.

The workload evidence must explicitly record that no SSH or `agent.update` call
occurred and distinguish simulated from physical evidence. The platform
release verifier combines these reports with platform update evidence; it does
not treat a simulator as physical GPU node acceptance.

```bash
scripts/accept-workload-packages --mode simulated --json
scripts/accept-workload-package-failures --json
scripts/verify-platform-release --candidate 1.0.0 --json
```

A blocked result names the exact missing gate. Keep the redacted JSON output,
source commit, release digests, and test command in the protected release
artifact. Hosted CI stores the two canonical outputs as
`workload-package-acceptance.json` and
`workload-package-failure-matrix.json`; the release verifier checks their
content digests instead of trusting an unrecorded console result. Do not
synthesize physical hardware evidence; record it later with the approved GPU node
inventory procedure.
