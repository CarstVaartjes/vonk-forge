# Engineering principles

These are the standing engineering commitments behind Vonk Forge. They are
durable rules, not evidence: a passing test, a green pipeline, or a cache
operation never stands in for physical Spark qualification.

The commitments were recovered from the dated design set that preceded this
document, so the reasoning survives even though those point-in-time briefs do
not.

## Simplicity is a load-bearing choice

The target deployment is one Docker Compose application on one service host,
one PostgreSQL database, and a small number of GPU nodes and administrators.
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

## Stability comes from one current path

- Keep one current definition and one current execution path per document and
  operation. Retired parsers, DTOs, aliases, and fallback shapes are removed
  together with their callers rather than carried forward.
- State before controls: show health, current workload, capacity, and
  live-versus-desired difference before asking for a decision.
- Preview and apply are the same decision. A preview binds the exact plan the
  apply executes, and a stale plan is rejected rather than re-interpreted.
- Consequential fleet-wide platform changes proceed one Spark at a time.
  Recipe profile changes stay topology-atomic.
- Exact cross-site image reproducibility is desirable but never silently
  assumed. When a local result differs from a publisher's tested result for the
  same recipe, Vonk shows the difference and withholds the claim.

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
