# Install the Vonk Forge agent

Each GPU node runs one outbound-only Rust service. The NAS/controller never
opens SSH for routine work and the node exposes no Vonk listener. The agent
connects to the controller over mTLS, reports capacity and installed/running
recipes, then claims only operations matching its advertised capabilities.

## Prerequisites

- Ubuntu 24.04 ARM64 on the GPU node.
- NVIDIA's DGX OS, Docker Engine, NVIDIA driver, and NVIDIA Container Toolkit.
  Vonk does not install or reconfigure these platform-owned components.
- A route from the node to the NAS agent endpoint. The NAS may use its
  node-only management-LAN listener at TCP 8443; human and inference access
  remain Tailscale-only.
- The controller CA certificate and its independently verified SHA-256 digest.

Do not assume local DNS. Before pairing, add the management-LAN names to
`/etc/hosts` on the GPU node so the enrollment and post-identity controller
names resolve to the NAS management address:

```text
<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
```

Use the same hostnames on the NAS itself so local diagnostics, Caddy
certificates, and agent recovery checks all refer to the same names.
Allow `<NODE_MANAGEMENT_CIDR>` to `<NAS_MANAGEMENT_IP>:8443` in the NAS and host
firewalls, and reject all other sources to that port. Verify each name resolves
to `<NAS_MANAGEMENT_IP>` before pairing; `/etc/hosts` supplies names only, so
both agent URLs must retain the explicit `:8443` port.

Do not add the service user to `docker`, `sudo`, or an NVIDIA administration
group. Rootless Podman is used only to build an untrusted source bundle. A
controller-signed, narrowly typed root helper imports and starts the accepted
image through the Spark-managed Docker/NVIDIA runtime; the agent cannot open
the Docker socket. Raw InfiniBand and GPUDirect RDMA are not part of this
runtime contract. Vonk images must declare the policy's explicit numeric
non-root user and are started read-only with dropped capabilities, bounded
memory/PIDs/shared memory, no swap, and only declared mounts and ports.

Before installing Vonk, validate the platform exactly as NVIDIA documents:

```bash
test "$(uname -m)" = aarch64
. /etc/os-release && test "$ID:$VERSION_ID" = ubuntu:24.04
command -v docker nvidia-ctk nvidia-smi
nvidia-ctk cdi list | grep -Fx 'nvidia.com/gpu=all'
sudo docker run --rm --gpus all \
  nvcr.io/nvidia/cuda:13.0.1-devel-ubuntu24.04@sha256:7d2f6a8c2071d911524f95061a0db363e24d27aa51ec831fcccf9e76eb72bc92 \
  nvidia-smi -L
```

Use `sudo docker` for this administrator preflight; Docker-group membership is
not a prerequisite. Stop if any check fails and repair DGX OS through NVIDIA's
supported update/recovery path rather than adding a Vonk workaround.

## Install

Configuring one signed Vonk Forge apt channel is a one-time prerequisite on
every GPU node. For nodes following accepted `main` builds, install the archive
key and `dev` source with the complete verification block in
[Install the `dev` channel](agent-package-release.md#install-the-dev-channel).
Do not enable both `dev` and `stable` on the same node.

After that one-time repository setup, install the package normally:

```bash
sudo apt update
sudo apt install vonk-forge-agent
```

This first installation initializes signed slot A. Later apt upgrades stage the
inactive slot and deliberately keep the current agent active. Use the
[canary activation procedure](agent-package-release.md#update-and-switch-channels)
for every upgrade; do not treat `apt install --only-upgrade` as rollout
completion.

The maintainer script creates the unprivileged account, allocates the first
non-overlapping standard subordinate UID and GID ranges permitted by
`/etc/login.defs`, initializes rootless container storage and signed A/B slots,
and leaves network state disabled. It performs no download, pairing, or service
start. The service has no ambient capabilities; its capability bounding set is
the tested minimum needed for the distribution's setuid
`newuidmap`/`newgidmap` helpers to create the delegated namespace:
`CAP_DAC_OVERRIDE`, `CAP_SETUID`, `CAP_SETGID`, and `CAP_SYS_ADMIN`. Existing
host-managed subordinate ranges are preserved and reused when they are large
enough. Package installation does not mutate Docker, containerd, NVIDIA CDI,
the driver, firmware, Netplan, or the Docker group. It installs but does not
activate the Docker-aware site firewall; the operator supplies node-specific
addresses and accepted host ports before the runtime helper can start.

Copy the CA and edit the bootstrap configuration:

```bash
sudo install -o root -g vonk-agent -m 0640 controller-ca.pem \
  /etc/vonk-forge-agent/controller-ca.pem
openssl x509 -in controller-ca.pem -outform DER | sha256sum
sudoedit /etc/vonk-forge-agent/agent.toml
```

Install the matching public host-runtime grant key generated with the NAS
secret generation. This key is public but authority-sensitive; copy it from
the same verified generation as the controller secrets. Never copy
`host-runtime-grant-private-key` to a GPU node.

```bash
sudo install -o root -g root -m 0644 host-runtime-grant-public-key \
  /etc/vonk-forge-agent/host-helper-authority.pub
```

The DER SHA-256 fingerprint command prints one line in the form
`<64-lowercase-hex>  -`. Copy only the first field into `ca_sha256`; do not
paste the certificate, token, or any secret into notes or logs.

Set the HTTPS root origins and controller CA path explicitly:

```toml
enrollment_url = "https://<ENROLLMENT_HOSTNAME>:8443/"
controller_url = "https://<CONTROLLER_HOSTNAME>:8443/"
ca_path = "/etc/vonk-forge-agent/controller-ca.pem"
ca_sha256 = "<64_LOWERCASE_HEX_FROM_SHA256SUM>"
node_id = "<NODE_ID>"
# Required for multi-node admission; use one address from the common direct
# TCP fabric configured by NVIDIA Sync/Cluster Assistant, or a grandfathered
# existing site whose unchanged fabric has separate accepted evidence.
fabric_address = "<NODE_FABRIC_IP>"
fabric_bandwidth_mbps = 200000
```

`enrollment_url` is used only by `pair`; `controller_url` is used only after
certificate issuance by the authenticated service. The development values are
the exact roots `https://<ENROLLMENT_HOSTNAME>:8443/` and
`https://<CONTROLLER_HOSTNAME>:8443/`; do not rely on HTTPS port 443 defaults.
Keep `data_dir` at
`/var/lib/vonk-forge-agent` unless a reviewed packaging change says otherwise.
An upgraded, already-paired agent can continue to run with its preserved
legacy conffile, but an administrator must add `enrollment_url` before any
future pairing or recovery enrollment.

In the admin interface, create a one-use pairing grant for that node. The
ordering is strict: create the grant, run `pair`, approve the pending
enrollment, then run the exact same `pair` command again to collect the issued
certificate. Supply the one-use token through standard input so it never
appears in shell history:

```bash
sudo -u vonk-agent -- \
  /var/lib/vonk-forge/supervisor/current/vonk-agent pair \
  --enrollment https://<ENROLLMENT_HOSTNAME>:8443/ \
  --ca-sha256 <64_LOWERCASE_HEX_FROM_SHA256SUM> \
  --token-stdin < /run/secrets/vonk-enrollment-token
```

Approve the displayed enrollment in the admin interface, then repeat the exact
command once to collect the issued certificate. Delete the token file after
the second successful run. If the node loses its key, expires before renewal,
or the disk is replaced, do not copy another node's certificate: create a new
one-use grant and repeat this same grant/pair/approve/pair flow.

## Validate and start

Before enabling the privileged helper, install
`/etc/vonk-forge-agent/docker-firewall.conf` and enable
`vonk-forge-docker-firewall.service` with the exact site-policy procedure in
[Development agent workloads](../runbooks/development-agent-workloads.md#etchosts-and-firewall).
This ordering is mandatory: the helper has a systemd requirement on the
validated policy and fails closed when the file, Docker chain, or managed rules
are unavailable.

```bash
sudo -u vonk-agent env \
  HOME=/var/lib/vonk-forge-agent \
  XDG_DATA_HOME=/var/lib/vonk-forge-agent \
  XDG_RUNTIME_DIR=/run/vonk-forge-agent \
  CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf \
  podman info
sudo awk -F: '$1 == "vonk-agent" { total += $3 } END { exit !(total >= 65536) }' \
  /etc/subuid
sudo awk -F: '$1 == "vonk-agent" { total += $3 } END { exit !(total >= 65536) }' \
  /etc/subgid
sudo systemctl enable --now vonk-forge-docker-firewall.service
sudo systemctl is-active vonk-forge-docker-firewall.service
sudo systemctl enable --now vonk-forge-package-helper.socket
sudo systemctl enable --now vonk-forge-agent-supervisor.service
sudo systemctl status vonk-forge-agent.service vonk-forge-agent-supervisor.service
sudo systemctl restart vonk-forge-agent-supervisor.service
sudo systemctl status vonk-forge-agent-supervisor.service
```

The controller must show `Rust agent`, migration `complete`, protocol 3, the
signed runtime identity, fresh inventory, `build.rootless-podman.v1`, and
`runtime.spark-docker-nvidia.v1`. The latter is reported only when the Docker
client and NVIDIA CDI `nvidia.com/gpu=all` checks pass.
Install/start admission remains controller-side: disk is checked before image
and weight installation, RAM/VRAM and current workloads before start, and all
participants/fabric links before a multi-node start.

## Boundary checks

```bash
sudo -u vonk-agent podman ps
sudo -u vonk-agent test ! -r /run/docker.sock
sudo test -S /run/docker.sock
sudo ss -lntup
systemd-analyze security vonk-forge-agent.service
```

The agent must have no listening TCP socket. `podman ps` validates the isolated
build store; it is not the accepted workload runtime. The first socket check
must succeed because the service identity cannot read Docker, while the root
check proves the NVIDIA-managed daemon socket exists. Workload images are
transferred by immutable digest, imported by the signed helper, and run through
Docker with NVIDIA CDI only when the recipe requests a GPU.

Before accepting a recipe start, configure a persistent Docker-aware host
firewall as described in
[Development agent workloads](../runbooks/development-agent-workloads.md#etchosts-and-firewall).
Published Docker ports bypass ordinary UFW `INPUT` policy. Keep NVIDIA's Docker
firewall integration enabled and enforce source/original-destination rules in
`DOCKER-USER`; an inactive or UFW-only policy is a start blocker. The current
managed boundary is IPv4-only, and the privileged helper rejects IPv6 workload
publications.

## Rotation, recovery, and removal

- Controller CA rotation: install the replacement certificate at
  `/etc/vonk-forge-agent/controller-ca.pem`, recompute the DER SHA-256
  fingerprint with the exact `openssl x509 ... -outform DER | sha256sum`
  command above, update `ca_sha256`, then restart the supervisor and confirm
  the controller still reports the same `node_id`.
- Identity-loss recovery: for expiry, key loss, or storage replacement, create
  a fresh one-use grant and repeat the original grant/pair/approve/pair
  sequence. Recovery is always a new local key plus a new certificate.
- Removal: stop the running units first, then remove the package:

  ```bash
  sudo systemctl disable --now vonk-forge-agent-supervisor.service \
    vonk-forge-package-helper.socket vonk-forge-docker-firewall.service
  sudo apt remove vonk-forge-agent
  ```
