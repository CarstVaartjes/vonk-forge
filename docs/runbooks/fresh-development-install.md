# Fresh development installation

This is the shortest supported path from an empty NAS project and clean
Ubuntu 24.04 ARM64 GPU nodes to a working Vonk Forge development fleet. It
uses public `:dev` control images and the signed APT `dev` channel produced
from accepted `main` commits. Production uses the separate trusted host
updater and is not installed with this guide.

The finished NAS directory contains only `docker-compose.yml` and `secrets/`.
GPU nodes receive only the public controller CA and their own locally generated
identity. No GitHub, GHCR, R2, database, signing, or model credential is copied
to a GPU node or baked into an image.

NVIDIA Sync owns supported cluster networking and node-to-node SSH. DGX
Dashboard owns DGX OS, firmware, kernel, driver, Docker, and NVIDIA Toolkit
updates. This fresh-install path validates those platform prerequisites but
does not replace or reconfigure them. Do not run the archived SSH-controller
node policy, `disable-earlyoom`, runtime-release, or manual Netplan workflows
as part of a fresh install.

## 1. Record the site values

Choose these values once and use them consistently:

```text
NAS address:            <NAS_MANAGEMENT_IP>
GPU-node network:       <NODE_MANAGEMENT_CIDR>
Direct-fabric networks: <DIRECT_FABRIC_CIDRS_OR_NONE>
Enrollment name:        <ENROLLMENT_HOSTNAME>
Agent controller name:  <CONTROLLER_HOSTNAME>
Reserved registry name: <REGISTRY_HOSTNAME>
```

Each GPU node also needs a unique identity generated with:

```bash
printf 'spk_%s\n' "$(openssl rand -hex 16)"
```

Never reuse a node identity. The examples in this repository use
`enroll.vonk-forge.lan`, `agents.vonk-forge.lan`, and
`registry.vonk-forge.lan`, but those names are not product defaults.
For a multi-node fleet, record the canonical comma-separated CIDRs configured
by NVIDIA Sync, for example `192.168.100.0/24,192.168.101.0/24`. They must not
overlap the management network. For an intentionally single-node installation,
record the literal `none` and omit both fabric fields from the agent config.

## 2. Download the accepted Compose artifact

Open the successful **Development images** GitHub Actions run for the accepted
`main` commit and download
`vonk-forge-dev-compose-<40-character-commit>`. Keep both files from the
artifact:

- retain `docker-compose.dev.yml` as the publisher input; the publisher writes
  it to the NAS as `docker-compose.yml`;
- retain `docker-compose.pinned.yml` off the NAS for exact reproduction or
  guarded recovery.

Both GHCR packages must pull anonymously. Do not install a registry token on
the NAS. Normal updates pull/redeploy the unchanged `docker-compose.yml`;
restarting containers alone does not fetch a moved `:dev` tag.

## 3. Generate and publish the NAS project

Generate secrets on a private Linux filesystem, not directly on SMB:

```bash
set -euo pipefail
install -d -m 0700 '<LOCAL_STAGING_DIRECTORY>'
scripts/dev-runtime-secrets.py \
  --secrets-dir '<LOCAL_STAGING_DIRECTORY>/secrets' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
scripts/dev-runtime-project \
  --source-compose '<DOWNLOAD_DIRECTORY>/docker-compose.dev.yml' \
  --secrets-dir '<LOCAL_STAGING_DIRECTORY>/secrets' \
  --destination '<MOUNTED_NAS_PARENT>/vonk-forge' \
  --nas-address '<NAS_MANAGEMENT_IP>' \
  --management-cidrs '<NODE_MANAGEMENT_CIDR>' \
  --direct-fabric-cidrs '<DIRECT_FABRIC_CIDRS_OR_NONE>' \
  --enroll-hostname '<ENROLLMENT_HOSTNAME>' \
  --agent-hostname '<CONTROLLER_HOSTNAME>' \
  --registry-hostname '<REGISTRY_HOSTNAME>'
```

Back up all 17 local source files as one encrypted generation. The publisher
copies exactly 14 files to the NAS and deliberately excludes the controller CA
private key, public Git-signing key, and public host-runtime grant key. Do not display secret contents while
checking the result. The publisher takes a nonblocking Linux file lock on the
mounted share and rejects a concurrent invocation; it fails closed if locking
is unavailable. If an SMB write, mount, or workstation is interrupted, do not
edit the destination or delete the hidden `.vonk-forge-publish` recovery
journal or `.vonk-forge-publish.cleanup` tombstone. Remount the same share and
rerun the same command; under the exclusive lock, a stale rollback journal is
restored and a stale cleanup tombstone is safely discarded before publication
retries. A stable hidden lock file beside (never inside) the project coordinates
publishers across workstations and is safe to retain. A successful run removes
both hidden transaction states from the project and leaves exactly
`docker-compose.yml` plus `secrets/`.

An existing installation with the original valid 15-file local source can be
upgraded without rotating its CA, database password, or other authority: repeat
the generator command once with `--upgrade-host-runtime-authority`, then back
up the resulting 17-file generation and republish it. The add-only migration
refuses every other incomplete or unknown state.

## 4. Configure names and start the NAS stack

Add this line to `/etc/hosts` on the NAS and every GPU node:

```text
<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
```

Allow the GPU-node management CIDR to reach NAS TCP 8443 and reject other
sources. In the NAS Docker/Compose UI:

1. Import the `vonk-forge/` directory as a project.
2. Select `docker-compose.yml` and choose **Pull**, then **Redeploy**.
3. Keep all named volumes.
4. Wait for PostgreSQL, API, worker, Caddy, and LiteLLM to become healthy.

One-shot cohort, initializer, and migration containers should exit with status
zero. They are completed prerequisites, not failed services. See
[Development NAS installation](development-nas-installation.md) if startup
does not reach healthy state. Configure the guide's
[restricted operator loopback forwarding](development-nas-installation.md#restrict-operator-loopback-forwarding)
before the acceptance tunnel in step 7; do not expose ports 8080 or 4000 on the
LAN.

## 5. Install and configure each GPU node

On every Ubuntu 24.04 ARM64 node:

1. Complete the verified one-time
   [APT `dev` channel setup](../operations/agent-package-release.md#install-the-dev-channel).
2. Run the installation guide's NVIDIA Docker/CDI preflight, then
   `sudo apt update && sudo apt install vonk-forge-agent`.
3. Copy only `controller-ca` and `host-runtime-grant-public-key` from the
   private local source bundle to the node. Never copy either corresponding
   private key.
4. Install the CA at `/etc/vonk-forge-agent/controller-ca.pem`, owned by
   `root:vonk-agent` with mode `0640`.
5. Install the helper authority public key at
   `/etc/vonk-forge-agent/host-helper-authority.pub`, owned by `root:root` with
   mode `0644`.
6. Set the two explicit `:8443` HTTPS origins, CA path, independently computed
   DER SHA-256 fingerprint, unique node ID, and (for multi-node use) the
   node's direct-fabric address and measured 200000 Mb/s bandwidth in
   `/etc/vonk-forge-agent/agent.toml`.
7. Create the root-owned six-key `docker-firewall.conf`, then enable the signed
   `vonk-forge-docker-firewall.service` before the package-helper socket. It
   installs persistent Docker-aware `DOCKER-USER` policy for every accepted
   original published host port and the peer-only rendezvous flow. UFW `INPUT`
   policy does not protect Docker-published ports; use the exact
   original-destination/source contract from the workload runbook and run the
   packaged check after Docker restart and host reboot.

Use the exact commands and minimal configuration in
[Install the Vonk Forge agent](../operations/install-vonk-agent.md). Do not add
the agent account to Docker, sudo, or an NVIDIA administration group.

## 6. Pair and start one node at a time

For each node, complete this order without reusing its one-use token:

1. Create a pairing grant for that exact node ID in the administrator UI.
2. Save the token in a private root-readable file on that node.
3. Run the installation guide's exact
   `/var/lib/vonk-forge/supervisor/current/vonk-agent pair ... --token-stdin`
   command; the controller records a pending enrollment.
4. Compare the displayed node, CSR, host, hardware, agent, and boot evidence,
   then approve it.
5. Repeat the same pair command to collect the issued certificate and remove
   the token file.
6. Enable the package-helper socket and agent supervisor as documented in the
   agent installation guide.

The controller must show the same certificate-bound node ID, Rust protocol 3,
migration `complete`, and fresh inventory. Repeat for each additional node.

## 7. Prove the installation

Create a private admin token, forward the NAS loopback API and inference ports,
and run the deterministic synthetic lifecycle:

```bash
install -d -m 0700 .state/development-acceptance
scripts/dev-admin-token \
  --output .state/development-acceptance/admin-token \
  --signing-key-file '<LOCAL_STAGING_DIRECTORY>/secrets/token-signing-key' \
  --ttl-seconds 21600

# Keep this tunnel open in another terminal.
ssh -N \
  -L 18080:127.0.0.1:8080 \
  -L 14000:127.0.0.1:4000 \
  '<NAS_SSH_TARGET>'

scripts/run-development-slices \
  --api-base http://127.0.0.1:18080 \
  --inference-base http://127.0.0.1:14000 \
  --admin-token-file .state/development-acceptance/admin-token \
  --inference-token-file '<LOCAL_STAGING_DIRECTORY>/secrets/litellm-master-key' \
  --phase synthetic \
  --builder-node '<NODE_ID>' \
  --target-node '<NODE_ID>' \
  --evidence-file .state/development-acceptance/synthetic.json
```

Success proves source verification, isolated rootless image build, signed
Docker import/start, install, route publication, inference, stop, route
withdrawal, and uninstall.
For real single-node and multi-node model qualification, restart persistence,
and rank failure/recovery, continue with
[Development agent workload acceptance](development-agent-workloads.md).

## Normal updates

- NAS development stack: in the existing Docker UI project choose **Pull**,
  then **Redeploy**. Keep the Compose file, secrets, and named volumes.
- GPU nodes: after the accepted APT `dev` publication is complete, follow
  [Update and switch channels](../operations/agent-package-release.md#update-and-switch-channels).
  Apt stages the signed inactive slot; activate and prove one canary node,
  wait for fresh controller inventory, then repeat on the next node.
- Never substitute production `latest` tags or a local build. Production is
  selected only through its signed release and trusted host updater.
