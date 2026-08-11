# Install the Vonk Forge agent

Each GPU node runs one outbound-only Rust service. The NAS/controller never
opens SSH for routine work and the node exposes no Vonk listener. The agent
connects to the controller over mTLS, reports capacity and installed/running
recipes, then claims only operations matching its advertised capabilities.

## Prerequisites

- Ubuntu 24.04 ARM64 on the GPU node.
- NVIDIA driver and NVIDIA Container Toolkit with a working CDI device list:
  `nvidia-ctk cdi list` must include `nvidia.com/gpu=all`.
- A route from the node to the NAS agent endpoint. The NAS may use its
  node-only management-LAN listener; human and inference access remain
  Tailscale-only.
- The controller CA certificate and its independently verified SHA-256 digest.

Do not assume local DNS. Before pairing, add the management-LAN names to
`/etc/hosts` on the GPU node so the enrollment and post-identity controller
names resolve to the NAS management address:

```text
<NAS_MANAGEMENT_IP> <ENROLLMENT_HOSTNAME> <CONTROLLER_HOSTNAME> <REGISTRY_HOSTNAME>
```

Use the same hostnames on the NAS itself so local diagnostics, Caddy
certificates, and agent recovery checks all refer to the same names.

Do not add the service user to `docker`, `sudo`, or an NVIDIA administration
group. The package runs rootless Podman in a single-UID namespace with
`fuse-overlayfs`, `slirp4netns`, NVIDIA CDI devices, and an allow-listed
InfiniBand device class for multi-node recipes. Vonk images must run as root
inside that namespace; this maps only to the unprivileged `vonk-agent` account
on the host. Images declaring another OCI user are rejected before install.

## Install

Configuring one signed Vonk Forge apt channel is a one-time prerequisite on
every GPU node. For nodes following accepted `main` builds, install the archive
key and `dev` source with the complete verification block in
[Install the `dev` channel](agent-package-release.md#install-the-dev-channel).
Do not enable both `dev` and `stable` on the same node.

After that one-time repository setup, install or upgrade the package normally:

```bash
sudo apt update
sudo apt install vonk-forge-agent
```

The maintainer script creates the unprivileged account, single-UID rootless
container storage, signed A/B slots, and disabled network state. It performs no
download, pairing, or service start. The single-UID boundary deliberately keeps
`NoNewPrivileges=yes`; no setuid `newuidmap`/`newgidmap` helper is available to
the long-running agent. Because `vonk-agent` is a package-dedicated account,
installation removes only that account's `/etc/subuid` and `/etc/subgid`
mappings if an earlier prerelease or host policy created them.

Copy the CA and edit the bootstrap configuration:

```bash
sudo install -o root -g vonk-agent -m 0640 controller-ca.pem \
  /etc/vonk-forge-agent/controller-ca.pem
openssl x509 -in controller-ca.pem -outform DER | sha256sum
sudoedit /etc/vonk-forge-agent/agent.toml
```

The DER SHA-256 fingerprint command prints one line in the form
`<64-lowercase-hex>  -`. Copy only the first field into `ca_sha256`; do not
paste the certificate, token, or any secret into notes or logs.

Set the HTTPS root origins and controller CA path explicitly:

```toml
enrollment_url = "https://<ENROLLMENT_HOSTNAME>/"
controller_url = "https://<CONTROLLER_HOSTNAME>/"
ca_path = "/etc/vonk-forge-agent/controller-ca.pem"
ca_sha256 = "<64_LOWERCASE_HEX_FROM_SHA256SUM>"
node_id = "<NODE_ID>"
```

`enrollment_url` is used only by `pair`; `controller_url` is used only after
certificate issuance by the authenticated service. Keep `data_dir` at
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
  --enrollment https://<ENROLLMENT_HOSTNAME>/ \
  --ca-sha256 <64_LOWERCASE_HEX_FROM_SHA256SUM> \
  --token-stdin < /run/secrets/vonk-enrollment-token
```

Approve the displayed enrollment in the admin interface, then repeat the exact
command once to collect the issued certificate. Delete the token file after
the second successful run. If the node loses its key, expires before renewal,
or the disk is replaced, do not copy another node's certificate: create a new
one-use grant and repeat this same grant/pair/approve/pair flow.

## Validate and start

```bash
sudo -u vonk-agent env \
  HOME=/var/lib/vonk-forge-agent \
  XDG_DATA_HOME=/var/lib/vonk-forge-agent \
  XDG_RUNTIME_DIR=/run/vonk-forge-agent \
  CONTAINERS_STORAGE_CONF=/etc/vonk-forge-agent/containers-storage.conf \
  podman info
sudo systemctl enable --now vonk-forge-package-helper.socket
sudo systemctl enable --now vonk-forge-agent-supervisor.service
sudo systemctl status vonk-forge-agent.service vonk-forge-agent-supervisor.service
sudo systemctl restart vonk-forge-agent-supervisor.service
sudo systemctl status vonk-forge-agent-supervisor.service
```

The controller must show `Rust agent`, migration `complete`, protocol 3, the
signed runtime identity, inventory, and only the four recipe capabilities.
Install/start admission remains controller-side: disk is checked before image
and weight installation, RAM/VRAM and current workloads before start, and all
participants/fabric links before a multi-node start.

## Boundary checks

```bash
sudo -u vonk-agent podman ps
sudo ss -lntup
systemd-analyze security vonk-forge-agent.service
```

The agent must have no listening TCP socket. `podman ps` must work without a
Docker socket. A recipe container receives only declared mounts, limits,
network mode, and NVIDIA CDI devices; the image is always pulled and inspected
by immutable digest.

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
    vonk-forge-package-helper.socket
  sudo apt remove vonk-forge-agent
  ```
