# Docker control-host deployment

This is the authoritative operator entry point for a production NAS deployment.
It applies a reviewed platform release to any supported `linux/amd64`,
Docker-capable control host. A NAS is convenient but not required. Production
never executes Compose from the repository checkout: the root-owned host
updater selects a TUF-authorized platform target, loads the verified OCI deployment bundle,
and runs only the resulting immutable generation.

The checkout and TUF release described here govern platform services, fleet
policy, and the compatibility workload-release projection. They are not a
recipe authoring gate. Local PostgreSQL is authoritative for recipe families,
authored/imported recipe revisions, WorkloadRun import reports, installations,
placements, and runs; those records remain available when Git or the optional
global catalog is unavailable.

## Development release channel

Development and production use this same Compose graph and the same site
configuration. They are mutually exclusive release channels for one
deployment, not two stacks intended to share a network. The only selected
runtime difference is the immutable API and worker image version published by
GitHub Actions. Use `scripts/dev-compose` with `VONK_DEV_API_IMAGE` and
`VONK_DEV_WORKER_IMAGE` set to those GHCR references; it does not build images,
clone source, create synthetic credentials, or select alternate ports/volumes.

The real `control-api` container performs one bounded privileged pre-exec phase.
It normalizes secrets, prepares shared-volume ownership, upgrades the maintained
Alembic head under a PostgreSQL advisory lock, and creates the initial authority
head before serving. It then clears supplementary groups, sets its real,
effective, and saved GID/UID to `10001`, verifies that `/run/secrets` is no
longer traversable, and execs the API so Uvicorn remains PID 1. Services that
consume this initialized state depend on `control-api` health; no sleeping or
exited helper container is part of the project.

The complete NAS project contains only:

```text
vonk-forge/
├── docker-compose.yaml
└── secrets/
    ├── postgres-password
    ├── database-url
    ├── admin-password-verifier
    ├── host-runtime-grant-private-key
    ├── agent-ca-certificate
    ├── agent-ca-key
    ├── agent-proxy-auth
    ├── controller-ca
    ├── controller-server-certificate
    ├── controller-server-key
    ├── litellm-database-password
    ├── litellm-master-key
    ├── litellm-upstream-key
    ├── management-cidrs
    ├── tailscale-oauth-client-id
    ├── tailscale-oauth-client-secret
    └── token-signing-key
```

Follow [Development NAS installation and runtime secrets](../../docs/runbooks/development-nas-installation.md)
for secure generation, host ownership and modes, generic NAS UI import,
rotation, backup, and first-start checks. No GitHub, registry, TUF, mTLS,
Cloudflare, or production credential belongs in this project.

The wrapper reads the same `.env`, secret paths, PKI state, named volumes,
hostnames, and management policy as the production deployment. Do not create a
parallel development secret or data root. Certificate issuance and renewal
remain the shared Step CA/Caddy responsibility described in the PKI runbook.

## Recipe containers are source-first

The Compose deployment and recipe workloads are separate layers. Compose starts
the Vonk Forge control services on the NAS; it does not build or publish a
community workload image. A recipe carries a digest-bound source bundle with
its Dockerfile and build context. The controller validates that bundle, checks
the builder's temporary disk and memory, and asks one compatible GPU-node agent
to perform a rootless Podman/Buildah build with the recipe's declared policy.

That rootless runtime is a build sandbox only. After export and digest
verification, each enrolled Spark imports and starts the accepted image through
its controller-signed helper and the Spark-managed Docker/NVIDIA runtime. The
agent has no Docker-socket access and no Docker-group membership. Raw RDMA,
host networking, arbitrary devices, privileged mode, and host socket mounts
are outside the workload contract.

The resulting OCI layout is retained in the controller's ephemeral transfer
staging area and is identified by its immutable image digest. PostgreSQL stores
the build authority and evidence; the enrolled node owns the imported runtime
image. For a multi-node mapping, Vonk
transfers that exact OCI layout through the authenticated agent channel and
verifies the digest on every target node; it never rebuilds independently on
each node. A community container registry is therefore not required. The
global catalog, when enabled, stores recipe metadata and source bundles, not
workload image layers or registry credentials.

Builds declaring `network.mode: public` are currently rejected at the agent
boundary until a dedicated egress proxy/firewall is deployed; `slirp4netns`
alone cannot enforce a hostname allowlist. Networkless recipes (or recipes
whose pinned base is already available in the node's local cache) can build now.
The rejection is deliberate and visible in the build admission result; it never
silently widens a recipe allowlist to unrestricted network access.

This source-first path applies to recipe workloads only. The API, worker, and
optional Hermes images used by this Compose project remain platform release
artifacts and are selected from the signed, digest-pinned platform target
described below.

## Current release state

No images are currently being published. Repository variables
`VONK_CONTAINER_RELEASES_ENABLED` and `VONK_PLATFORM_RELEASES_ENABLED` are
deliberately unset (default-off) until the entire repository is release-ready.
A maintainer must set both to `true` in a protected GitHub environment before
the stable-tag workflow can publish. Dependabot cannot publish: it only opens
weekly dependency-update pull requests, which a maintainer must review, merge,
and deliberately release with a stable version tag after enablement.

When enabled, one stable version tag builds and publishes these three packages
and the matching ARM64 `vonk-forge-agent` Debian package as one platform
release:

```text
ghcr.io/carstvaartjes/vonk-forge-api
ghcr.io/carstvaartjes/vonk-forge-worker
ghcr.io/carstvaartjes/vonk-forge-hermes
GitHub Release assets: the signed `vonk-forge-agent_<version>_arm64.deb`
```

Do not deploy an individual package, a tag, or a workflow summary. Select the
immutable `platform/releases/<version>/<sha256>.json` target published by the
release workflow. That target pins all three images, the exact agent package
evidence, and one verified OCI deployment bundle containing the exact Compose
graph and configuration assets. Never install an agent package or deploy an
image from a different tag than the selected platform target.
Maintainers use [Platform release publication](../../docs/runbooks/platform-release-publication.md);
control-host operators use
[Platform release update](../../docs/runbooks/platform-release-update.md).

## Host and network prerequisites

Use a supported `linux/amd64` machine with Docker Engine plus the Docker
Compose plugin, ORAS 1.3, age, POSIX ACL tools (`setfacl` and `getfacl`), local
DNS, and persistent storage. Install the reviewed host-updater package as a
root-owned executable; neither that executable nor its Python package may be
writable by a service UID. Keep the host updater, site configuration,
application data, and deployment secrets in separate host trees:

```bash
sudo install -d -m 0755 -o root -g root /srv/vonk-forge
sudo install -d -m 0700 -o root -g root /srv/vonk-forge/control-host
sudo install -d -m 0755 -o root -g root /srv/vonk-forge/control-identity
sudo install -d -m 0700 -o root -g root /srv/vonk-forge/site
sudo install -d -m 0700 /srv/vonk-forge/secrets /srv/vonk-forge/hermes /srv/vonk-forge/step-ca
```

The updater reads site values from the root-owned `/srv/vonk-forge/site`
boundary and release assets from verified generations. The control API does
not mount a source checkout and does not write Git metadata. PostgreSQL is the
authoritative store for control-plane documents, proposals, recipe data, source
bundles, job logs, and fleet state; the deployment bundle contains only the
immutable service configuration and runtime assets.

Reserve a host management-LAN address and put it in the host-local `.env`:

```dotenv
NAS_LAN_IP=10.0.0.2
```

`NAS_LAN_IP` is the NAS host's physical management-LAN address: it is not the Docker bridge, not a Tailscale `100.x` address, and not the public WAN address. Resolve these names only on the management LAN:

```text
enroll.vonk-forge.lan   10.0.0.2
agents.vonk-forge.lan   10.0.0.2
registry.vonk-forge.lan 10.0.0.2
```

Allow TCP 8443 to that LAN address only from the canonical GPU node management
CIDRs (preferably reserved GPU node leases). Human control, Grafana, inference,
and Hermes have no LAN or WAN access: use the exact Tailscale Services in
[the Tailscale runbook](../../docs/runbooks/tailscale.md). There is no LAN
fallback for tailnet-only access.

`control.vonk-forge.lan is not a LAN-accessible human endpoint`: do not create a
general-purpose LAN record or firewall rule for it. Human control reaches the
Tailscale `svc:vonk-forge` Service; the only published NAS LAN listener is the
GPU node-restricted TCP 8443 backend.

## Select a complete platform release

The protected release workflow publishes the deployment bundle first, then an
immutable TUF target, and updates the signed `stable` channel last. Production
selects the signed `stable` channel through the trusted host updater, which
resolves it to one immutable TUF target. The operator never turns a channel, a
Git tag, or an OCI tag into a Compose install target:

```text
platform/releases/X.Y.Z/REPLACE_MANIFEST_SHA256.json
```

The target binds the canonical manifest bytes, all three image digests, the
deployment-bundle manifest and layer descriptors, supported host-updater ABI,
database revision, and exact authorized predecessors. The updater downloads
OCI manifest and layer bytes by digest, verifies their media types, sizes, and
digests, verifies the canonical bundle, and renders Compose inside a new
root-owned generation. A public package needs no NAS GitHub token. For each
newly created package, a maintainer performs the one-time GitHub web setting:
package page → **Package settings** → **Danger Zone** → **Change visibility**
→ **Set package visibility to Public**. Never put a GitHub token in site
configuration to work around package visibility.

## Host-local `.env` inputs

All values below are host-local configuration. Store the updater-owned site
environment at `/srv/vonk-forge/site/.env`, mode `0600`, with paths and
non-secret configuration only; **no secret value belongs in `.env`**. A
release bundle supplies the Compose model. The site environment never supplies
release asset paths or executable content.

### Images

The selected platform target supplies these three values, each including a
version tag and `@sha256:` digest. Do not edit them independently:

```dotenv
CONTROL_API_IMAGE=ghcr.io/carstvaartjes/vonk-forge-api:X.Y.Z@sha256:REPLACE
CONTROL_WORKER_IMAGE=ghcr.io/carstvaartjes/vonk-forge-worker:X.Y.Z@sha256:REPLACE
HERMES_AGENT_IMAGE=ghcr.io/carstvaartjes/vonk-forge-hermes:X.Y.Z@sha256:REPLACE
```

Keep the checked-in upstream image pins (`POSTGRES_IMAGE`, `CADDY_IMAGE`,
`REGISTRY_IMAGE`, `LITELLM_IMAGE`, `PROMETHEUS_IMAGE`, `GRAFANA_IMAGE`,
`STEP_CA_IMAGE`, and `TAILSCALE_IMAGE`) version-and-digest pinned.

### NAS paths and networking

Set `COMPOSE_PROJECT_NAME`, `HERMES_DATA_ROOT`,
`NAS_LAN_IP`, `VONK_MANAGEMENT_CIDRS`, and optional
`VONK_DIRECT_FABRIC_CIDRS`. The control authority and recipe catalog are stored
in PostgreSQL; the NAS bundle contains only Compose configuration, secrets,
images, and Docker volumes.
`HERMES_DATA_ROOT` contains `data`, `workspaces`, and `cache`.
The control state volume contains durable controller state. OCI upload staging is an API-local tmpfs and
is deliberately lost on API restart; the build remains retryable from its
PostgreSQL record. Workload trust metadata remains isolated in the dedicated
workload TUF publication volume.

### Hostnames

Set `VONK_CONTROL_HOSTNAME`, `VONK_AGENT_ENROLL_HOSTNAME`,
`VONK_AGENT_HOSTNAME`, and `VONK_REGISTRY_HOSTNAME` to the names served by Caddy.
For the management-LAN example they are `control.vonk-forge.lan`,
`enroll.vonk-forge.lan`, `agents.vonk-forge.lan`, and
`registry.vonk-forge.lan` respectively. Set `HERMES_DASHBOARD_ORIGIN` to the one
exact `svc:hermes-dashboard` HTTPS origin supplied by Tailscale.

### PKI

Step CA is part of the canonical production-shaped graph. Set
`AGENT_CLIENT_CA_FILE`, `AGENT_INTERMEDIATE_CERTIFICATE_FILE`,
`AGENT_PROXY_AUTH_FILE`, `AGENT_CA_CREDENTIAL_FILE`,
`AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE`, `AGENT_CA_PROVISIONER_NAME`,
`AGENT_CA_PROVISIONER_KID`, `STEP_CA_CONFIG_FILE`,
`STEP_CA_ROOT_CERTIFICATE_FILE`, `STEP_CA_INTERMEDIATE_KEY_FILE`, and
`STEP_CA_PASSWORD_FILE`. Development uses the same provider and Compose graph,
with separate synthetic PKI credentials and disposable CA state.

### Required secret-file paths

Set every required secret path in `.env`; these are paths only, never values:
`DATABASE_URL_FILE`, `POSTGRES_PASSWORD_FILE`, `TOKEN_SIGNING_KEY_FILE`,
`METRICS_TOKEN_FILE`, `WORKER_API_TOKEN_FILE`,
`HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE`,
`LITELLM_MASTER_KEY_FILE`, `LITELLM_UPSTREAM_KEY_FILE`,
`LITELLM_DATABASE_URL_FILE`, `LITELLM_DATABASE_PASSWORD_FILE`,
`GRAFANA_ADMIN_PASSWORD_FILE`,
`AGENT_CLIENT_CA_FILE`, `AGENT_INTERMEDIATE_CERTIFICATE_FILE`,
`AGENT_PROXY_AUTH_FILE`, `AGENT_CA_CREDENTIAL_FILE`,
`AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE`, `STEP_CA_ROOT_CERTIFICATE_FILE`,
`STEP_CA_INTERMEDIATE_KEY_FILE`, `STEP_CA_PASSWORD_FILE`,
`TAILSCALE_OAUTH_CLIENT_ID_FILE`, `TAILSCALE_OAUTH_CLIENT_SECRET_FILE`, and
`HERMES_API_KEY_FILE`.

### Tailscale and Hermes

Set `TAILSCALE_OAUTH_CLIENT_ID_FILE` and
`TAILSCALE_OAUTH_CLIENT_SECRET_FILE` to local OAuth secret files. For the
official published Hermes wrapper, `HERMES_UID=1100` and `HERMES_GID=1100` are
fixed image requirements, not tunable runtime choices. Set
`HERMES_API_KEY_FILE` to the local key file. Optional resource limits
`HERMES_CPUS`, `HERMES_MEMORY_LIMIT`, and `HERMES_MEMORY_RESERVATION` retain
their Compose defaults unless the host is deliberately sized differently.

## Secret files

Create regular files under `/srv/vonk-forge/secrets` and parent directories mode
`0700`. The `control-api` pre-exec reads these host-backed Compose secrets as
root and copies them into the Docker-managed
`normalized-private-keys` volume with the exact owner needed by each non-root
consumer. The API image pre-creates `/run/secrets` as `root:root 0700`; after
the irreversible identity drop, the API process cannot traverse the source
secret directory. This is required for standalone Compose on NAS platforms:
file-backed Compose secrets are bind mounts, so Compose cannot reliably remap
their UID/GID/mode at container start.
Keep the source files `root:root 0400`; do not add host ACLs just to make a
non-root service read a bind-mounted secret.

The steady-state service users are `10001:10001` for control-api and control-worker,
`10002:10001` for LiteLLM, `65534:65534` for Prometheus, and `472:472` for
Grafana. The pinned step-ca image runs as `1000:1000` (`step`); Hermes' managed
process is fixed at `1100:1100`. PostgreSQL, Caddy, Tailscale, and the Hermes
entrypoint use their image startup identity and can read a `root:root 0400`
secret before dropping privileges. Re-check these identities after changing an
image pin with `docker image inspect IMAGE --format '{{.Config.User}}'` and,
for the pinned step-ca image, `docker run --rm --entrypoint id STEP_CA_IMAGE`.

Use one value per file, with a final newline only where the consumer format
permits it; never export a secret into `.env`, shell history, or a Compose
command line.

| `.env` path key → file | Staged consumer projection | Required content |
| --- | --- | --- |
| `DATABASE_URL_FILE` → `database-url` | API/worker `10001:10001 0400` | PostgreSQL URL. |
| `POSTGRES_PASSWORD_FILE` → `postgres-password` | PostgreSQL startup, `root:root 0400` | One PostgreSQL password. |
| `TOKEN_SIGNING_KEY_FILE` → `token-signing-key` | API `10001:10001 0400` | At least 32 bytes. |
| `METRICS_TOKEN_FILE` → `metrics-token` | API `10001:10001 0400`; Prometheus gets a separate `65534:65534 0400` projection | At least 16 non-whitespace characters. |
| `WORKER_API_TOKEN_FILE` → `worker-api-token` | API/worker `10001:10001 0400` | One unpadded base64url token, at least 32 characters. |
| `PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE` → `package-helper-grant-private-key` | Control API `10001:10001`, `10001:10001 0400` | Dedicated Ed25519 PKCS#8 PEM for short-lived workload-helper grants; never install on a GPU node. |
| `PACKAGE_HELPER_RECEIPT_PRIVATE_KEY_FILE` → `package-helper-receipt-private-key` | Control API `10001:10001`, `10001:10001 0400` | Independent Ed25519 PKCS#8 PEM for object receipts; never reuse the grant key or install on a GPU node. |
| `HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE` → `host-runtime-grant-private-key` | Control API `10001:10001`, `10001:10001 0400` | Dedicated Ed25519 PKCS#8 PEM for exact Spark Docker runtime grants. Install only its raw 32-byte public key (lowercase hexadecimal plus newline) at `/etc/vonk-forge-agent/host-helper-authority.pub` on GPU nodes. |
| `LITELLM_MASTER_KEY_FILE`, `LITELLM_UPSTREAM_KEY_FILE`, `LITELLM_DATABASE_URL_FILE` → matching `litellm-*` files | LiteLLM `10002:10001`, `10002:10001 0400` | Respectively the master key, dedicated upstream key, and PostgreSQL URL. |
| `GRAFANA_ADMIN_PASSWORD_FILE` → `grafana-admin-password` | Grafana `472:472`, `472:472 0400` | One Grafana administrator password. |
| `AGENT_CLIENT_CA_FILE` → `agent-client-ca` | API `10001:10001 0400`; Caddy reads its root-startup secret | PEM trust bundle. |
| `AGENT_INTERMEDIATE_CERTIFICATE_FILE` → `agent-intermediate-certificate` | API `10001:10001 0400`; Step CA gets `1000:1000 0400` | PEM intermediate certificate. |
| `AGENT_PROXY_AUTH_FILE` → `agent-proxy-auth` | API `10001:10001 0400`; Caddy reads its root-startup secret | One unpadded base64url token, at least 32 characters. |
| `AGENT_CA_CREDENTIAL_FILE` → `agent-ca-credential` | API `10001:10001 0400` | Private provisioner credential. |
| `AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE` → `agent-ca-public.jwk` | API `10001:10001 0400` | Public provisioner JWK. |
| `STEP_CA_ROOT_CERTIFICATE_FILE` → `step-ca-root-certificate` | API `10001:10001 0400`; Step CA gets `1000:1000 0400` | PEM root certificate. |
| `STEP_CA_INTERMEDIATE_KEY_FILE`, `STEP_CA_PASSWORD_FILE` → `step-ca-intermediate-key`, `step-ca-password` | Step CA `1000:1000 0400` | Encrypted intermediate private key and its one password. |
| `TAILSCALE_OAUTH_CLIENT_ID_FILE`, `TAILSCALE_OAUTH_CLIENT_SECRET_FILE` → matching `tailscale-oauth-*` files | Tailscale startup, `root:root 0400` | One OAuth client ID or secret; neither is a GitHub credential. |
| `HERMES_API_KEY_FILE` → `hermes-api-key` | Hermes entrypoint then managed `1100:1100`; `root:root 0400` | One 32+ character key using only `A-Z`, `a-z`, `0-9`, `_`, `.`, `~`, or `-`. |

The offline root private key never enters this NAS. The generated PKI paths and
permissions in [agent PKI](../../docs/runbooks/agent-pki.md) remain authoritative
for the step-ca material.

## Bootstrap the production-shaped step-ca overlay

Follow [agent PKI](../../docs/runbooks/agent-pki.md) first to create the
offline root, online intermediate, provisioner material, generated
`/srv/vonk-forge/step-ca/ca.json`, and all PKI secret files. Then follow
[Tailscale](../../docs/runbooks/tailscale.md) to create the scoped OAuth client,
tailnet policy, and exact Services. Do this in order:

1. Prepare the host paths, local DNS, all `.env` entries, and secret files.
2. Complete the agent-PKI production step-ca material and copy only its online
   artifacts to the paths named in `.env`.
3. Complete Tailscale policy and OAuth secret setup; do not enable a LAN
   fallback.
4. Record the exact immutable TUF target name and verify that the current
   trusted metadata retains every predecessor required for rollback.
5. Preview first selection with the root-owned host updater and review the
   release, bundle, image, database, space, site-config, and backup bindings.
6. Apply that exact plan. The updater initializes PostgreSQL, migrates, starts
   the candidate API in inert preselection mode, selects the generation, and
   requires generation-bound API and worker readiness.

The canonical graph includes Step CA for both production and development.
Development uses different immutable release identities and site-local
credentials; the provider topology does not change.

## Install and first selection

Install ORAS and age at the absolute paths configured by the host-updater
package. Install the package and its `vonk-control-offline` entry point as
`root:root`, mode `0755`, from the signed first-release bootstrap artifact. The
bootstrap artifact is the only out-of-band first-install input; after this,
successor tooling is accepted only when the currently installed updater
supports the target's declared updater ABI. Never install this entry point from
an unreviewed branch checkout.

Configure the trusted TUF root, metadata/target URLs, root-owned age-recipient
file, and root-owned site environment as described in
[Platform release update](../../docs/runbooks/platform-release-update.md).
Preview the immutable target without mutation, then apply the same target:

```bash
target_name=platform/releases/X.Y.Z/REPLACE_MANIFEST_SHA256.json
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  upgrade --target-name "$target_name"
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  upgrade --target-name "$target_name" --apply
```

The updater holds one operation lock through TUF refresh, OCI acquisition,
bundle validation, fixed backup, migration, candidate preselection, selection,
and generation-bound worker readiness. It runs Compose only from the verified
generation directory and records an immutable hash-chained journal. After the
first successful selection, create the first administrator through the web/CLI
admin workflow and apply the Hermes egress rule documented in
[Hermes Agent](../../docs/runbooks/hermes-agent.md). Verify the exact Tailscale
Services and confirm ordinary LAN clients cannot reach human or Hermes
endpoints.

## Upgrade and rollback

For an upgrade, resolve the next signed channel document to its immutable target
name, then preview and apply that exact name. Never edit image names or a
Compose model as the deployment mechanism:

```bash
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  upgrade --target-name "$target_name"
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  upgrade --target-name "$target_name" --apply
```

If an operation is unfinished, preview and apply only its journaled recovery:

```bash
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host recover
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host recover --apply
```

For an operator-requested rollback, use the recorded predecessor generation.
The current TUF target set must still authorize its exact target and bundle:

```bash
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  rollback --generation REPLACE_GENERATION_ID
sudo vonk-control-offline --state-path /srv/vonk-forge/control-host \
  rollback --generation REPLACE_GENERATION_ID --apply
```

Do not deploy a partial publication, a digest copied from a registry page, a
revoked predecessor, or a release whose OCI bundle cannot be verified exactly.

## Evaluation-only `latest`

For a disposable, explicitly non-production evaluation only, these public
aliases may be selected:

```text
ghcr.io/carstvaartjes/vonk-forge-api:latest
ghcr.io/carstvaartjes/vonk-forge-worker:latest
ghcr.io/carstvaartjes/vonk-forge-hermes:latest
```

`:latest` is evaluation/discovery only. It is also informational only:
production must not use these aliases; it requires the version-and-digest
references selected from one complete release asset by the trusted host
updater. Docker does not continuously update running containers: changing
`latest` remotely has no effect until an operator explicitly pulls and
recreates containers. Evaluation users must still deliberately pull and
recreate, and must not mistake that for a production update path.
