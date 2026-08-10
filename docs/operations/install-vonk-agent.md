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
sudoedit /etc/vonk-forge-agent/agent.toml
```

Set both HTTPS root origins explicitly:

```toml
enrollment_url = "https://enroll.example.internal/"
controller_url = "https://agents.example.internal/"
```

`enrollment_url` is used only by `pair`; `controller_url` is used only after
certificate issuance by the authenticated service. Also set `ca_sha256` and
the controller-created `node_id`; keep `data_dir` at
`/var/lib/vonk-forge-agent`. An upgraded, already-paired agent can continue to
run with its preserved legacy conffile, but an administrator must add
`enrollment_url` before any future pairing or recovery enrollment.

In the admin interface, create a new-node pairing grant for that node. Supply
the one-use token through standard input so it never appears in shell history:

```bash
sudo -u vonk-agent -- \
  /var/lib/vonk-forge/supervisor/current/vonk-agent pair \
  --enrollment https://enroll.example.internal/ \
  --ca-sha256 REPLACE_WITH_64_LOWERCASE_HEX \
  --token-stdin < /run/secrets/vonk-enrollment-token
```

Approve the displayed enrollment in the admin interface, then repeat the
exact command once to collect the issued certificate. Delete the token file.

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
