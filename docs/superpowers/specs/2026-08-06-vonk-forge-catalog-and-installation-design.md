# Vonk Forge catalog, installation, and WorkloadRun interoperability design

Date: 2026-08-06

Status: architecture approved; implementation requires separate plans for the
local product and global service

> **Recipe-model update (2026-08-07):** The source-first recipe specification
> in [2026-08-07-source-first-recipe-design.md](2026-08-07-source-first-recipe-design.md)
> supersedes this document's unreleased image-first recipe, payload,
> container-publication, WorkloadRun execution, sizing, topology-profile, global
> publication, and related verification assumptions. Nothing has been released,
> so the source-first contract is the sole initial `schema_version: 1`; no
> internal recipe migration or compatibility path is required. The remaining
> product, authority, agent, service-host, Tailscale, routing, and repository
> boundaries in this document continue to apply.

## Purpose

Turn Vonk Forge into Vonk Forge: a local-first application for installing,
placing, running, and operating containerized AI workloads on one or more Vonk Forge
GPU nodes, with a separate global catalog at `vonkforge.ai`.

The design has four related goals:

1. import WorkloadRun recipes without executing untrusted recipe instructions;
2. materialize supported recipes as verified, resource-aware installations;
3. let users author and test their own recipes locally, then optionally publish
   immutable revisions to the global catalog; and
4. make the complete private cluster usable from anywhere through a
   Tailscale-only service-host deployment.

This document records product and architecture decisions. It does not authorize
implementation, production publication, DNS mutation, Railway deployment, or
changes to physical GPU nodes.

## Product position

WorkloadRun is a capable CLI launcher and GPU node Arena is a benchmark and recipe
website. WorkloadRun already provides container orchestration, model distribution,
multi-node launch, runtime plugins, VRAM estimation, live monitoring, Git recipe
registries, and benchmark submission. Vonk Forge must not market those
capabilities as inventions.

The distinction is lifecycle and authority:

- WorkloadRun primarily answers: "How do I launch this recipe?"
- Vonk Forge answers: "What is installed, what fits now, where can it run, what
  exact revision is active, why is a transition blocked, and how can I publish
  the locally verified result?"

Vonk Forge is deliberately heavier than a CLI. It provides a persistent local
database, browser control plane, authenticated node agents, installation state,
capacity admission, immutable deployment revisions, and a public catalog. A
single-machine user who only wants a quick command may reasonably prefer
WorkloadRun. Importing WorkloadRun recipes complements that ecosystem instead of
forking it.

## Product and repository shape

Two source repositories have separate release and trust boundaries.

### `vonk-forge`

The installable local product contains:

- the PostgreSQL-backed controller API and durable worker;
- the local React administration interface;
- Caddy, LiteLLM, PostgreSQL, monitoring, backup, and Tailscale Compose
  services;
- the WorkloadRun importer and native recipe compiler;
- installation, placement, admission, reconciliation, and audit logic;
- the Vonk Forge GPU node agent, restricted privileged helper, and agent packaging; and
- versioned compatibility clients for the global catalog API.

The normal service-host deployment is one Docker Compose project on a NAS,
computer, server, or VM. Model-serving containers and model weights remain on
the GPU nodes.

### `vonk-forge-web`

This is a separate private source repository for the public catalog and
publishing service. It uses a FastAPI and React/Vite monorepo matching the local
product's established stack:

```text
vonk-forge-web/
├── api/          # FastAPI, SQLAlchemy, OAuth, catalog API
├── web/          # public React/Vite catalog and publisher dashboard
├── worker/       # asynchronous validation and registry inspection
├── migrations/   # Alembic migrations
├── schemas/      # published versioned contracts
└── deploy/       # Railway service configuration
```

Its production Railway project initially contains separate web, API, worker,
and PostgreSQL services. PostgreSQL-backed durable jobs avoid a Redis
dependency. Staging and production are separate projects or environments, and
production requires explicit backups and tested recovery.

The source repository may remain private while the deployed website, public API
schema, and public catalog are available to everyone.

### Contract boundary

The repositories communicate only through a versioned HTTPS/OpenAPI contract.
They do not share an internal Python package and neither connects to the
other's PostgreSQL database. Compatibility is enforced with published schemas,
generated or validated clients, golden fixtures, and consumer contract tests.

The global service never receives direct access to a local controller, agent,
GPU node, model endpoint, local database, or tailnet.

## Naming and public endpoints

The product name is **Vonk Forge**, with **Vonk** as its short name and `vonk`
as the intended CLI name. The working tagline is "Many nodes. One forge."

The initial endpoint plan is:

- `vonkforge.ai`: public catalog and publishing website;
- `www.vonkforge.ai`: redirect to the apex;
- `api.vonkforge.ai`: versioned global API;
- `staging.vonkforge.ai` and `api.staging.vonkforge.ai`: non-production
  validation; and
- a separate status endpoint only when independent status hosting is useful.

The registered domain has registrar transfer lock, private registration, and
DNSSEC enabled. These facts are operational prerequisites, not application
trust signals.

## Authority and storage boundaries

### Local authority

Local PostgreSQL is authoritative for:

- locally authored recipe records and drafts;
- imported WorkloadRun recipes and their complete import reports;
- global recipe revisions accepted into the local catalog;
- seeded and user-created workload families;
- node enrollment and current authenticated inventory;
- artifact and container installation state per node;
- installation plans, reservations, placements, and runs;
- observed resource use, health, logs, jobs, and audit history; and
- global synchronization identifiers and publication state.

A recipe does not require a Git commit, branch, pull request, or remote
repository before it can be installed or run. The browser interface and CLI
use the same local API and database transactions.

This decision supersedes the Git-as-authoring-authority statements in the older
workload-release designs for family records and promoted recipe revisions. Git
remains appropriate for application source,
migrations, seed fixtures, schemas, documentation, platform release inputs,
and optional exported backups. Existing fleet and platform-policy Git contracts
may be migrated separately, but they cannot become a hidden recipe execution
gate.

### Global authority

Global PostgreSQL is authoritative for published immutable recipe revisions,
publisher identity and roles, public metadata, global validation results, test
reports, forks, deprecation, and moderation history.

Downloading a revision creates a durable local snapshot. Later global edits,
deprecation, unlisting, revocation, or outages never silently mutate or delete
that snapshot. A revoked revision is prominently marked and can be blocked for
new installations by local policy, but Vonk does not automatically terminate an
existing workload.

### Payload authority

Neither database stores model weights or container layers. Recipes refer to:

- public OCI images by immutable digest for the initial global release;
- Hugging Face or other model repositories by immutable revision and expected
  content metadata; and
- approved OCI or object-storage locations for other artifacts.

GPU nodes fetch large payloads directly from their declared source or an approved
mirror into their local content-addressed stores. The NAS and Railway service
are not payload proxies or inference hot paths.

## Domain model

The public term is **recipe**. The generalized model is:

```text
Workload family
  -> Artifact variant
  -> Runtime profile
  -> Topology / operating profile
  -> Materialized deployment revision
```

- A workload family identifies the logical model or application.
- An artifact variant identifies exact weights and quantization, including
  immutable upstream identity and content sizing.
- A runtime profile selects the OCI runtime implementation and typed runtime
  configuration.
- A topology or operating profile declares supported node counts, placement,
  fabric, context, concurrency, and resource behavior.
- A materialized deployment revision combines exact immutable inputs with
  local node assignment, routing, non-secret configuration, and reservations.

Workload families are local Library records, not TOML files. Installation uses
retained Library entities and recipe revisions; there is no package/deployment
compatibility table or seeded package-family API in the clean-slate control
plane.

## Recipe contract

A recipe is an immutable declarative workload contract, not a script. It
contains at least:

- schema version, stable identity, title, description, tags, and attribution;
- publisher or local author and provenance;
- workload family and artifact variant;
- exact container digest and supported architecture;
- exact model revision, expected logical and installed sizes, and required
  auxiliary artifacts;
- typed runtime family, options, entrypoint contract, exposed API, and health
  checks;
- minimum, maximum, and explicitly tested node counts;
- topology, rank, fabric, and node-parity requirements;
- required mounts, devices, Linux capabilities, network policy, and writable
  paths;
- declared and observed disk, memory, staging, cache, and runtime envelopes;
- configuration parameters and secret references, never secret values;
- validation suites, test evidence, and compatibility declarations; and
- import source, fork lineage, revision, and content hash.

Mutable local facts do not belong in the recipe. Installed bytes, actual node
placement, current process state, measured memory, active leases, and last
health result are separate local records linked to the immutable revision.

Published revisions are never edited. Changing any executable input,
artifact identity, resource contract, topology, privilege, or health contract
creates a new revision and content hash.

## Container execution model

OCI containers are the default runtime boundary. Model weights remain outside
the image in verified GPU node-local stores and are mounted read-only. Writable
caches, generated output, logs, and runtime state receive explicit bounded
paths.

For community publication, the publisher must build and push the image before
publication. The global service validates metadata and registry availability;
it does not:

- build submitted Dockerfiles;
- run publisher scripts;
- host community images;
- repair incompatible images; or
- manufacture an image for an imported WorkloadRun recipe.

The initial global catalog accepts only publicly retrievable OCI images.
Private registries and per-site credentials are deferred.

Containers reduce host mutation but do not make arbitrary code safe. Local
policy still rejects privileged containers, arbitrary host mounts, undeclared
devices, host networking, root requirements without an approved capability,
and unsupported Linux capabilities. Runtime images carry publisher and
validation trust labels. The same validation rules apply to official and
community entries; official status is derived from an authorized Vonk
publisher role, not a bypass.

The native compiler initially targets vLLM, SGLang, and llama.cpp. Specialized
native runtimes such as DS4 remain distinct declared capabilities rather than
being forced into a generic vLLM profile.

## WorkloadRun import

Import is translation into the Vonk schema, never execution of the source
recipe. It accepts a local file or supported WorkloadRun registry/export reference
and preserves the original source document and source identity for audit.

The importer parses model, revision, runtime, container, defaults, environment,
node limits, metadata, commands, mods, tuning, and benchmark references. Each
source element receives exactly one reported outcome:

- **Imported**: represented without semantic change;
- **Transformed**: deterministically represented by a typed Vonk field;
- **Resolution required**: needs an immutable digest, size, or upstream
  identity before installation;
- **Overlay required**: needs local topology, resource, privilege, or policy
  information;
- **Unsupported—blocking**: cannot be represented safely and prevents a
  runnable revision;
- **Dropped redundant**: intentionally omitted because the container or Vonk
  runtime contract already owns the behavior.

The report explains the original field, destination or omission, reason,
severity, and exact action needed. No information is silently ignored.

Recognized vLLM, SGLang, and llama.cpp commands are parsed into typed runtime
arguments. Raw command strings, shell fragments, installers, and shared mods
are retained only as non-executable import evidence. If their semantics cannot
be represented, the imported record remains an editable draft and cannot be
installed.

An image tag must resolve to an immutable digest and support Vonk Forge GPU node ARM64.
When no compatible publisher-built image exists, import may still succeed as a
draft but publication and execution remain blocked. Vonk never promises that
every syntactically valid WorkloadRun profile is automatically runnable.

Broad WorkloadRun fixtures must cover official, experimental, community, and
external registry forms rather than optimizing for only two demonstration
recipes. Mia dual-node and DS4 single-node examples remain important authored
acceptance profiles because they exercise topology, native capability,
artifacts, and resource behavior that basic vLLM recipes do not.

## Local lifecycle

The principal state flow is:

```text
local draft | WorkloadRun import | global download
  -> resolved
  -> install planned
  -> installed on exact nodes
  -> available to run on that placement
  -> starting as a fenced group
  -> running
  -> stopped or failed with retained evidence
```

Creating, importing, resolving, installing, running, and publishing are
separate actions. Import success never implies compatibility, installation,
or execution approval.

### Installation admission

Before downloading, the planner computes per-node and aggregate requirements:

- container download and extracted bytes;
- model logical, physical, shared, and staging bytes;
- temporary peak while retaining active and rollback generations;
- current free, reserved, reclaimable, and policy-protected disk; and
- download credentials, license acceptance, architecture, and source policy.

The plan identifies cache reuse and exact deficits. Installation reserves its
peak requirement before transfer. It never deletes images, model data, caches,
or user files merely to make a plan fit.

### Runtime admission

Before start, the planner evaluates declared and observed per-node memory,
workspace, KV/cache growth, context, concurrency, CPU, devices, current
reservations, pending starts, and other running workloads. Unknown or
unbounded required values block admission instead of being treated as zero.

The action boundary repeats the relevant inventory and reservation checks so a
stale browser preview cannot overcommit the cluster.

### Multi-node admission

A gang deployment additionally requires:

- an explicitly supported world size;
- exact matching recipe, image, artifact, and configuration identities;
- authenticated healthy agents on every assigned node;
- accepted management and fabric topology;
- rank and peer assignments created by the controller, not recipe text;
- capacity on every member; and
- one group fence and readiness barrier.

Failure before publication stops or compensates the complete group. A partial
deployment is never routed as healthy.

## Local web experience

The local browser application is the operating console. It provides:

- catalog search across local, imported, and global entries;
- recipe comparison by runtime, quantization, disk, memory, node count,
  topology, trust, and validation;
- structured authoring and import-resolution workflows;
- exact install and run previews with explainable blockers;
- cluster cards for every GPU node showing memory, disk, thermal, power, fabric,
  agent, and platform status;
- installed artifacts and images per node with logical, physical, shared, and
  reclaimable sizes;
- running and pending workloads with reserved and observed memory;
- multi-node workloads displayed once with linked ranks and nodes;
- prospective-capacity overlays before installation or start; and
- jobs, bounded logs, validation evidence, audit, rollback, and removal
  previews.

The UI, CLI, and automation clients call the same API. The UI contains no
independent placement, privilege, or reconciliation logic.

Useful GPU dashboard ideas include compact node cards and live resource visibility,
but Vonk retains authenticated outbound agents, typed operations, durable state,
resource admission, and multi-node workload identity rather than copying an
SSH-oriented or privileged monitoring architecture.

## Global catalog and publication

The global data model uses relational identity and authorization with canonical
JSON recipe revisions:

- `users` and `oauth_accounts`;
- `publishers` and `publisher_memberships`;
- `recipes` as stable public identities;
- `recipe_drafts` as mutable private work;
- `recipe_revisions` as immutable canonical documents with schema version and
  content hash;
- `validation_results` and `test_reports`;
- `recipe_forks`; and
- `moderation_events` for auditable deprecation, unlisting, and revocation.

Frequently queried runtime, model family, quantization, memory, disk, node
count, topology, and trust fields are indexed relationally or through JSONB.

Users authenticate with standard OAuth providers. Publisher namespaces and
memberships have explicit roles. Anyone authorized for a namespace can upload
their own draft and publish after automated validation. Official status is
derived from the Vonk organization publisher role. Community publication does
not enter a manual quarantine queue.

Forking copies a revision into the user's namespace and retains source
attribution. Editing any published entry creates a new immutable revision.

### Local-first publishing flow

```text
create or import locally
  -> build/use a local image
  -> install and run locally
  -> optionally capture observed sizing and test evidence
  -> push the image to a public registry
  -> resolve the immutable image digest
  -> authenticate to vonkforge.ai
  -> upload a private global draft
  -> explicitly publish an immutable public revision
```

Upload and publish are separate explicit actions. Upload sends metadata,
artifact references, the image digest, topology and resource declarations, and
optional test reports. It does not upload the image or weights.

The global website may also create a private draft. A user pulls that draft
into their local Vonk instance for installation and testing before returning an
updated draft or publishing.

"Self-tested" records publisher-supplied local evidence. A stronger
"Vonk-qualified" status requires the official validation suite on real
supported Vonk Forge GPU node topology. These labels are transparent evidence levels,
not claims that community containers are harmless.

## Global API and synchronization

The public `/v1` API provides cursor-based catalog search, recipe identity,
immutable revision retrieval, schemas, and compatibility metadata. Authenticated
resources provide the current user, publisher membership, drafts, validation,
upload, publish, fork, deprecate, and sync state.

The local UI opens the global website for OAuth authorization-code flow with
PKCE. A one-time callback grants a short-lived, narrowly scoped local token.
Long-lived provider credentials never enter the local recipe database.

Uploads are idempotent by local recipe UUID and canonical content hash.
Publication transactionally verifies ownership, current schema, public image
digest, architecture manifest, artifact references, required metadata, and
completed validation before creating the immutable revision.

Immutable responses receive long cache lifetimes. Mutable catalog views use
ETags and cursors. Synchronization imports exact revisions; it never overwrites
local installations, placement, observations, or run state.

Errors use a stable machine-readable problem format. Conflicts distinguish
stale drafts, duplicate slugs, ownership, and immutable-revision violations.
Validation failures identify exact fields. External registry or model-hub
outages leave a retryable validation pending rather than corrupting or silently
rejecting the draft.

## GPU node agent installation and node preparation

Host preparation and service installation are different products.

### Readiness and hardening guidance

`vonkforge.ai` publishes a versioned Vonk Forge GPU node readiness and hardening runbook
with a human checklist, explained commands, rollback steps, and an optional
prompt for use with an LLM assistant. LLM output is guidance, not acceptance
evidence.

A read-only `vonk diagnose` or equivalent tool produces structured pass,
warning, and fail results for DGX OS, architecture, NVIDIA container support,
Docker configuration, storage, memory, networking, time, certificates, and
optional RDMA prerequisites. Remediation is explicit and never smuggled into a
recipe or silently applied by diagnostics.

The agent installer hardens only its own boundary. General SSH policy, firmware,
host firewall policy, fabric configuration, and operating-system upgrades
remain separately reviewed host changes.

### Signed APT package

The persistent service is installed as a signed ARM64 Debian package named
`vonk-agent`, not as a disposable `uvx` environment. Before an APT repository
exists, the supported path is:

```text
sudo apt install ./vonk-agent_<version>_arm64.deb
sudo vonk-agent pair https://controller-name
```

The eventual signed repository at a dedicated package endpoint supports normal
`apt update`, `apt install vonk-agent`, and operator-controlled upgrades.
Package provenance, signature, digest, SBOM, and supported DGX OS range are
published with every release.

The production agent, stable supervisor, and restricted privileged helper are
Rust binaries. Rust gives the long-lived node trust boundary one small,
self-contained ARM64 artifact without a Python interpreter or mutable virtual
environment. First-party agent crates forbid unsafe code where their required
system interfaces permit it; dependency policy, protocol validation, systemd
sandboxing, signatures, and recovery tests remain necessary because language
memory safety does not prove authorization or lifecycle correctness.

The `.deb` installs the pinned binaries below `/opt/vonk-agent` and resolves no
dependencies at service start. It also installs:

- an unprivileged service identity;
- systemd service and stable supervisor definitions;
- protected configuration, state, certificate, and log paths;
- the restricted privileged helper and policy;
- bounded log rotation and resource limits; and
- explicit repair, update, rollback, and removal behavior.

`uvx` remains suitable for a one-off readiness diagnostic or development tool,
but not as the permanent service lifecycle. The existing Python agent remains a
behavioral oracle during the contract-driven Rust transition and is removed
from the production package only after protocol, failure, update, and physical
GPU node parity pass.

### Pairing and agent trust

Pairing generates the private key on the GPU node and presents a short code and
fingerprint for approval in the local UI. Approval binds a short-lived mTLS
certificate to an immutable node ID. Enrollment secrets do not appear in shell
history, and private keys never leave the node.

The existing outbound-agent transport remains authoritative: the GPU node initiates
bounded HTTPS long polling to the controller and exposes no routine inbound
management service. Every mutation is a typed, fenced operation. The protocol
has no arbitrary shell, command, script, environment, filesystem path, or
repository-provided privileged operation.

This replaces SSH as the normal initial installer path. SSH remains an explicit
break-glass option for recovery when package installation, agent repair, or
certificate recovery cannot be performed locally. It is never an availability
fallback for routine operations.

## Service-host Compose deployment

The NAS or computer runs the complete private control and access plane as one
Compose project:

- PostgreSQL;
- control API and durable worker;
- local React web interface;
- Caddy;
- LiteLLM as the OpenAI-compatible inference gateway;
- Tailscale gateway;
- Prometheus and Grafana where enabled by the product baseline;
- backup and migration jobs; and
- optional development tooling that retains its existing isolation boundary.

The normal installation result is one documented `docker compose up -d`. The
stack performs safe database migrations, health checks, and first-run setup
without creating recipe or cluster dependencies on `vonkforge.ai`.

## Tailscale-first remote access

Tailscale is the only supported remote-access path for the initial product, not
an optional profile. The existing containerized Tailscale gateway design is
retained:

```text
tailnet client
  -> named Tailscale HTTPS Service
  -> Compose Tailscale gateway
  -> Caddy
  -> local UI | control API | LiteLLM | approved dashboards
```

The GPU nodes do not join Tailscale. They use restricted LAN egress to the
controller for enrollment and outbound mTLS agent traffic, while LiteLLM uses
validated GPU node model endpoints on the management network. Tensor and fabric
traffic remains GPU node-to-GPU node.

No user, administrator, inference, dashboard, SSH, or database service is
published directly on the public internet or ordinary LAN. There is no
automatic public HTTPS, port-forward, Cloudflare Tunnel, or unauthenticated LAN
fallback. Other ingress mechanisms require a later design review.

Tailscale identity controls network reachability but does not replace
application authorization. The local UI still has roles and sessions;
inference clients still use separately managed API credentials; and
controller-agent traffic still uses mTLS.

The service-host gives clients one stable OpenAI-compatible URL and stable
model aliases. Switching a healthy deployment changes an audited LiteLLM route
rather than client configuration. A service-host outage removes shared UI and
inference ingress but does not delete GPU node-local artifacts or pretend running
workloads stopped.

### Inference route publication

Caddy is a static ingress and security boundary, not model-routing authority.
Its tailnet listener sends `/v1/*` to LiteLLM and sends administration paths to
the local controller. Model services do not register with or call Caddy.

The controller combines the immutable recipe and materialized placement in
PostgreSQL with authenticated, fresh agent endpoint and health evidence. It
validates the entrypoint node, management CIDR, recipe-declared port and scheme,
model identity, deployment revision, topology readiness, and route policy. Only
then does it atomically publish a LiteLLM route generation mapping stable model
aliases to approved upstream URLs.

LiteLLM reads only this controller-published route generation. It authenticates
inference clients, applies quotas and replica routing, and connects directly
over the restricted management LAN to the approved GPU node entrypoint. It does
not discover containers, select placement, start workloads, or accept dynamic
model authority from its administration UI. A multi-node gang publishes only
its entrypoint endpoint; worker ranks communicate over the GPU node fabric and are
not LiteLLM upstreams.

The resulting request path is:

```text
tailnet client -> Tailscale -> Caddy -> LiteLLM -> GPU node entrypoint model API
```

The independent control paths are:

```text
GPU node agent -> outbound mTLS Caddy backend -> controller
controller -> atomic validated route generation -> LiteLLM
```

Stale health, a failed rank, stop, replacement, or policy failure withdraws the
LiteLLM route before workload cleanup. Caddy never invents a fallback endpoint.

## Security and trust boundaries

- Recipe documents are untrusted data and never executable scripts.
- Community publishers supply their own public container images.
- Container trust is explicit; container isolation is not treated as proof of
  benign behavior.
- Global validation never grants access to a local cluster.
- Local policy and current inventory make the final install and run decision.
- Agent operations are typed, fenced, certificate-bound, and idempotent.
- Node identity is certificate-bound and independent of hostname or address.
- Secrets are references delivered through protected local boundaries, never
  recipe values, URLs, arguments, audit payloads, or logs.
- Multi-node routes remain withdrawn until all ranks pass readiness.
- Remote users enter only through Tailscale and Caddy.
- Large payloads move directly between approved upstreams and GPU node-local
  stores.

## Failure behavior

- A global outage prevents new global search or publication but does not
  prevent local authoring or operation of materialized recipes.
- A registry outage pauses image validation or download and preserves retryable
  state.
- An artifact-source outage pauses installation without changing the active
  generation.
- Insufficient disk or memory produces an exact per-node deficit and no
  destructive cleanup.
- An offline or incompatible agent blocks only placements requiring that node.
- A failed rank prevents gang route publication and triggers group compensation.
- A revoked recipe revision is retained locally with policy-visible status.
- A Tailscale outage closes remote access and does not open another listener.
- A controller restart resumes durable jobs only after fence and operation-state
  verification.
- A failed agent update uses the established A/B rollback boundary.

## Delivery decomposition

The work is too large for one implementation plan or branch. It is divided
into contract-led programs.

### Program A: local catalog and authority migration

- Define the canonical recipe schema and database models.
- Make PostgreSQL authoritative for recipe, package-family, install, deployment,
  and publication-link state.
- Add idempotent standard-family seeding and migrations from existing checked-in
  workload definitions where applicable.
- Preserve exact export and backup formats without retaining a Git execution
  gate.

### Program B: GPU node agent distribution and pairing

- Port the production agent, stable supervisor, and privileged helper to
  first-party Rust crates against the existing Python protocol oracle.
- Produce signed ARM64 `.deb` artifacts with pinned Rust binaries and systemd
  integration.
- Implement local pairing, mTLS issuance, rotation, revocation, repair, and A/B
  updates.
- Publish readiness guidance and read-only diagnostics.
- Replace routine SSH installation with local APT installation while retaining
  explicit recovery tooling.

### Program C: import, resolution, and native execution

- Implement WorkloadRun parsing and exhaustive import reports.
- Add vLLM, SGLang, and llama.cpp compilers plus distinct native capability
  support.
- Resolve image and model identities, artifacts, resources, privileges, and
  topology into immutable local revisions.
- Exercise broad registry fixtures and Mia/DS4 acceptance profiles.

### Program D: installation, admission, and cluster UX

- Add artifact inventory and disk-aware installation planning.
- Add memory, co-residency, pending-operation, placement, and gang admission.
- Build the local catalog, recipe editor, install/run workflow, node cards,
  artifact views, workload views, and prospective capacity overlay.
- Integrate durable jobs, evidence, routes, and audit.

### Program E: global catalog service

- Create the private `vonk-forge-web` repository and Railway staging project.
- Implement OAuth, publishers, drafts, immutable revisions, forks, validation,
  moderation, public search, and the website.
- Publish OpenAPI and versioned recipe schemas.
- Keep image building, weight storage, and local cluster access out of scope.

### Program F: synchronization and publishing

- Generate or verify the local global-API client.
- Add download, update visibility, OAuth linking, idempotent draft upload, and
  explicit publish flows.
- Add self-tested and official qualification evidence.
- Run cross-repository contract and end-to-end tests.

Each program receives its own implementation plan, review boundary, migrations,
tests, and acceptance evidence. Contract work precedes concurrent repository
implementation.

## Verification and acceptance

Acceptance requires more than schema unit tests.

### Contract and import

- Canonical serialization and content hashes are stable across both
  repositories.
- Old supported schema revisions remain readable or receive explicit migration
  errors.
- Golden WorkloadRun fixtures cover every import outcome and supported registry
  class.
- Arbitrary commands, mods, unknown arguments, and missing digests cannot become
  executable by omission.
- Export followed by import preserves immutable meaning and attribution.

### Global service

- OAuth state and PKCE, publisher RBAC, namespace ownership, immutable
  revisions, idempotent uploads, forks, and moderation are tested.
- Registry and artifact inspection is bounded, HTTPS-only, redirect-aware,
  size-limited, and protected against SSRF.
- External outages result in retryable jobs and stable error codes.
- PostgreSQL migrations, backup, restore, and Railway-like deployment are
  exercised outside production.

### Local operations

- Fresh local install works without global availability.
- Disk plans account for downloads, extraction, staging, sharing, retention,
  and rollback peaks.
- Runtime admission accounts for observed inventory, reservations, running
  workloads, and pending starts.
- One-, two-, and larger-node fixtures prove no fixed node names or counts.
- Multi-node mismatches and rank failures cannot publish a partial route.
- UI and CLI create the same plan, job, audit, and result resources.

### Agent and packaging

- `.deb` install, upgrade, rollback, removal, reinstall, and unsupported-platform
  behavior are tested on Vonk Forge-compatible ARM64 Ubuntu environments.
- The production agent package contains no Python runtime, virtual environment,
  `pip`, or `uv` dependency.
- First-party Rust crates enforce the reviewed unsafe-code policy, locked
  dependencies, SBOM, provenance, and vulnerability/license gates.
- Pairing, certificate rotation, revocation, expiration, node replacement,
  disconnection, stale fences, and A/B recovery fail closed.
- No routine operation opens SSH or accepts arbitrary execution.

### Compose and remote access

- A clean Compose install, upgrade, backup, restore, and host replacement are
  tested.
- No human-facing host port is published outside the Tailscale gateway.
- Authorized tailnet users can reach only granted services.
- Ordinary LAN and public clients cannot reach UI, inference, administration,
  dashboards, development SSH, or databases.
- The GPU nodes remain free of Caddy, LiteLLM, PostgreSQL, Tailscale, and general
  monitoring services.

### Physical acceptance

At least one real single-GPU node recipe and one real multi-GPU node gang recipe must
complete import or authoring, resolution, installation, start, health,
inference, stop, memory recovery, and retained-evidence workflows. A local
recipe must also complete draft upload and publication against staging before
production publication is enabled.

## Explicit non-goals

- Building or hosting community containers.
- Copying model weights into Railway or the service-host database.
- Executing recipe-provided shell or privileged installation hooks.
- Requiring Git to author, install, run, or publish a recipe.
- Installing Tailscale on every GPU node.
- Public internet or unauthenticated LAN access to the private control plane.
- Kubernetes, Slurm, Docker Swarm, a message broker, or a service mesh for the
  initial product.
- Automatic execution of newly discovered or newly published revisions.
- Automatic cache deletion to satisfy disk admission.
- Claiming every WorkloadRun recipe is safely or automatically portable.
- Treating community, self-tested, or official labels as a substitute for local
  policy and admission.

## Success criteria

The design succeeds when a user can:

1. start the Vonk Compose application on a suitable NAS or computer;
2. prepare each GPU node using documented guidance and install one signed agent
   package;
3. approve node pairing and observe authenticated inventory without a subnet
   scan or routine SSH;
4. create a local recipe, import a WorkloadRun recipe, or download an immutable
   global revision;
5. understand every unsupported or transformed import element;
6. see exact per-node installation and runtime capacity before acting;
7. install and run only on compatible single- or multi-node placement;
8. use the stable AI gateway and administration interface through Tailscale
   from anywhere;
9. observe installed artifacts, active workloads, sizing, topology, health, and
   evidence per node; and
10. test a local recipe, upload it as a private draft, and explicitly publish an
    immutable public revision without giving the global service access to the
    local cluster or payloads.

## Related designs and references

This design preserves and amends the following local specifications:

- `2026-08-03-outbound-node-agent-design.md` for outbound long polling, mTLS,
  fencing, typed operations, and A/B updates;
- `2026-08-05-containerized-nas-access-and-devbox-design.md` for the Tailscale
  Compose gateway and LAN/tailnet split;
- `2026-08-05-generalized-workload-package-system-design.md` for immutable
  package content, resource envelopes, GPU node-local stores, topology, and
  lifecycle, while replacing Git recipe authority;
- `2026-08-03-scalable-node-platform-control-plane-design.md` for the control
  API, worker, Compose, LiteLLM, observations, and N-node planning, while
  removing the Git gate from recipe operations; and
- `2026-08-06-workload-validation-runner-design.md` for validation binding and
  lifecycle evidence.

External design inputs include:

- WorkloadRun documentation: <https://workload_run.dev/>;
- WorkloadRun recipe format: <https://workload_run.dev/recipes/format/>;
- WorkloadRun registries: <https://workload_run.dev/recipes/registries/>;
- GPU node Arena: <https://node-arena.com/>;
- Ubuntu package management:
  <https://documentation.ubuntu.com/server/how-to/software/package-management/>;
- Debian service policy:
  <https://www.debian.org/doc/debian-policy/ch-opersys.html>;
- Debian Python policy:
  <https://www.debian.org/doc/packaging-manuals/python-policy/>;
- uv tool environments: <https://docs.astral.sh/uv/concepts/tools/>; and
- Rust ownership and memory safety:
  <https://doc.rust-lang.org/book/ch04-00-understanding-ownership.html>.
