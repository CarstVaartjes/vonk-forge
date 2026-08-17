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
enables the account's lingering user manager, and leaves the network agent
disabled. It performs no download, pairing, or network-service start. Package
removal disables lingering again. The service has no ambient capabilities;
its capability bounding set is
the tested minimum needed for the distribution's setuid
`newuidmap`/`newgidmap` helpers to create the delegated namespace:
`CAP_DAC_OVERRIDE`, `CAP_SETUID`, `CAP_SETGID`, and `CAP_SYS_ADMIN`. Existing
host-managed subordinate ranges are preserved and reused when they are large
enough. Package installation does not mutate Docker, containerd, NVIDIA CDI,
the driver, firmware, Netplan, or the Docker group. It installs but does not
activate the Docker-aware site firewall; the operator supplies node-specific
addresses and accepted host ports before the runtime helper can start.

Copy the CA:

```bash
sudo install -o root -g vonk-agent -m 0640 controller-ca.pem \
  /etc/vonk-forge-agent/controller-ca.pem
openssl x509 -in controller-ca.pem -outform DER | sha256sum
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

Registration generates the node-specific runtime inputs. The supported flow is
Fleet-backed and generated: the operator receives a generated bootstrap command
or protected bootstrap file from **Add Spark**, not a hand-edited local
configuration checklist. Manual `agent.toml` editing is unsupported.

The next Fleet bootstrap implementation contract is:

1. Fleet creates the node-bound bootstrap grant and registration intent.
2. Fleet emits the generated bootstrap command together with the exact
   non-secret runtime inputs: `enrollment_url`,
   `controller_url = "https://<CONTROLLER_HOSTNAME>:8443/"`,
   `ca_path = "/etc/vonk-forge-agent/controller-ca.pem"`,
   `ca_sha256`, the assigned `node_id`, and—when multi-node admission is in
   scope—the node's direct-fabric address plus
   `fabric_bandwidth_mbps = 200000`.
3. Running that generated bootstrap command writes
   `/etc/vonk-forge-agent/agent.toml`, stores the one-use token without
   exposing it in shell history, submits enrollment evidence, waits for
   approval, repeats collection when needed, removes the consumed token, and
   leaves the authenticated runtime ready for validation.

Until the bootstrap emitter lands, this section is the implementation
contract. The packaged placeholder `agent.toml` is a materialization target for
registration output, not an operator-authored document. The development origins
remain the exact roots `https://<ENROLLMENT_HOSTNAME>:8443/` and
`https://<CONTROLLER_HOSTNAME>:8443/`; do not rely on HTTPS port 443 defaults.
Keep `data_dir` at `/var/lib/vonk-forge-agent` unless a reviewed packaging
change says otherwise.

## Validate and start

Before enabling the privileged helper, install
`/etc/vonk-forge-agent/docker-firewall.conf` and enable
`vonk-forge-docker-firewall.service` with the exact site-policy procedure in
[Development agent workloads](../runbooks/development-agent-workloads.md#etchosts-and-firewall).
This ordering is mandatory: the helper has a systemd requirement on the
validated policy and fails closed when the file, Docker chain, or managed rules
are unavailable.

```bash
agent_uid="$(id -u vonk-agent)"
test "$(loginctl show-user vonk-agent -p Linger --value)" = yes
test -S "/run/user/${agent_uid}/bus"
sudo -u vonk-agent env \
  HOME=/var/lib/vonk-forge-agent \
  XDG_DATA_HOME=/var/lib/vonk-forge-agent \
  XDG_RUNTIME_DIR="/run/user/${agent_uid}" \
  DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/${agent_uid}/bus" \
  CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf \
  podman --cgroup-manager=systemd info
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

`podman info` must report the systemd cgroup manager without a fallback
warning. The user manager is build authority only: it does not run the Vonk
network agent, expose a socket, or gain Docker/sudo membership.

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
- Start-limit recovery: `Restart=on-failure` is deliberately bounded. If
  `systemctl status` reports `start-limit-hit`, first make the controller and
  its authenticated agent endpoint healthy, then inspect the node journal for
  the original failure. Once that cause is resolved, clear the failed counter
  exactly once and start the same installed slot:

  ```bash
  sudo systemctl status --no-pager --full vonk-forge-agent.service
  sudo journalctl -u vonk-forge-agent.service -n 100 --no-pager
  sudo systemctl reset-failed vonk-forge-agent.service
  sudo systemctl start vonk-forge-agent.service
  sudo systemctl is-active vonk-forge-agent.service
  ```

  Confirm the controller receives a fresh inventory from the same `node_id`.
  Do not loop `reset-failed`, re-pair, delete agent state, or switch A/B slots
  merely to clear the limit. If the unit reaches the limit again, stop and fix
  the still-present journaled cause.
- Removal: stop the running units first, then remove the package:

  ```bash
  sudo systemctl disable --now vonk-forge-agent-supervisor.service \
    vonk-forge-package-helper.socket vonk-forge-docker-firewall.service
  sudo apt remove vonk-forge-agent
  ```
