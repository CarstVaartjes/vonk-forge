# Engineering principles

These are the standing engineering commitments behind Vonk Forge. They are
durable rules, not evidence: a passing test, a green pipeline, or a cache
operation never stands in for physical Spark qualification.

These principles and the [architecture overview](architecture-overview.md)
define current direction. Dated product briefs preserve product rationale;
they do not override current storage, security, or execution contracts.

## Simplicity is a load-bearing choice

The target deployment is one Docker Compose application on one service host,
one PostgreSQL service, and a small number of GPU nodes and administrators.
That scale does not justify distributed infrastructure.

- A separate message broker, workflow cluster, service mesh, workload-identity
  server, or mandatory Vault cluster would add more failure modes than it
  removes Vonk Forge code.
- The platform is one Compose application of separate containers. It is neither
  one container holding every dependency nor a network of custom microservices.
- Kubernetes, a service mesh, an event bus, and custom microservices stay
  explicit non-goals.
- GPU nodes run workloads. Caddy, LiteLLM, databases, monitoring, and the admin
  UI do not run on a GPU node.

Simplicity is not licence to skip the standards-heavy, security-sensitive
areas. PKI, software-update trust, artifact distribution, configuration
application, and GPU telemetry earn real implementations precisely because
they are where shortcuts become incidents.

## Each fact has one owner

PostgreSQL earns its place through transactions, concurrent operation ownership,
security state, reservations, durable desired state, and operational queries.
The Controller and LiteLLM use separate databases in that one service. Keep
those responsibilities; use filesystem storage for artifact bytes and their
local recovery facts. Do not introduce a database for facts whose simplest
reliable owner is managed storage.

The [ownership table](architecture-overview.md#state-ownership) defines the
boundary. In the target design, model and image manifests, verification
receipts, and download/build checkpoints travel with their managed files as
canonical typed JSON. An exact digest in a profile is a reference, not a second
copy of the artifact's availability state. UI indexes may be rebuilt from the
owner; they cannot independently admit missing bytes or require manual repair.

Self-descriptive files need explicit crash and concurrency guarantees: bounded
validated records, one writer per artifact, durable data before publication,
atomic visibility, and rejection of stale writers. Keep coordinated job intent,
leases, cancellation, and security decisions in PostgreSQL. File storage must
not grow another implementation of a transactional job database.

The [implementation plan](plans/resilient-artifact-storage.md) records the
completed model-availability and image-receipt ownership changes. Complete each
future ownership change across producers and consumers and remove the retired path;
do not maintain two authorities during a compatibility period.

## Stability comes from one current path

- Coordination must have an acyclic wait graph: work cannot wait on a dependency
  that needs a lock, slot, or reservation held by that same waiting work. Follow
  the [coordination boundaries](architecture-overview.md#coordination-and-deadlock-prevention).
  Use one lock order, short database transactions, nonblocking claims, and
  bounded waits. A timeout is a recovery mechanism, not proof that cycles
  cannot exist.
- Keep one current definition and one current execution path per document and
  operation. Retired parsers, DTOs, aliases, and fallback shapes are removed
  together with their callers rather than carried forward.
- State before controls: show health, current workload, capacity, and
  live-versus-desired difference before asking for a decision.
- Preview and apply are the same decision. A preview binds the exact plan the
  apply executes, and a stale plan is rejected rather than re-interpreted.
- Recovery belongs to the durable operation that owns the work. Persist child
  identity and progress, adopt existing effects before consulting mutable
  admission, and resume unfinished work after a restart.
- A later explicit workload request supersedes older overlapping intent.
  Children inherit their parent's authority; automatic recovery must never
  promote an old request above a newer operator decision.
- Temporary dependencies recover through bounded retries with a visible next
  attempt. Uncertain effects require exact observation or a safe same-effect
  retry. Invalid authority, invalid contracts, denied access, and integrity
  failures remain explicit blockers.
- Preserve completed work and partial transfers. Managed cache receipts make
  reuse cheap; ordinary retries must not repeat downloads, full-file hashing,
  image imports, or package installation without a reason.
- Consequential fleet-wide platform changes proceed one Spark at a time.
  Recipe profile changes stay topology-atomic.
- Exact cross-site image reproducibility is desirable but never silently
  assumed. When a local result differs from a publisher's tested result for the
  same recipe, Vonk shows the difference and withholds the claim.
- Model files and runtime images are reconstructible caches. After a restore
  or interrupted transfer, reconcile recorded availability with managed storage,
  re-download missing model or published image assets, and rebuild missing
  source images through the existing preparation path. Preserve completed files
  and resumable transfers. A historical success record must not prevent repair
  or make absent bytes look ready. A rebuilt image receives its own verified
  identity; it must not silently replace the image in an already bound plan.
- A failed attempt never permanently poisons an artifact identity. Reuse valid
  results, resume compatible partial work, and safely replace damaged temporary
  work. A failed refresh or rebuild preserves the last verified result.
- Automatic retries stay within current intent and authorization. Bound their
  rate and resource use, expose why work is waiting, and resume when a temporary
  dependency recovers. Do not turn a finite attempt budget into a permanent ban
  on a fresh authorized request. Cancellation and newer intent remain final.
- Recovery and cleanup are scoped to the affected object or operation. One
  malformed record must not stop unrelated work. Unavailable references or a
  denied scan defer cleanup; they never authorize deletion of possibly used data.

## Security is fail-closed and least-authority

- `PermissionDenied` during a directory scan is never treated as an absent
  entry; that would be fail-open against the declared storage limit.
- An overlong grant is rejected rather than silently clamped. A caller's
  invalid request must not be quietly repaired into a valid one.
- The update-receipt signing key is not an administrative authorization
  credential. Action grants come from a separate authority key that is never
  mounted into the worker or the signer.
- Candidate tooling is never used to install itself. A release may supply its
  successor updater for a future transaction, and host-tooling compatibility is
  an explicit release-manifest range.
- The platform update plane is separate from the workload plane.
  `agent.update` updates Vonk Forge itself and must never deliver an ordinary
  workload family, adapter, runtime, container, dependency, or model release.
- Routine operation does not require SSH. The Spark agent connects outbound,
  and SSH stays diagnostic and bootstrap-only.

## Every frontier recipe must remain runnable

The product promise is being able to run any model on local Spark capacity. A
curated library is what makes that safe, so the library exists to keep frontier
recipes reproducible rather than to restrict them.

- A recipe binds model, runtime, topology, capacity, source, and qualification
  facts to immutable identities, so "any model" means one exact, comparable
  artifact set rather than a loose configuration.
- The NAS/Controller cache is the authority for what a profile may place.
  Missing assets are actionable blockers with a prepare-cache action; they are
  never deferred to a Spark.
- Profile admission trusts the durable verification receipt for an immutable
  object in managed NAS storage, checking file presence, type and length.
  It does not rehash model weights on each read or after a Controller restart;
  publication, transfer and explicit verification retain their content checks.
- Run admission enforces the recipe's declared system memory reserve alongside
  its workload demand and existing reservations. It does not impose an
  additional fixed platform reserve that prevents a fitting recipe from running.
- The operator experience stays model-first: discovery starts from a model or
  task, and recipes are the exact ways to make it runnable on this fleet.
- Labels and grouping metadata stay cross-cutting so filters, saved scopes, and
  profile targeting compose instead of forming a rigid hierarchy.

## Operating-model vocabulary

The interaction model adopts proven patterns while keeping Vonk Forge's safety
boundary: Kubernetes labels and selectors for cross-cutting groupings,
Tailscale Machines for search and health filtering, Grafana variables for
fleet-wide filters that stay consistent and shareable, Argo CD's live-versus-
desired diff as the language of preview, and Nomad node pools plus progressive
rollout for placement and one-at-a-time fleet change.
