# Outbound GPU node agent and platform update design

## Purpose

Replace routine SSH orchestration with one production control path for every
GPU node fleet size:

```text
CLI or web UX -> control API -> durable fenced job -> outbound mTLS agent -> GPU node
```

SSH remains available only for initial enrollment, agent repair, GPU node
replacement, certificate recovery, and emergency rollback. There is no silent
fallback from the agent path to SSH.

## Trust and connection model

Each GPU node runs a small `vonk-forge-agent` systemd service and a minimal stable
supervisor. The agent initiates outbound HTTPS long-poll requests through
Caddy. GPU nodes expose no inbound Vonk Forge management port. Long polling is
preferred to WebSockets or gRPC because it survives proxies, service-host
restarts, and short disconnections without adding a second transport model.

After enrollment, every request uses mutual TLS. A client certificate is bound
to one immutable canonical `spk_` node ID. The API authorizes that identity
against the active Git-backed fleet record before returning work or accepting
evidence. A retired or revoked node cannot claim jobs.

The initial CA provider uses an offline root and a service-host intermediate.
Only the intermediate key is available to the control service, supplied as a
protected secret file. Node certificates are short-lived and renew before
expiry over the authenticated channel. The issuer is behind a provider
interface so Smallstep can replace the built-in issuer later without changing
the agent protocol. Vault is not an initial dependency.

## Enrollment

An administrator creates a short-lived, single-use grant for an intended node
ID. `node-install` uses SSH for the final routine time to install the pinned
agent and supervisor, the control-plane trust anchor, and the enrollment grant.
It does not copy a CA private key or reusable administrator credential.

The new agent connects outbound and submits bounded host-key, hardware, boot,
platform, and agent evidence. An administrator compares this with the
onboarding evidence and approves or rejects enrollment. Approval consumes the
grant and issues the node certificate; the agent durably removes the grant.
Replaying, reusing, or presenting an expired grant fails closed.

Replacement creates a new explicit enrollment decision. It never transfers an
old agent private key to different hardware or silently reuses an identity.

## Agent protocol

The versioned HTTPS protocol provides:

- agent registration, approval status, and certificate renewal;
- capability, protocol-version, Vonk Forge semantic version, build digest, and
  active A/B slot advertisement;
- node-targeted job claim with attempt fence and lease deadline;
- heartbeat, bounded progress, cancellation observation, and terminal result;
- content-addressed artifact retrieval; and
- signed, bounded execution evidence upload.

Every mutating request carries a job ID, attempt number, fence, target node ID,
eligible repository commit, operation, payload digest, and deadline. The agent
persists its active attempt before mutation and rejects stale fences, other-node
targets, unknown fields, expired work, incompatible protocol versions, changed
payloads, and secret-bearing payload keys.

The agent supports only a compiled registry of typed operations. The initial
legacy lifecycle vocabulary is:

- `node.probe`
- `release.install`
- `workload.prepare`
- `workload.start`
- `workload.stop`
- `workload.health`
- `workload.verify`
- `agent.update`
- `agent.rollback`

The clean-slate control-plane boundary removes the old package/deployment
operation vocabulary from orchestration. The retained compiled registry remains
the safe operation vocabulary, not a catalog of workload families, model IDs,
adapter IDs, images, checkpoints, or releases.

There is no arbitrary command, script, shell, environment, filesystem-path, or
repository-provided command-line operation in the network protocol. Each
operation validates its own structured inputs. Workload-specific adapters are
selected by an independently signed, content-addressed workload release lock
and execute unprivileged through the compiled package backend and adapter ABI.
Installing a new adapter, Mia/DS4 version, model, container, environment, or
checkpoint does not require an agent release when it uses that existing ABI.

## Control-plane orchestration

The API and worker remain separate services. PostgreSQL stores jobs, attempts,
leases, observations, agent sessions, certificate serials, update plans, and
bounded audit/evidence metadata. Git remains the sole desired-state authority.

The worker decomposes a reconciliation into deterministic node operations. An
agent claims only its own ready work; the API never pushes a connection into a
GPU node. Lease expiry permits a new fenced attempt only after operation-specific
verification determines whether retry, compensation, rollback, or operator
intervention is safe.

Multi-node work preserves declared worker/entrypoint order and does not publish
routes until every required agent result and acceptance gate succeeds. An
unavailable agent, revoked certificate, incompatible version, stale attempt, or
failed verification leaves affected routes withdrawn.

## CLI and web equivalence

The CLI and web UX are adapters over the same `/api/v1` resources. Routine
`vonkctl nodes status`, validation, preparation, switching, endpoint,
deployment, agent, and update commands authenticate to the control API. They
plan or enqueue the same jobs and expose the same authorization, audit, status,
and evidence as the web UX.

Neither interface contacts agents directly or receives agent credentials.
Structured CLI output includes plan and job digests so automation can poll or
stream bounded progress. If the control plane is unavailable, routine commands
fail; they do not fall back to SSH.

Bootstrap and recovery remain separate, explicit tools:

- `node-install` for first enrollment;
- `vonk-agent-repair` for broken agent/supervisor/certificate recovery; and
- `vonk-control-offline` for stopped-service control-host maintenance.

Legacy direct-controller behavior requires an explicit compatibility mode,
cannot be selected by availability failure, and is removed from the recommended
production workflow.

## Platform release and update model

A Vonk Forge release is one signed, content-addressed manifest containing:

- control API/worker image digests and web assets;
- Compose/configuration and database migration versions;
- agent and supervisor artifacts for supported GPU node architectures;
- node policy and runtime-tool artifacts;
- protocol and rollback compatibility ranges;
- SBOM and provenance digests; and
- required release evidence.

### Service-host update

The control plane does not replace itself through an ordinary API job. A
host-local `vonk-control-offline upgrade --release RELEASE --apply` verifies the
manifest, creates a recoverable backup, checks disk and compatibility, pulls
exact images, applies expand-compatible migrations, replaces API and worker in
a controlled order, verifies readiness through Caddy, and commits the active
generation. Failure restores the earlier image/configuration generation and
uses the documented database recovery boundary.

Migrations remain compatible with old and new application versions throughout
the update window. Destructive contract migrations occur only in a later
release after all old components have been removed and recovery evidence has
been accepted.

### GPU node update

Healthy agents update through `agent.update`, not SSH. The agent downloads the
exact artifact, verifies signature, digest, platform, release compatibility,
and available space, then installs into an inactive A/B slot. The stable
supervisor atomically selects the slot and starts it. Failure to reconnect and
pass self-tests within the deadline automatically returns to the previous slot.
The only working agent executable is never overwritten in place.

Control-plane updates precede GPU node updates and retain compatibility with both
the current and next agent protocol. Fan-out defaults to an explicit canary,
a configured soak interval, and batches of one node. The planner respects
workload topology and never updates every member of a distributed workload at
once. The first failure pauses the rollout; continuing after rollback requires
operator approval. Full fleet and representative model acceptance complete the
release.

After a service-host update, the control plane compares its active platform
version/build digest with every authenticated agent report. When the NAS is
newer, the web interface shows a persistent update prompt listing affected
GPU nodes, compatibility, active workloads, the proposed canary, batch order, and
rollback slot. It does not update nodes merely because versions differ.
Administrator confirmation creates the same signed topology-aware
`agent.update` plan exposed by the API and CLI. Compatible older agents may
remain online during the rollout; incompatible skew blocks affected mutations,
and offline nodes remain explicitly pending.

CLI and web use the same update plan and job APIs. The CLI surface is:

```text
vonkctl admin updates skew --json
vonkctl admin updates plan --release RELEASE --json
vonkctl admin updates apply --plan-digest DIGEST --json
vonkctl admin updates status JOB_ID --json
```

## Recovery boundary

SSH is allowed only when the agent channel cannot perform the task safely:

- initial installation;
- repair or replacement of an agent or supervisor;
- recovery of lost certificates or both corrupted A/B slots;
- GPU node hardware replacement; and
- emergency rollback when no agent can reconnect.

Recovery requires explicit apply flags, uses verified pinned artifacts, records
local bounded evidence, and imports that evidence into the control-plane audit
after service is restored. Normal agent updates, node probes, releases, model
operations, and profile changes never use SSH.

## Migration and acceptance

Migration proceeds without pretending the current SSH worker is the final
production boundary:

1. add the protocol contracts, CA provider, agent simulator, and fenced API;
2. implement the agent and stable A/B supervisor;
3. adapt control jobs and every routine CLI command to the API/agent path;
4. install agents using the existing SSH onboarding mode;
5. run direct and agent transports only through explicit compatibility tests;
6. complete physical enrollment, disconnect, restart, update, rollback,
   certificate rotation/revocation, replacement, and multi-node failure tests;
7. make agent transport mandatory for routine production operations; and
8. retain SSH only in audited bootstrap and recovery tools.

Release acceptance requires proof that routine CLI and web actions create the
same jobs, no routine path opens SSH, no agent accepts arbitrary execution,
stale attempts cannot publish success, A/B rollback survives a bad agent, and
one unavailable node cannot cause unsafe cluster-wide fan-out.

## Scope boundaries

The NAS/service host is not introduced into model-weight or tensor traffic.
Small signed runtime and agent artifacts may be served by the control plane;
model repositories and weights continue to use their defined immutable sources
and GPU node-local caches. The architecture imposes no fixed GPU node count, though
defaults are optimized for small administrator and node populations.

Fleet/Library workload state is independent from this platform update plane.
`agent.update` updates Vonk Forge itself; it must never be used to deliver an
ordinary workload family, adapter, runtime, container, dependency, or model
release.
