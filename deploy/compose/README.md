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

The production graph uses one persistent, network-isolated `control-bootstrap`
service for privileged shared-volume preparation. It remains healthy after
creating the signer sockets, TUF publication directories, route state, and
API-only admin-grant runtime key. This intentionally replaces several stopped
one-shot init services because some NAS Docker UIs incorrectly mark successful
`Exited (0)` init containers as a failed project. The long-running API, worker,
signers, and LiteLLM services retain their restricted users and depend on the
bootstrap health check; the helper does not serve application traffic.

The complete NAS project contains only:

```text
vonk-forge/
├── docker-compose.yaml
└── secrets/
    ├── postgres-password
    ├── database-url
    ├── admin-password-verifier
    ├── git-signing-key
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

The resulting OCI layout is retained in the local artifact store and is
identified by its immutable image digest. For a multi-node mapping, Vonk
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
writable by a service UID. Keep authority, site configuration, application
data, and the admin Git repository in separate host trees:

```bash
operator_user=$(id -un)
operator_group=$(id -gn)
sudo install -d -m 0755 -o root -g root /srv/vonk-forge
sudo install -d -m 0700 -o root -g root /srv/vonk-forge/control-host
sudo install -d -m 0755 -o root -g root /srv/vonk-forge/control-identity
sudo install -d -m 0700 -o root -g root /srv/vonk-forge/site
sudo install -d -m 0750 -o "$operator_user" -g "$operator_group" /srv/vonk-forge/admin-repository
git clone https://github.com/CarstVaartjes/vonk-forge.git /srv/vonk-forge/admin-repository
sudo install -d -m 0700 /srv/vonk-forge/secrets /srv/vonk-forge/hermes /srv/vonk-forge/step-ca
```

Use `/srv/vonk-forge/admin-repository` for platform and release-policy source.
The deployment operator owns this checkout so later reviewed Git updates work.
`id -gn` records the actual primary group; do not
assume a same-named group. The updater reads site values from the root-owned
`/srv/vonk-forge/site` boundary and release assets from verified generations,
never from this checkout.

`control-api` mounts this checkout read-write as UID `10001:10001` for the
platform/release administration path. CONTROL_API writes `.git` for signed
changes.
Recipe CRUD and imports use the local catalog database instead. Preserve
operator administration while granting both the named operator and UID 10001
recursive read/write/traverse access now and on all future files and
directories. The access-control masks keep both named-user entries effective:

```bash
sudo setfacl -R -m u:"$operator_user":rwX,u:10001:rwX,m::rwX /srv/vonk-forge/admin-repository
sudo find /srv/vonk-forge/admin-repository -type d -exec setfacl -m \
  u:"$operator_user":rwx,u:10001:rwx,m::rwx,d:u:"$operator_user":rwx,d:u:10001:rwx,d:m::rwx {} +
sudo getfacl /srv/vonk-forge/admin-repository /srv/vonk-forge/admin-repository/.git
```

The first command repairs existing operator- or UID-10001-created entries; the
default ACLs on every directory make the rule bidirectional for future files.
Reapply both commands after a checkout replacement or restore. Do not replace
this with `chown -R 10001`, which would remove the operator's administrative
ownership. The secrets, Hermes data, and step-ca directories stay
administrator-owned and are prepared with the consumer-specific ownership below.

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

Set `COMPOSE_PROJECT_NAME`, `REPOSITORY_PATH`, `HERMES_DATA_ROOT`,
`NAS_LAN_IP`, `VONK_BACKEND_PORT`, `VONK_MANAGEMENT_CIDRS`, and optional
`VONK_DIRECT_FABRIC_CIDRS`. `REPOSITORY_PATH` is the platform/release checkout
mounted into the API as data; it is not the Compose source or the recipe
catalog database.
`HERMES_DATA_ROOT` contains `data`, `workspaces`, and `cache`.
The control state volume contains the explicitly separated agent artifact and
TUF publication roots. Active mTLS-authenticated GPU nodes can read only bounded,
strictly named regular files below `/state/agent-tuf/metadata` and
`/state/agent-tuf/targets`; never place signing keys or registry credentials in
either publication directory.

### Hostnames

Set `VONK_CONTROL_HOSTNAME`, `VONK_AGENT_ENROLL_HOSTNAME`,
`VONK_AGENT_HOSTNAME`, and `VONK_REGISTRY_HOSTNAME` to the names served by Caddy.
For the management-LAN example they are `control.vonk-forge.lan`,
`enroll.vonk-forge.lan`, `agents.vonk-forge.lan`, and
`registry.vonk-forge.lan` respectively. Set `HERMES_DASHBOARD_ORIGIN` to the one
exact `svc:hermes-dashboard` HTTPS origin supplied by Tailscale.

### PKI

For the production `compose.step-ca.yaml` overlay set
`AGENT_CLIENT_CA_FILE`, `AGENT_INTERMEDIATE_CERTIFICATE_FILE`,
`AGENT_PROXY_AUTH_FILE`, `AGENT_CA_CREDENTIAL_FILE`,
`AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE`, `AGENT_CA_PROVISIONER_NAME`,
`AGENT_CA_PROVISIONER_KID`, `STEP_CA_CONFIG_FILE`,
`STEP_CA_ROOT_CERTIFICATE_FILE`, `STEP_CA_INTERMEDIATE_KEY_FILE`, and
`STEP_CA_PASSWORD_FILE`. `AGENT_INTERMEDIATE_KEY_FILE` is development-only for
the mutually exclusive built-in CA overlay.

### Required secret-file paths

Set every required secret path in `.env`; these are paths only, never values:
`DATABASE_URL_FILE`, `POSTGRES_PASSWORD_FILE`, `TOKEN_SIGNING_KEY_FILE`,
`METRICS_TOKEN_FILE`, `GIT_SIGNING_KEY_FILE`, `WORKER_API_TOKEN_FILE`,
`HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE`,
`AGENT_UPDATE_AUTHORITY_KEY_FILE`, `ADMIN_GRANT_PUBLIC_KEY_FILE`,
`AGENT_TUF_BOOTSTRAP_ROOT_FILE`,
`LITELLM_MASTER_KEY_FILE`, `LITELLM_UPSTREAM_KEY_FILE`,
`LITELLM_DATABASE_URL_FILE`, `GRAFANA_ADMIN_PASSWORD_FILE`,
`AGENT_CLIENT_CA_FILE`, `AGENT_INTERMEDIATE_CERTIFICATE_FILE`,
`AGENT_PROXY_AUTH_FILE`, `AGENT_CA_CREDENTIAL_FILE`,
`AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE`, `STEP_CA_ROOT_CERTIFICATE_FILE`,
`STEP_CA_INTERMEDIATE_KEY_FILE`, `STEP_CA_PASSWORD_FILE`,
`TAILSCALE_OAUTH_CLIENT_ID_FILE`, `TAILSCALE_OAUTH_CLIENT_SECRET_FILE`, and
`HERMES_API_KEY_FILE`. The development-only built-in overlay additionally needs
`AGENT_INTERMEDIATE_KEY_FILE`.

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
`0700`. Compose bind-backs each secret file, so host ownership and mode must
allow the **actual consuming container UID** to read it. Do not use a blanket
`root:root 0600` rule: it prevents the explicitly non-root API, worker,
LiteLLM, Prometheus, and Grafana services from reading their secret mounts.

The Compose service users are `10001:10001` for control-api and control-worker,
`10003:10001` for the networkless control-signer,
`10002:10001` for LiteLLM, `65534:65534` for Prometheus, and `472:472` for
Grafana. The pinned step-ca image runs as `1000:1000` (`step`); Hermes' managed
process is fixed at `1100:1100`. PostgreSQL, Caddy, Tailscale, and the Hermes
entrypoint use their image startup identity and can read a `root:root 0400`
secret before dropping privileges. Re-check these identities after changing an
image pin with `docker image inspect IMAGE --format '{{.Config.User}}'` and,
for the pinned step-ca image, `docker run --rm --entrypoint id STEP_CA_IMAGE`.

For a secret with one non-root consumer, use its exact owner and mode `0400`.
For a secret shared by different UIDs, retain `root:root 0400` and grant only
the listed service UIDs read access with POSIX ACLs. For example, the metrics
token is read by both control-api (`10001`) and Prometheus (`65534`):

```bash
sudo chown root:root /srv/vonk-forge/secrets/metrics-token
sudo chmod 0400 /srv/vonk-forge/secrets/metrics-token
sudo setfacl -m u:10001:r,u:65534:r /srv/vonk-forge/secrets/metrics-token
sudo getfacl /srv/vonk-forge/secrets/metrics-token
```

Use one value per file, with a final newline only where the consumer format
permits it; never export a secret into `.env`, shell history, or a Compose
command line.

| `.env` path key → file | Consumer UID(s), host ownership/mode | Required content |
| --- | --- | --- |
| `DATABASE_URL_FILE` → `database-url` | `10001:10001`, `10001:10001 0400` | PostgreSQL URL. |
| `POSTGRES_PASSWORD_FILE` → `postgres-password` | PostgreSQL startup, `root:root 0400` | One PostgreSQL password. |
| `TOKEN_SIGNING_KEY_FILE` → `token-signing-key` | API `10001:10001`, `10001:10001 0400` | At least 32 bytes. |
| `METRICS_TOKEN_FILE` → `metrics-token` | API `10001`, Prometheus `65534`; `root:root 0400` plus ACLs | At least 16 non-whitespace characters. |
| `GIT_SIGNING_KEY_FILE` → `git-signing-key` | API `10001:10001`, `10001:10001 0400` | Private SSH signing key. |
| `WORKER_API_TOKEN_FILE` → `worker-api-token` | API/worker `10001:10001`, `10001:10001 0400` | One unpadded base64url token, at least 32 characters. |
| `AGENT_UPDATE_AUTHORITY_KEY_FILE` → `agent-update-authority-key` | Signer only, `10003:10001 0400` | Ed25519 PKCS#8 PEM. The API, worker, and every GPU node must never receive this private key. |
| `ADMIN_GRANT_PUBLIC_KEY_FILE` → `admin-grant-public-key` | Signer only, `10003:10001 0400` | Canonical public document for the separate API admin-action grant authority. |
| `PACKAGE_HELPER_GRANT_PRIVATE_KEY_FILE` → `package-helper-grant-private-key` | Control API `10001:10001`, `10001:10001 0400` | Dedicated Ed25519 PKCS#8 PEM for short-lived workload-helper grants; never install on a GPU node. |
| `PACKAGE_HELPER_RECEIPT_PRIVATE_KEY_FILE` → `package-helper-receipt-private-key` | Control API `10001:10001`, `10001:10001 0400` | Independent Ed25519 PKCS#8 PEM for object receipts; never reuse the grant key or install on a GPU node. |
| `HOST_RUNTIME_GRANT_PRIVATE_KEY_FILE` → `host-runtime-grant-private-key` | Control API `10001:10001`, `10001:10001 0400` | Dedicated Ed25519 PKCS#8 PEM for exact Spark Docker runtime grants. Install only its raw 32-byte public key (lowercase hexadecimal plus newline) at `/etc/vonk-forge-agent/host-helper-authority.pub` on GPU nodes. |
| `AGENT_TUF_BOOTSTRAP_ROOT_FILE` → `agent-tuf-bootstrap-root` | Signer only, `10003:10001 0400` | Explicit trusted public TUF root for platform releases. The corresponding offline root private key never enters the NAS. |
| `LITELLM_MASTER_KEY_FILE`, `LITELLM_UPSTREAM_KEY_FILE`, `LITELLM_DATABASE_URL_FILE` → matching `litellm-*` files | LiteLLM `10002:10001`, `10002:10001 0400` | Respectively the master key, dedicated upstream key, and PostgreSQL URL. |
| `GRAFANA_ADMIN_PASSWORD_FILE` → `grafana-admin-password` | Grafana `472:472`, `472:472 0400` | One Grafana administrator password. |
| `AGENT_CLIENT_CA_FILE` → `agent-client-ca` | API `10001` and Caddy startup; `root:root 0400` plus ACL `u:10001:r` | PEM trust bundle. |
| `AGENT_INTERMEDIATE_CERTIFICATE_FILE` → `agent-intermediate-certificate` | API `10001`, step-ca `1000`; `root:root 0400` plus `u:10001:r,u:1000:r` ACLs | PEM intermediate certificate. |
| `AGENT_PROXY_AUTH_FILE` → `agent-proxy-auth` | API `10001` and Caddy startup; `root:root 0400` plus ACL `u:10001:r` | One unpadded base64url token, at least 32 characters. |
| `AGENT_CA_CREDENTIAL_FILE` → `agent-ca-credential` | API `10001:10001`, `10001:10001 0400` | Private provisioner credential. |
| `AGENT_CA_PROVISIONER_PUBLIC_JWK_FILE` → `agent-ca-public.jwk` | API `10001`, step-ca `1000`; `root:root 0400` plus `u:10001:r,u:1000:r` ACLs | Public provisioner JWK. |
| `STEP_CA_ROOT_CERTIFICATE_FILE` → `step-ca-root-certificate` | API `10001`, step-ca `1000`; `root:root 0400` plus `u:10001:r,u:1000:r` ACLs | PEM root certificate. |
| `STEP_CA_INTERMEDIATE_KEY_FILE`, `STEP_CA_PASSWORD_FILE` → `step-ca-intermediate-key`, `step-ca-password` | step-ca `1000:1000`, `1000:1000 0400` | Encrypted intermediate private key and its one password. |
| Development-only `AGENT_INTERMEDIATE_KEY_FILE` → `agent-intermediate-key` | API `10001:10001`, `10001:10001 0400` | Built-in CA intermediate private key; never combine this overlay with step-ca. |
| `TAILSCALE_OAUTH_CLIENT_ID_FILE`, `TAILSCALE_OAUTH_CLIENT_SECRET_FILE` → matching `tailscale-oauth-*` files | Tailscale startup, `root:root 0400` | One OAuth client ID or secret; neither is a GitHub credential. |
| `HERMES_API_KEY_FILE` → `hermes-api-key` | Hermes entrypoint then managed `1100:1100`; `root:root 0400` | One 32+ character key using only `A-Z`, `a-z`, `0-9`, `_`, `.`, `~`, or `-`. |

The offline root private key never enters this NAS. The generated PKI paths and
permissions in [agent PKI](../../docs/runbooks/agent-pki.md) remain authoritative
for the step-ca material.

## Pin the GPU node update authority

The host updater writes the selected release identity to
`/srv/vonk-forge/control-identity/active.json`. API, worker, and signer mount the
identity **directory** read-only and reopen that file for each validation. They
never mount `/srv/vonk-forge/control-host`, and no online container can select a
generation. Do not copy version or digest values into a mutable environment
file as an alternative authority.

Create one cluster update-signing key on the NAS. Only the networkless
`control-signer` receives the private key; `control-api`, `control-worker`,
Caddy, and the GPU nodes receive no signing
material:

```bash
sudo -u '#10003' openssl genpkey -algorithm ED25519 \
  -out /srv/vonk-forge/secrets/agent-update-authority-key.pem
sudo chown 10003:10001 /srv/vonk-forge/secrets/agent-update-authority-key.pem
sudo chmod 0400 /srv/vonk-forge/secrets/agent-update-authority-key.pem
```

During first selection, the trusted updater starts the selected networkless
signer and records its canonical public authority document in the generation
evidence. Export that bounded public document for GPU node installation; never run
a Compose service from the authoring checkout to derive it.

Provide that public document to every GPU node installation with
`install-vonk-agent --update-authority /path/to/update-authority.json` together
with the normal enrollment and TUF bootstrap inputs. One public authority is
valid for any number of GPU nodes in the cluster; receipts are still bound to one
node, operation, attempt, fence, deadline, and observed supervisor generation.
The signer verifies the requested agent artifact through its own persistent
python-tuf cache before signing. It also requires a short-lived action grant
signed by a separate API/admin authority and bound to the exact rollout, job,
node set, action, release, expiry, and nonce. The worker receives only the
signer's Unix socket and cannot read either private authority key, TUF bootstrap
root, or verifier cache.

Back up the private key as a high-value online recovery secret. Restoring the
same key preserves all existing GPU node pins. Replacing a lost or compromised key
requires explicitly reinstalling or reprovisioning each GPU node with the new
public document before the worker switches signers; there is deliberately no
unsigned remote key-rotation path. Expired, replayed, source-drifted, or retried
receipts require a newly approved and signed operation.

## Bootstrap the production step-ca overlay

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

The base file deliberately selects no CA provider. Select exactly one overlay:
`compose.step-ca.yaml` for production, or the built-in CA overlay for local
development—never both.

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
