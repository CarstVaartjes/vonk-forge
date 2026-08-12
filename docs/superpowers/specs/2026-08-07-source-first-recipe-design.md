# Vonk Forge source-first recipe design

Date: 2026-08-07

Status: approved architecture recorded for user review before implementation
planning

## Purpose

Define the initial Vonk Forge recipe model without carrying compatibility debt
from unreleased internal drafts. A Vonk recipe must be able to express every
workload represented by WorkloadRun while making build inputs, artifacts,
topology, sizing, security, installation, and runtime behavior more explicit.

The central decision is:

> A Vonk recipe describes the workload and contains an immutable source bundle
> with everything needed to build its runtime container.

Vonk does not require workload publishers to operate a container registry.
One GPU node builds a recipe once, and every target GPU node imports and verifies the
same resulting OCI image.

This design covers workload recipes. The prebuilt service images used to start
the Vonk control plane on a NAS or other service host are a separate platform
distribution concern.

## Superseded draft decisions

Nothing has been released, so this is the only initial recipe model. It replaces
the unreleased image-first assumptions in the catalog and installation design,
current draft schemas, fixtures, and implementation:

- there is no internal recipe v1-to-v2 migration;
- `schema_version: 1` means the source-first contract defined here;
- a publisher-provided workload image is not required;
- the global service does not require, host, or validate a community image
  registry reference;
- global publication uploads recipe source, not a workload image;
- a WorkloadRun container reference can become a pinned base image in a generated
  Dockerfile instead of being the canonical runtime identity;
- WorkloadRun mods and setup material are translated into the source bundle or a
  confined in-container lifecycle action instead of being categorically
  discarded; and
- supported node arrangements are explicit deployment profiles, not one loose
  minimum/maximum envelope.

There is one recipe execution path. Authoring origin is provenance, not a
runtime mode.

The operational model is:

```text
portable recipe revision
  -> local cluster mapping
  -> installed content on mapped GPU nodes
  -> zero or more fenced runs over time
```

## Product boundary

WorkloadRun remains a useful source ecosystem and import format. Vonk is a
capability superset rather than a permissive syntax clone:

- every supported WorkloadRun behavior must have a Vonk representation;
- imported implicit behavior becomes explicit typed data;
- unknown or unsafe behavior is preserved and explained, never silently run;
- missing sizing or topology facts leave an editable draft rather than being
  guessed as zero; and
- the final Vonk recipe runs without WorkloadRun being installed or invoked.

## Authorities and stored objects

The model separates portable intent from local facts.

### Recipe authority

Local PostgreSQL is authoritative for local drafts and locally accepted
immutable recipe revisions. Recipes never require Git, a branch, or a pull
request before they can be built, installed, run, or published.

Global PostgreSQL is authoritative only for global identities, immutable public
revisions, publisher roles, moderation state, and attached evidence. Importing
a global revision creates a durable local snapshot.

### Source-bundle authority

A recipe references an immutable content-addressed source bundle. The bundle is
a normalized archive containing:

- exactly one selected Dockerfile;
- scripts, patches, configuration, templates, and lock files used by the build;
- files needed by confined runtime lifecycle actions; and
- a generated manifest of paths, modes, byte sizes, and hashes.

The local service stores bundles in its content-addressed artifact store and
records their identities in PostgreSQL. The global service stores published
bundles in object storage and records their identities in global PostgreSQL.
Neither database stores arbitrary bundle bytes in recipe JSON rows.

The bundle forbids absolute paths, parent traversal, special files, device
nodes, sockets, hard links outside the bundle, and unbounded archive expansion.
Canonical archive rules make the bundle digest independent of upload filename
and archive ordering.

### Derived build authority

A built workload image is not part of the portable recipe document. It is a
derived local build record containing at least:

- recipe content digest and source-bundle digest;
- builder node identity and platform inventory;
- build policy and resolved build-input identities;
- start and finish timestamps and result state;
- resulting OCI manifest and image digests;
- compressed, extracted, and local storage sizes;
- inspection, SBOM, scan, and reproducibility evidence when available; and
- bounded logs with secrets and credentials removed.

The local artifact store or NAS may retain the exact runtime-import archive as
a cache. It does not need to expose the registry protocol.

### Cluster-mapping authority

A cluster mapping is a local, mutable desired-state object that binds one exact
recipe revision and one deployment profile to the user's cluster. It records
the chosen physical GPU nodes, role and rank assignment, non-secret parameter
values, local secret references, artifact placement, endpoint allocation, and
capacity reservations or policy. PostgreSQL is authoritative for the mapping.

A mapping is not part of the recipe and is never uploaded merely because the
recipe is published. The same portable recipe can have different mappings on
different clusters, or multiple mappings on one cluster when capacity and
policy allow them.

Changing recipe revision, deployment profile, node membership, role or rank
assignment, executable parameter values, or artifact identity creates a new
resolved mapping generation. Observations such as current addresses and health
update mapping state without changing portable recipe meaning.

### Installation and run authority

Per-node image import, model files, and installed bytes are reusable local
inventory linked to mappings but not owned exclusively by one mapping.
Processes, leases, health, endpoint evidence, and observed resource use belong
to fenced run records. They never enter the portable recipe.

## Three authoring paths

All authoring paths create the same local draft:

```text
WorkloadRun import ---------+
Start from standard -----+--> local recipe draft --> validate --> build
Fully custom ------------+
```

### Import WorkloadRun

The user imports a local YAML document, URL, GPU node Arena export, or supported
WorkloadRun registry reference. Vonk retrieves the bounded source and associated
mods or tuning material, creates a source bundle, translates fields, and shows
an exhaustive import report before the draft becomes runnable.

### Start from a standard

Vonk installation seeds standard recipes and source bundles for maintained
runtimes such as vLLM, SGLang, llama.cpp, and TensorRT-LLM where supported.
Starting from a standard clones its complete recipe and bundle into a new local
draft. The clone has no live template dependency.

The UI may call this a template, but a template is only an authoring
convenience. It ultimately produces the same ordinary Dockerfile and bundle as
every other recipe.

### Fully custom

The user creates metadata, artifacts, Dockerfile, bundle files, parameters,
runtime contract, and deployment profiles directly. Custom recipes receive the
same validation, build isolation, admission checks, and publication rules as
seeded or imported recipes.

## Canonical recipe document

Recipes use canonical JSON in storage and APIs. YAML may be an import and
export presentation, but it is not a separate semantic format. Unknown fields
fail validation so typos cannot silently alter execution.

The initial `schema_version: 1` document has the following sections.

### Identity and metadata

- stable publisher namespace and recipe slug;
- title, description, tags, license, attribution, and documentation links;
- logical workload family and capabilities;
- authoring origin and complete provenance;
- fork lineage where applicable; and
- immutable recipe content digest outside the hashed document.

Catalog metadata is descriptive. It cannot weaken build, runtime, or admission
policy.

### Build

- source-bundle object identity, digest, and expected bytes;
- Dockerfile path within the bundle;
- optional Dockerfile target;
- required `linux/arm64` output platform;
- declared, non-secret build parameters with types and constraints;
- network requirement and declared external source hosts;
- build timeout and resource envelope; and
- required output properties, including runtime user and OCI entrypoint
  expectations.

There is no alternative template build type. Seeded standards simply provide
ready-made Dockerfiles.

Base images and direct downloaded build inputs must use immutable identities
where their ecosystems provide them. Mutable input discovered during import is
a visible resolution requirement. A completed draft cannot hide a floating
container tag, Git branch, or unverified downloaded binary.

Build secrets are not part of the initial workload build contract. Model-hub
credentials belong to the separate artifact installation path and are local
secret references.

### Parameters

Recipe authors declare user-tunable parameters with:

- stable name and description;
- string, integer, number, boolean, or enumerated type;
- default value;
- bounds, pattern, or allowed values;
- whether changing the value requires a rebuild, reinstall, or restart; and
- the exact typed destinations that consume it.

Parameters expand only into typed Docker build arguments, runtime argument
values, or non-secret environment values. Vonk does not perform recursive shell
text interpolation.

### Artifacts

Each model, tokenizer, quantization, adapter, kernel table, or auxiliary file
declares:

- stable artifact ID and kind;
- immutable repository or object identity and revision;
- selection rules for repositories containing multiple variants;
- expected download and installed bytes;
- content hashes or immutable upstream metadata;
- target mount and read/write policy;
- required node roles or all-node placement; and
- local credential reference class when access is restricted.

Model weights remain outside the runtime image and are mounted read-only unless
a recipe explicitly declares a bounded writable derived-artifact path.

### Runtime contract

- trusted Vonk runtime adapter identifier and adapter contract version;
- typed argv/entrypoint definition;
- typed environment values and local secret references;
- declared ports, protocol, model aliases, and health/readiness checks;
- container user, devices, mounts, capabilities, writable paths, and network
  policy;
- confined in-container pre-start or post-stop argv where required; and
- stop, timeout, and restart behavior.

Recipes cannot add host-side plugins or agent code. Runtime adapters are trusted
Vonk software. A generic OCI process adapter handles workloads that need no
special orchestration, while specialized adapters own vLLM, SGLang, Ray, MPI,
TensorRT-LLM, llama.cpp, DS4, and other topology semantics.

Lifecycle actions execute inside the same constrained workload container as a
non-root user. They reference bundle content baked into the image and cannot
become host shell operations.

### Deployment profiles

A recipe contains one or more explicit deployment profiles. Each profile
represents one tested or supported way to run the workload and declares:

- stable profile name and description;
- exact positive node count;
- process and role counts;
- runtime strategy such as single, tensor parallel, pipeline parallel, data
  parallel, Ray, MPI, or a typed combination;
- parallelism dimensions and adapter settings;
- artifact placement per role;
- the role that owns the routable endpoint;
- homogeneous-node and software parity requirements;
- fabric capabilities and required connectivity graph;
- per-role resource envelopes; and
- profile-specific parameter defaults or constraints.

There is no power-of-two constraint. Profiles for one, two, three, four, or any
other positive node count are valid when the runtime adapter and declared
topology support them. A three-node GLM 5.2 profile is a first-class case, not
an exception.

The recipe declares roles and connectivity, not physical GPU node identities.
During placement the controller selects actual nodes and assigns deterministic
ranks. The resulting assignment is persisted as the cluster mapping.

An author publishes multiple profiles when the same workload supports multiple
world sizes. Each profile owns its corresponding arguments, topology, and
resource envelope; Vonk never assumes that sizing scales linearly between
profiles.

### Resource envelopes

Values use integer bytes rather than human-formatted GB strings. Every declared
or observed value has a basis of `declared`, `derived`, or `measured`; measured
values link to evidence.

The build envelope includes:

- source and dependency download bytes;
- extracted base and intermediate-layer bytes;
- temporary build peak;
- final compressed and extracted image estimates;
- writable build cache allowance; and
- CPU, memory, disk, and time limits.

The installation envelope includes, per node role:

- image import bytes;
- artifact download, installed, staging, and verification bytes;
- reusable versus new content;
- writable runtime cache allowance;
- retained rollback bytes where applicable; and
- required free-space safety margin.

The runtime envelope includes, per node role:

- startup memory peak;
- steady-state memory;
- bounded context/KV/cache growth assumptions;
- memory reserved for the operating system and agent;
- CPU and accelerator requirements; and
- expected versus hard maximum values where the runtime can enforce them.

Memory declares whether it is unified, host, or accelerator memory. Vonk Forge GPU node
unified memory is counted once, not once as RAM and again as VRAM.

Drafts may contain missing resource values. Build, installation, run, and
publication gates explain the missing values. Installation and publication
require complete disk envelopes for the selected profiles; running requires a
complete runtime envelope. Unknown required capacity is never treated as zero.

### Validation, benchmarks, and evidence

- static validation suites required before build or publication;
- health and identity assertions required after start;
- optional functional prompts or workload-specific correctness probes;
- typed benchmark profiles and expected units;
- publisher-supplied test reports bound to recipe, bundle, build result,
  hardware, runtime, and deployment profile; and
- measured disk, memory, startup, throughput, latency, and quality observations.

Evidence is attached to an immutable recipe revision but remains a separate
record. A new observation does not rewrite the recipe. An author can create a
new revision when measured evidence justifies changing the conservative
resource envelope.

## WorkloadRun translation

Import accounts for every source element exactly once and produces an editable
draft plus a report. Report dispositions are:

- imported without semantic change;
- transformed into a typed Vonk field;
- incorporated into the generated source bundle;
- resolved from a mutable reference to an immutable identity;
- user input or measurement required;
- preserved as redundant or informational provenance; or
- unsupported and blocking, with the exact reason.

Core mappings include:

| WorkloadRun concept | Vonk representation |
| --- | --- |
| `model`, revision, GGUF selector | artifact identity and selection |
| `container` | digest-pinned `FROM` in a generated Dockerfile |
| `runtime` | trusted runtime adapter |
| `command` | typed runtime argv and parameters |
| `defaults` | validated parameters and profile defaults |
| `env` | typed environment or local secret reference |
| `min_nodes`, `max_nodes`, old mode flags | one or more explicit deployment profiles |
| `mods`, `pre_exec` | bundle files plus build steps or confined lifecycle argv |
| runtime-specific keys | typed adapter configuration |
| tuning data | artifacts or bundle content with typed adapter reference |
| benchmark block/profile | Vonk benchmark definition |
| descriptive metadata | catalog metadata and provenance |

When a WorkloadRun recipe supplies only a container image, the generated
Dockerfile uses its resolved ARM64 digest as a base. This remains dependent on
an upstream publisher-built binary, and the import report says so. It does not
require Vonk to run a workload registry.

Unknown fields are preserved in source evidence and block completion only when
their execution significance cannot be ruled out. The report never claims that
ignored data was imported.

## Build, install, and run flow

### Resolve and preview

The controller validates the recipe, resolves immutable external identities,
inspects the bundle, computes a build-capacity preview, and explains executable
Dockerfile and lifecycle content. Import success alone never authorizes a
build.

### Build once

The planner chooses one compatible GPU node with enough temporary disk and memory
as the builder. The agent builds through a rootless isolated builder with:

- no Docker socket or root-equivalent daemon access;
- no host mounts, GPU devices, tailnet, local secrets, or controller
  credentials;
- no privileged mode or additional Linux capabilities;
- bounded CPU, memory, disk, process count, output size, and duration;
- network access limited to public destinations required by declared build
  inputs, with private, loopback, link-local, metadata, LAN, and controller
  destinations blocked; and
- sanitized bounded logs.

The build produces an OCI image and immutable result digest. It exports that
image in the selected host runtime's import format; DGX Spark uses a
Docker-loadable archive. Static inspection verifies architecture, configured
user, entrypoint, layers, labels, and policy. The build result is not yet an
installation or runtime authorization.

### Distribute the exact image

For a multi-node deployment, Vonk never rebuilds independently on each GPU node.
It transfers the exact runtime-import archive from the local cache or builder
to every target node through the existing authenticated artifact operation,
then verifies the image identity after import. All ranks therefore run
identical image bytes even if the original Dockerfile was not perfectly
reproducible.

The controller may retain the archive on the service host or builder according
to local cache policy. This is an implementation detail, not a public registry
requirement.

### Install artifacts

The controller selects a deployment profile, proposes compatible nodes, and
materializes a cluster mapping. It checks per-node disk including staging and
safety margin, reserves capacity, downloads immutable artifacts directly to the
mapped nodes, and verifies their identities. Cleanup is explicit and never
deletes unrelated data to make a plan fit.

### Admit and run

Immediately before start, the controller rechecks live inventory, current and
pending reservations, memory, node parity, fabric links, exact image and
artifact identities, and the resolved cluster-mapping generation. It starts the
mapped workload as one fenced group, waits for every required readiness result,
and publishes only the mapped entrypoint to LiteLLM.

A three-node profile therefore results in three selected compatible GPU nodes,
one persisted cluster mapping with a deterministic rank map, validation of
every required fabric edge, and one group readiness barrier. A partial group is
never routed.

## Reproducibility and evidence

The source-bundle digest proves which files were used; the build-input record
proves which external identities and policy were resolved; the image digest
proves which bytes a particular cluster ran.

Exact cross-site image reproducibility is desirable but not silently assumed.
If a local result differs from a publisher's tested result for the same recipe,
Vonk displays the difference and withholds any claim that the publisher tested
those exact bytes. Official Vonk recipes should progressively require
reproducible output or an explicitly documented reason why only input-level
reproducibility is available.

Within one installation or multi-node run, exact image equality is mandatory.

## Local interface behavior

The recipe editor starts with three equal choices: import WorkloadRun, start from a
standard, and fully custom. All enter the same editor after creation.

The editor groups fields into overview, source, artifacts, parameters, runtime,
deployment profiles, resources, validation, and provenance. It provides:

- a browsable Dockerfile and source-bundle file tree;
- a field-by-field WorkloadRun import report;
- profile cards for arbitrary node counts, including three-node layouts;
- declared, derived, and measured sizing with evidence links;
- build, install, and run previews as separate actions;
- exact per-node disk and memory deficits;
- explicit display of executable build and lifecycle content;
- build logs and resulting image identity;
- test results bound to a selected deployment profile; and
- publication readiness with every blocker explained.

Cluster views show, per GPU node, installed images and artifacts with sizes,
running and pending workloads with reservations and observations, and linked
multi-node roles. Prospective placement overlays show which profiles fit now
without mutating the cluster.

Recipe pages therefore offer **Map to cluster**, not a combined install/run
mutation. The mapping preview shows selected profile, exact GPU nodes, roles,
ranks, required transfers, reusable content, disk and memory headroom, fabric
edges, and blockers. Once accepted, installation makes that mapping available
to run; each later start creates a new fenced run without redefining the
mapping.

## Global publication and download

Publication is source-first:

```text
author/import locally
  -> complete recipe and source bundle
  -> build and test locally
  -> attach optional test evidence
  -> authenticate to vonkforge.ai
  -> upload private draft plus bundle
  -> validate
  -> explicitly publish immutable revision
```

The publisher is responsible for testing locally. The global service does not
build community submissions and does not accept arbitrary publisher registry
credentials. Automated global validation checks schema, canonical digest,
bundle safety, Dockerfile policy, immutable references, licenses and
attribution, artifact metadata, topology consistency, complete publication
resource envelopes, and evidence binding.

Public download returns the immutable recipe and source bundle. The local
controller imports both into its own authoritative catalog, repeats local
policy checks, and builds on a selected GPU node. Global availability is not
required after download.

Official status is a publisher role and evidence level, not a bypass around
local build isolation or admission.

## Failure behavior

- An incomplete import remains an editable draft with exact blockers.
- A global or source outage does not affect already downloaded recipes or
  locally retained build results.
- A build-capacity deficit names the builder and exact disk or memory deficit.
- A build timeout, output overflow, policy violation, or digest mismatch fails
  without installing or starting the result.
- A builder failure can resume from safe cached content or restart on another
  eligible GPU node; completed images are accepted only by verified digest.
- An image transfer or import mismatch blocks the complete target group.
- Missing disk, unknown runtime memory, unsupported fabric, or an unhealthy
  required node prevents mutation or route publication.
- A failed rank withdraws or withholds the group route and triggers fenced
  compensation.
- Publication validation failure preserves the private draft and identifies
  exact fields or bundle paths.
- No failure path opens SSH, executes a host shell fragment, or performs
  destructive automatic cleanup.

## Verification

### Contract and authoring

- Canonical recipe and source-bundle digests are stable in both repositories.
- Every seeded standard materializes as an ordinary recipe and Dockerfile.
- Standard, custom, and WorkloadRun origins produce the same executable contract.
- Drafts allow incomplete metadata, while build/install/run/publication gates
  reject the corresponding missing requirements.
- Unknown recipe fields and unsafe bundle paths fail closed.

### WorkloadRun coverage

- Golden fixtures cover every current core field, deprecated topology fields,
  arbitrary defaults, environment references, runtime configuration, mods,
  tuning, benchmarks, official, experimental, community, and external
  registries.
- vLLM, vLLM distributed and Ray, SGLang, llama.cpp, TensorRT-LLM, Atlas where
  representable, and imported generic OCI workloads receive explicit outcomes.
- Each input field has one report disposition and no executable source is
  silently ignored.
- Container tags and model branches become visible resolution requirements.

### Build boundary

- Builder isolation tests prove that builds cannot reach host files, Docker
  sockets, devices, credentials, private networks, controller endpoints, or
  metadata services.
- CPU, memory, process, duration, disk, archive, log, and output limits fail
  deterministically.
- Successful builds record exact inputs and OCI output digest.
- A malicious Dockerfile can fail the build but cannot create a host operation.

### Capacity and arbitrary topology

- Admission covers one-, two-, three-, four-, and larger node profiles without
  power-of-two assumptions.
- Three-node tests cover deterministic ranks, required fabric edges, per-role
  artifact placement, persisted cluster mapping, per-node capacity, group
  readiness, and compensation.
- Unified memory is counted once.
- Build peak, image, staging, artifact, cache, rollback, and safety-margin disk
  are all represented and checked at the correct action boundary.
- Existing and pending workload reservations participate in runtime admission.

### Distribution and lifecycle

- Multi-node installation imports one exact built image digest everywhere.
- Independent per-node builds cannot enter one deployment.
- One portable recipe can produce multiple independent local cluster mappings,
  and one mapping can produce multiple fenced runs without changing rank or
  artifact intent.
- Mapping-generation changes invalidate stale install or run previews.
- Start remains unrouted until every required rank passes identity and health
  checks.
- Stop or rank failure withdraws the LiteLLM route before cleanup.
- Rebuild, reinstall, cache reuse, interrupted transfer, rollback, and explicit
  removal preserve authoritative state and audit evidence.

### Cross-repository publication

- Local upload and global download validate the same recipe and bundle fixtures.
- OAuth and publisher authorization cannot alter recipe meaning.
- Publication stores source and evidence without requiring a workload registry.
- Downloaded revisions remain buildable and visible during a later global
  outage when their declared public upstream build and model inputs remain
  available or cached.

## Initial non-goals

- A Vonk-hosted public workload container registry.
- A global build farm for community submissions.
- Building independently on every target GPU node.
- Host-side recipe scripts or publisher-supplied agent plugins.
- Hidden inference of missing resource values as zero.
- Automatic interpolation between deployment profile node counts.
- Internal compatibility with unreleased image-first recipe drafts.
- Private global source bundles and private build registries in the first
  release.

## Implementation boundaries

The design should be implemented in contract-led increments:

1. replace the unreleased recipe schema and shared fixtures with the canonical
   source-first v1 contract;
2. add local/global content-addressed source-bundle storage and validation;
3. seed standard source recipes and implement the unified editor paths;
4. translate the complete supported WorkloadRun surface into drafts and bundles;
5. implement isolated build planning and typed GPU node agent build operations;
6. store, distribute, verify, and inventory derived OCI results;
7. connect explicit deployment profiles to disk, memory, topology, install, and
   run admission; and
8. change global publication, download, evidence, and end-to-end contracts from
   image-first to source-first.

Each increment requires tests at both repository boundaries. No migration code
for the unreleased draft schema should be introduced.
