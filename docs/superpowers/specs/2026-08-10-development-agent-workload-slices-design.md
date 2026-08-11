# Development Agent and Workload Validation Design

**Date:** 2026-08-10
**Status:** Approved by delegated operator judgment

## Context

The public development control images now deploy successfully on the NAS from a
single mutable Compose artifact plus host-provided secret files. The two DGX
Spark nodes run the Rust `vonk-forge-agent` package from the development APT
channel, but the development control stack deliberately disables the agent
runtime and exposes only the human API on NAS loopback. That proves image and
database startup, not node enrollment or workload execution.

This change completes development validation in three vertical slices:

1. securely enroll both Sparks and show authenticated inventory;
2. run a tiny synthetic recipe through the complete source-first lifecycle; and
3. run a real model, including a two-node placement, routing, inference,
   recovery, and restart checks.

The development path remains distinct from production. Production continues to
use immutable selected releases, the host updater, step-ca, TUF, and release
signers. No development convenience may weaken those boundaries.

## Goals

- Keep the NAS installation contract to `docker-compose.yml` and `secrets/`.
- Keep the mutable `:dev` image channel for operator-triggered development
  redeploys.
- Expose enrollment and agent APIs only through a TLS reverse proxy on the
  management LAN; keep the human API loopback-only.
- Use separate enrollment and normal controller URLs on every agent.
- Use host files as the source of runtime secrets; secrets never enter Git,
  images, image labels, Compose environment values, or CI artifacts.
- Exercise the existing source-first recipe pipeline rather than adding a
  development-only fake execution path.
- Make every phase independently testable and leave an auditable runbook.
- Configure the two current Sparks by hostname through `/etc/hosts`, without
  depending on local DNS or mDNS.
- Make the implementation reproducible for another operator from a clean NAS
  and clean supported nodes. Site addresses, hostnames, CIDRs, node IDs, secret
  locations, and model selections are validated inputs rather than constants in
  product code.

## Non-goals

- Replacing the production release or host-update path.
- Publishing production images, packages, recipes, or TUF metadata locally.
- Running step-ca in the development NAS stack.
- Exposing PostgreSQL, the worker API, the control API, or LiteLLM directly to
  the LAN.
- Treating a mutable `:dev` deployment as an exact rollback mechanism. Pinned
  development artifacts remain the reproducibility and rollback tool.
- Introducing a workload registry merely for development validation.

## Security boundaries

### Network surface

The final development stack publishes two host ports:

- `127.0.0.1:8080` (configurable with the existing variable) for the human
  control API and UI. Operators access it locally or through an SSH tunnel.
- `0.0.0.0:8443` for Caddy's agent ingress. Caddy and the API both restrict
  accepted agent traffic to the configured management CIDRs. The NAS firewall
  runbook restricts TCP 8443 to the same CIDRs.

Caddy serves two names on the same port:

- `https://enroll.vonk-forge.lan:8443/`: permits only
  `POST /agent/v1/enroll`, uses normal server TLS, and does not require a client
  certificate because an unpaired node has none.
- `https://agents.vonk-forge.lan:8443/`: permits the authenticated
  `/agent/v1/*` surface except enrollment and requires a certificate issued by
  the development agent CA.

Caddy removes all inbound identity and proxy-auth headers, reconstructs the
verified client-certificate identity headers, and adds a random shared proxy
token. The API accepts agent requests only when that token and the certificate
metadata contract validate. Caddy has no access to the agent CA private key.

### Development PKI

Development uses two independent local authorities:

1. a controller server CA signs the Caddy certificate for
   `enroll.vonk-forge.lan`, `agents.vonk-forge.lan`, and the reserved
   `registry.vonk-forge.lan` name;
2. an Ed25519 agent CA signs short-lived Spark client certificates through the
   existing built-in control authority.

The controller server CA private key is retained in the protected local source
generation so the generator can validate it against the public controller CA
and operators can rotate the server certificate. `controller-ca-key` must not
be copied to the NAS. The NAS receives the server certificate/key, the public
controller CA, and the agent CA certificate/key. The agent CA key is projected
only into the API service. The worker, Caddy, LiteLLM, and Sparks cannot read it.
Sparks receive only the public controller CA and their locally generated
private identity.

The built-in authority is permitted only when all of the following are true:

- deployment mode is `development`;
- agent runtime is explicitly `enabled`;
- CA provider is explicitly `builtin`;
- every required file and proxy-auth setting is present.

Production retains its existing fail-closed requirement for step-ca. Mixed or
implicit settings fail startup.

### Runtime secrets

The NAS `secrets/` directory contains the existing files plus:

- `agent-ca-certificate`
- `agent-ca-key`
- `agent-proxy-auth`
- `controller-ca`
- `controller-server-certificate`
- `controller-server-key`
- `litellm-master-key`
- `litellm-upstream-key`
- `management-cidrs`
- `token-signing-key`

`management-cidrs` is integrity-sensitive configuration rather than a secret,
but it lives in this directory to preserve the two-item NAS project contract.
The protected local source contains 15 local source files: 14 protected
secret/config files plus the public `git-signing-key.pub`. The publisher
validates that complete generation and copies exactly 13 deployment files to
the NAS; `controller-ca-key` and `git-signing-key.pub` are local-only.
An existing 14-file local source from an earlier branch head is incomplete
because the missing CA private key cannot be reconstructed. It is replaced by
a coordinated, backed-up 15-file PKI generation rather than repaired in place.

LiteLLM effective configuration is intentionally database-free. The checked
route document contains a fixed database marker only as an input-schema guard;
the supervisor removes it before launching LiteLLM. No database URL is
projected to LiteLLM, and its Admin UI remains disabled.

The repository supplies an idempotent local preparation script. It creates
missing keys and certificates in a gitignored development-secret directory,
prints fingerprints and expiry dates but never secret values, refuses to
overwrite existing material, validates existing material before reuse, and
copies only explicitly selected files to the deployment destination. Operators
back up all 15 local files as one generation in 1Password. Key generation never
occurs on SMB storage or inside a published image.

Repository initialization and runtime authority projection are separate
one-shot services. Networked `dev-repository-init` receives no secrets and
mounts only the selected cohort plus repository volume. The separate
`network_mode: none` `dev-init` runs as root only to read Compose file secrets
and establish exact ownership. It does not mount the repository and creates
these least-privilege projections:

- API: database URL, Git signing key, admin grant key, worker token, agent CA
  certificate/key, proxy token, and token-signing key;
- worker: database URL and worker token;
- migration: database URL;
- LiteLLM: master key and upstream key only;
- Caddy: controller certificate/key, agent CA certificate, proxy token, and
  management CIDRs.

No projection is shared between service identities merely for convenience.

## Agent endpoint split

The Rust configuration gains two mandatory HTTPS origins:

- `enrollment_url`, set to `https://enroll.vonk-forge.lan:8443/` and used only
  by `vonk-forge-agent pair`;
- `controller_url`, set to `https://agents.vonk-forge.lan:8443/` and used after
identity issuance for claims, results, presence, recipe specs, source/image
transfer, complete recipe-run health snapshots, and all other authenticated
operations. While local recipe runs exist, the Rust agent reports their exact
run IDs and one bounded endpoint-readiness result at most ten seconds apart.
The controller projects only the authenticated node's assigned ranks; a
missing or unhealthy rank is failed without conflating workload health with
agent presence. Fresh healthy evidence permits automatic route republication.

Both must be canonical root URLs without userinfo, query, or fragment. They may
be equal for compatible deployments, but the documented development and
production configurations use different hostnames. The controller CA path and
SHA-256 pin protect both names. Pairing rejects a CLI enrollment URL that does
not exactly match `enrollment_url`; normal runtime code never uses it.

This is a configuration-schema change made before deployment. The development
APT package template, installer, examples, parser tests, pair tests, and
runbooks are updated together. Existing unpaired Sparks are rewritten during
the guided installation. An already paired agent must be stopped before its
root-owned configuration is changed and restarted only after validation.

## Compose runtime

The mutable development Compose artifact remains self-contained. Configuration
assets for Caddy and the LiteLLM supervisor are package resources in the control
API image. `dev-init` validates and copies their exact bytes into a dedicated
runtime-config volume with service-specific ownership; the NAS does not need a
source checkout or additional loose files. All third-party images are version-
and digest-pinned, run with dropped capabilities and
`no-new-privileges`, and have read-only filesystems plus narrowly scoped
writable volumes or tmpfs.

The stack adds:

- Caddy for the split agent ingress;
- LiteLLM plus the existing configuration supervisor;
- any one-shot ownership initialization required for their dedicated volumes.

The worker's current atomic route publisher is unchanged. A recipe run becomes
`published` only after LiteLLM starts with the exact generated configuration and
writes a recent acknowledgement bound to that activation marker. An empty
bootstrap configuration keeps LiteLLM healthy before any model is running.

The accepted upstream baseline remains local `main`, while the API continues to
mutate the separate local `deploy` branch. `dev-init` advances accepted `main`
to the image cohort commit and either advances an untouched `deploy` branch or
preserves locally committed development changes using the existing
`refs/vonk/deploy-base` merge-base contract. The generation settings therefore
continue to select `deploy`; changing them to `main` would break safe redeploys
after a direct development commit. The settings also enable the agent runtime,
select the built-in CA, and read management CIDRs from the protected projection.
Direct-fabric CIDRs remain separate and cannot be used as management endpoints.

## Slice 1: pairing and inventory

### Implementation

- Add the endpoint split and fail-closed development-agent settings.
- Add the PKI preparation and validation workflow.
- Add Caddy, secret projections, and documented `/etc/hosts` mappings.
- Configure each Spark with:
  - `enroll.vonk-forge.lan` as `enrollment_url`;
  - `agents.vonk-forge.lan` as `controller_url`;
  - the exact controller CA fingerprint;
  - its stable node ID;
  - management and direct-fabric facts discovered on that host.
- Generate a one-use enrollment grant through the loopback human API, pair one
  node at a time, approve it, and start the socket/supervisor services.

### Acceptance

- Untrusted LAN requests, spoofed identity headers, and requests outside the
  management CIDR fail closed.
- Enrollment is reachable without a client certificate, but no other agent
  endpoint is.
- Each one-use grant can pair only its intended node and cannot be replayed.
- Both agents present a valid issued certificate and report authenticated
  presence/inventory.
- Inventory reports the Rust runtime identity, architecture, OS, NVIDIA driver,
  container runtime, GPU/resources, artifact-store state, and declared recipe
  capabilities.
- Restarting the NAS stack and both agent services preserves identity and
  returns both nodes to ready state without re-pairing.

## Slice 2: synthetic source-first workload

### Fixture

Add one audited, intentionally tiny local recipe and source bundle. Its
digest-pinned Dockerfile builds a non-root HTTP service without downloading
model weights. The service exposes deterministic health and inference-shaped
responses. It declares realistic endpoint, lifecycle, resource, and security
fields while requiring negligible GPU and disk resources.

This fixture is test-only operational data, not a special runtime capability.
It traverses the same API, database, agent job, source policy, rootless Podman,
OCI archive, health, and route code used by real recipes.

### Lifecycle

1. Create and resolve the local recipe.
2. Upload and verify its source bundle.
3. Select Spark 1 as builder and request a build.
4. Upload the exact digest-bound OCI archive to controller state.
5. Create a mapping and distribute/import the exact image to the selected
   target.
6. Install, start, health-check, and publish the LiteLLM route.
7. Invoke the route through LiteLLM and verify the deterministic response.
8. Stop, confirm route withdrawal, uninstall, and verify idempotent cleanup.
9. Repeat the lifecycle after agent and NAS restarts.

### Acceptance

- Source-policy failures, digest mismatches, archive traversal, rootful build
  requests, stale presence, and resource overcommit are rejected.
- Build and imported image identities are exact and recorded.
- No route appears before workload health and exact LiteLLM acknowledgement.
- Stop or lost health withdraws the route.
- Interrupted operations recover or enter an explicit operator state; they do
  not silently repeat unsafe mutations.
- No synthetic-only branch exists in production code.

## Slice 3: real model and two-node validation

### Recipe selection

Use the smallest audited model/runtime combination that is demonstrably
compatible with Ubuntu 24.04, aarch64, DGX Spark GB10 (`sm_121`), the installed
driver, and rootless Podman. Prefer an existing pinned repository profile or an
official NVIDIA DGX Spark recipe. Before downloading weights, record:

- immutable base image digest and multi-architecture manifest evidence;
- model revision and artifact hashes;
- runtime arguments and expected context/resource envelope;
- license/access requirements; and
- available disk and memory on both nodes.

If a candidate fails these gates, reject it and select the next smallest
compatible audited candidate; never weaken source, image, or artifact checks to
force a launch. Credentials for a model provider remain root-owned Spark files
and are never sent to the controller or embedded in the recipe.

### Validation sequence

1. Run the model on one Spark and verify health plus a deterministic smoke
   inference through LiteLLM.
2. Stop and restart it, proving route withdrawal and republication.
3. Materialize a two-rank mapping across both Sparks using their direct-fabric
   addresses and measured bandwidth.
4. Ensure both nodes import the exact same image and model artifacts.
5. Start the gang, require every rank healthy, publish only the designated
   entrypoint, and run an inference through LiteLLM.
6. Stop one rank's rootless managed container while its agent remains healthy,
   then prove the authenticated complete snapshot marks only that rank failed,
   withdraws the route, and reports the gang unhealthy.
7. Start that exact managed container again, require a fresh successful health
   snapshot, republish, then restart agent and NAS services and verify
   reconciliation without rebuilding or redownloading immutable artifacts.
8. Stop and uninstall the validation deployment, preserving cached immutable
   artifacts according to normal reference-count policy.

### Acceptance

- The controller never routes to direct-fabric addresses or stale management
  observations.
- Gang startup and route publication are all-or-nothing.
- Exact image/model identity is equal on both nodes.
- At least one real inference succeeds through the user-facing LiteLLM route.
- Failure, recovery, restart, stop, and uninstall evidence is retained in the
  normal operation records.

## Documentation and operator workflow

Documentation is revised as one coherent contract:

- The generic development NAS guide says to place only
  `docker-compose.yml` and `secrets/` in the project directory, pull `:dev`, and
  redeploy manually.
- The secret guide lists every file, generation/import/rotation procedure,
  ownership expectation, safe 1Password backup workflow, and fingerprint
  verification. Examples never print private values.
- Node onboarding and package installation show the two URLs, CA pin, stable
  node ID, service ordering, and `/etc/hosts` entries on the NAS, both Sparks,
  and the operator workstation where needed:

  ```text
  192.168.1.231 enroll.vonk-forge.lan agents.vonk-forge.lan registry.vonk-forge.lan
  ```

  This address is explicitly labeled as the current-site example. The generic
  command takes the operator's NAS management address; no Carst-specific
  address, account, 1Password item ID, share name, or SSH alias is required by
  shipped code.

- Runbooks explain how to add or remove those entries idempotently, and how to
  verify name resolution before pairing.
- A slice runbook gives exact API/CLI checks, expected states, failure recovery,
  and cleanup for pairing, synthetic workload, and real model validation.
- Production documentation continues to say that production selection and
  updates are host-updater mediated and immutable; `:latest` is informational,
  not production deployment authority.

## Testing strategy

Implementation follows test-driven changes and keeps suites parallelizable.

- Rust unit/integration tests cover configuration compatibility, URL
  separation, CA pinning, pairing, mTLS normal calls, and package templates.
- Control tests cover development setting matrices, built-in CA validation,
  secret projection permissions, Caddy contract rendering, dev-init
  idempotence, route acknowledgement, and Compose structure.
- Contract tests validate the generated mutable and pinned artifacts without
  requiring private secrets.
- Existing recipe tests remain the main correctness suite; new end-to-end tests
  use the synthetic fixture through public service APIs.
- Container smoke tests start the complete development Compose stack locally
  with generated disposable secrets and test enrollment plus route
  acknowledgement. Hardware-only cases are marked explicitly and are executed
  on the two real Sparks before completion.
- Security checks scan generated image filesystems and artifacts, verify no
  secret bytes appear in images/artifacts/logs, and validate least-privilege
  mounts.
- A clean-room rehearsal generates fresh disposable PKI and secrets, renders
  the Compose project, installs a fresh agent configuration, and executes the
  synthetic slice using only checked-in code and documented inputs.

## Rollout and recovery

1. Merge only after all repository checks and review pass.
2. Let GitHub Actions publish the new public `:dev` cohort, development APT
   packages, and mutable Compose artifact. Local release publication remains
   prohibited.
3. Back up current NAS secrets and named volumes, copy the accepted Compose and
   new secret files, pull, and redeploy.
4. Add `/etc/hosts` entries and updated agent configuration on both Sparks.
5. Execute Slice 1, then Slice 2, then Slice 3; do not skip a failed gate.
6. Remove temporary passwordless sudo from NAS and Sparks after validation and
   verify unattended sudo no longer works.

Before node enrollment, rollback is the previous pinned development Compose and
the documented repository-volume reset when moving to an older accepted
baseline. After enrollment, preserve PKI and database volumes unless the
recovery procedure explicitly rotates identities. A failed workload never
requires deleting node identity. Workload cleanup uses normal stop/uninstall and
reference-count behavior rather than broad volume or artifact deletion.

## Completion criteria

The work is complete only when repository tests, local complete-stack smoke
tests, GitHub checks, NAS deployment, both physical Spark enrollments, the
synthetic lifecycle, real single-node inference, real two-node inference,
failure/recovery, restart persistence, documentation audit, secret scan, and
temporary-sudo removal all have recorded evidence. A code-only implementation
or a merely healthy control API is not completion.
