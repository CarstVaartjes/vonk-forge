# Direct ConnectX-7 fabric

The local `scripts/validate_fabric.py` SSH boundary uses the same
developer-machine transport selection as `vonkctl`: `ssh` on macOS and native
Linux, `ssh.exe` on WSL when available, and `VONK_SSH_BIN` as an explicit
override. This applies only to local-to-GPU node commands. The pinned
GPU node-1-to-GPU node-2 fabric SSH command remains a nested remote command and is
not replaced by the developer-machine selector.

This runbook configures a two-Vonk Forge GPU node, directly cabled ConnectX-7 fabric.
The selected path is the official NVIDIA DGX Spark CLI/manual fallback from
NVIDIA `dgx-spark-playbooks` commit `1fb66f059ee427c5a3678b3117ef73aab042b458`.
It deliberately does not use the Mac administration key for node-to-node
access and does not enable SSH agent forwarding. The direct fabric must never
receive a default route.

For a fresh DGX Spark cluster, prefer NVIDIA Sync's Cluster Assistant and
retain its generated validation report. The manual Netplan procedure below is
the accepted fallback for this existing installation; do not layer it on top
of Sync-managed Netplan. Regardless of how addresses are configured, Vonk's
current container workload contract uses TCP over one declared direct-fabric
address per node. The host-level RDMA/NCCL measurements in this runbook prove
the physical platform, but Vonk does not pass raw InfiniBand devices into
workload containers and does not claim GPUDirect RDMA.

`inventory/reports/fabric.json` is committed as explicitly-labelled
preconfiguration/staging evidence. Do not populate `inventory/cluster.toml` or
replace its null post-configuration values until the probes below have been
captured.

## Evidence and safety gate

Before applying the manual plan, retain all of the following:

- a photo of the label on the *single* QSFP112 DAC cable, including its part
  number,
- a rear-panel photo proving that one cable joins the same numbered ConnectX-7
  QSFP port on both GPU nodes, and
- the elevated `ethtool -m` output captured from both ends.

On the current hosts, one physical QSFP connection is exposed as two Linux
interfaces and RDMA HCAs. This is expected on Vonk Forge GPU node; do **not** treat it as
two cables or select only one function:

| Node | Linux interfaces reported `LOWER_UP` | RDMA HCAs | Observed physical-link state |
| --- | --- | --- | --- |
| GPU node 1 | `enp1s0f1np1`, `enP2p1s0f1np1` | `rocep1s0f1`, `roceP2p1s0f1` | both functions report 200000 Mb/s |
| GPU node 2 | `enp1s0f1np1`, `enP2p1s0f1np1` | `rocep1s0f1`, `roceP2p1s0f1` | both functions report 200000 Mb/s |

Those duplicate reports describe one 200 Gb/s physical QSFP link. They must
not be added to claim 400 Gb/s. NVIDIA documents the two functions as the NIC's
two PCIe Gen5 x4 paths into the SoC, while NVIDIA Sync defines 184 Gb/s as the
lower accepted speed-test result for this 200 Gb/s connection.

The controller's elevated, read-only `ethtool -m` evidence identifies the
installed cable at both ends as `Amphenol`, OUI `78:a7:14`, vendor PN
`NJAAKK-C106`, revision `B`, serial `APF261610697AC`: a 1 m passive copper,
PAM4 DAC. It agrees at both ends. Amphenol's primary material identifies the
`NJAAKK` family as QSFP 400G, 112G/lane passive DAC, but NVIDIA's current Sync
guide lists `NJAAKK-N911` (not `NJAAKK-C106`) and `Luxshare LMTQF022-SD-R` as
the supported models. Treat `C106` as an undocumented OEM/customer identifier,
not as confirmed supported hardware.

If the evidence must be recaptured, the controller may run this read-only,
elevated probe and attach its output to the change record:

```bash
for host in vonk-node-1 vonk-node-2; do
  ssh -o BatchMode=yes -o ForwardAgent=no "$host" \
    'sudo ethtool -m enp1s0f1np1; sudo ethtool -m enP2p1s0f1np1'
done
```

The cable PN remains an undocumented OEM/customer identifier. The selected
manual path may proceed only through the staged preflight below; it requires
both functions to be `UP` at 200000 Mb/s and preserves the management default
route. Any cable or link warning, failed preflight, Netplan error, route
change, or failed postcheck is a hard stop. Do not apply a manual workaround.

## Historical helper constraint for this manual rollout

NVIDIA Sync/Cluster Assistant was excluded from this already-completed manual
rollout. The reviewed helper revision at the time copied
`~/.ssh/id_ed25519_shared` private material to every node and appended a
`Host * IdentityFile` rule, which did not meet this site's key separation.
That historical finding is not a blanket ban on current Sync releases: for a
fresh site, review the current generated SSH and Netplan changes, use Cluster
Assistant when they meet policy, and choose the manual fallback only when they
do not.

## Selected manual CLI rollout

The official two-GPU node playbook assigns the active `f1` function pair to two
point-to-point subnets. The staged plan uses the current Linux MTU/default,
`1500`; no jumbo-MTU value is assumed without primary evidence or a successful
live validation.

| Node | Interface | HCA | Planned IPv4 | MTU |
| --- | --- | --- | --- | --- |
| GPU node 1/head | `enp1s0f1np1` | `rocep1s0f1` | `192.168.100.10/24` | 1500 |
| GPU node 1/head | `enP2p1s0f1np1` | `roceP2p1s0f1` | `192.168.101.10/24` | 1500 |
| GPU node 2/worker | `enp1s0f1np1` | `rocep1s0f1` | `192.168.100.11/24` | 1500 |
| GPU node 2/worker | `enP2p1s0f1np1` | `roceP2p1s0f1` | `192.168.101.11/24` | 1500 |

`nodes/bin/configure-direct-fabric` is an audited, idempotent installer. It
only manages `/etc/netplan/99-vonk-node-direct-fabric.yaml`, refuses to mix
with `99-nvidia-sync-cluster.yaml`, requires both functions to be up at
200000 Mb/s, sets `dhcp4: false` on both fabric interfaces, verifies that the
management default route is not on the fabric, and uses `netplan try` for both
installation and rollback. It has no SSH or private-key handling.

### Stage and inspect the worker first

The following first two commands only stage the reviewed script and run its
read-only preflight. They do not change either GPU node. Do not add `-A` or enable
agent forwarding.

```bash
scp -o ForwardAgent=no nodes/bin/configure-direct-fabric \
  vonk-node-2:/tmp/configure-direct-fabric
ssh -o BatchMode=yes -o ForwardAgent=no vonk-node-2 \
  'bash /tmp/configure-direct-fabric --node node2 --check'
```

If—and only if—the preflight prints a pass result with a Wi-Fi/10 GbE
management default route, the controller/user can make the first approved
change on the worker:

```bash
ssh -t -o BatchMode=yes -o ForwardAgent=no vonk-node-2 \
  'sudo bash /tmp/configure-direct-fabric --node node2 --apply'
```

Review `netplan try` at the console and accept only if management remains
reachable. If it is not accepted, it automatically rolls back. Do not continue
to GPU node 1 if worker application, route preservation, or local validation
fails. Before staging the head, the worker must prove its own addresses, MTU,
RoCEv2 GID-to-netdev binding, and absence of a fabric default route:

```bash
ssh -o BatchMode=yes -o ForwardAgent=no vonk-node-2 \
  'sudo bash /tmp/configure-direct-fabric --node node2 --local-postcheck'
```

After the worker result is recorded, repeat the same staged preflight and
interactive application for the head:

```bash
scp -o ForwardAgent=no nodes/bin/configure-direct-fabric \
  vonk-node-1:/tmp/configure-direct-fabric
ssh -o BatchMode=yes -o ForwardAgent=no vonk-node-1 \
  'bash /tmp/configure-direct-fabric --node node1 --check'
ssh -t -o BatchMode=yes -o ForwardAgent=no vonk-node-1 \
  'sudo bash /tmp/configure-direct-fabric --node node1 --apply'
```

### Separate head-to-worker cluster key

Only after both Netplan applications and both postchecks pass, generate the
Ed25519 key on GPU node 1. The private key never leaves GPU node 1; only its public
key is transferred through the controller. This workflow does not copy the Mac
administration key and does not forward an agent.

```bash
ssh -o BatchMode=yes -o ForwardAgent=no vonk-node-1 '
  set -euo pipefail
  key="$HOME/.ssh/vonk_node_fabric_ed25519"
  test ! -e "$key" && test ! -e "$key.pub"
  umask 077
  ssh-keygen -q -t ed25519 -N "" -f "$key" \
    -C "node1-to-node2-fabric"
  cat "$key.pub"
' > /tmp/vonk_node_fabric_ed25519.pub
```

Install the public key on GPU node 2 with the two GPU node 1 fabric addresses as
source restriction and OpenSSH's `restrict` option. `restrict` denies agent,
port, X11, and PTY forwarding while still permitting the noninteractive SSH
processes required for cluster work.

```bash
{
  printf 'restrict,from="192.168.100.10,192.168.101.10" '
  cat /tmp/vonk_node_fabric_ed25519.pub
} | ssh -o BatchMode=yes -o ForwardAgent=no vonk-node-2 '
  set -euo pipefail
  umask 077
  install -d -m 0700 "$HOME/.ssh"
  cat >> "$HOME/.ssh/authorized_keys"
  chmod 0600 "$HOME/.ssh/authorized_keys"
'
```

On GPU node 1, create a narrow fabric-only alias (the user and home path must
match the live account):

```sshconfig
Host vonk-node-2-fabric
    HostName 192.168.100.11
    User carst
    BindAddress 192.168.100.10
    IdentityFile ~/.ssh/vonk_node_fabric_ed25519
    IdentitiesOnly yes
    ForwardAgent no
```

Verify from GPU node 1 with `ssh -o ForwardAgent=no vonk-node-2-fabric hostname`.
Do not add `Host *`, do not add the fabric key to an agent, and do not copy its
private component to GPU node 2 or the Mac.

### Manual rollback

Run the single reviewed controller sequence from the repository root:

```bash
nodes/bin/rollback-direct-fabric
```

It runs with `set -euo pipefail`, derives the checksum in its own scope,
re-stages `configure-direct-fabric` with `scp -o ForwardAgent=no`, compares
each remote `sha256sum`, and transfers no key material. GPU node 2 is a hard
gate: a failed transfer, checksum, worker rollback, or management reconnect
exits before GPU node 1 is staged or touched. Only after the worker reconnects
over the management alias does it stage, verify, roll back, and reconnect to
GPU node 1.

The rollback retains the managed Netplan file under
`/root/vonk-node-fabric-rollback/` and uses `netplan try`; it does not remove
the head-only SSH key, which must be removed separately only if the cluster
relationship is intentionally dismantled. After a deliberately completed
rollback, verify both nodes over management and remove the two temporary
`/tmp/configure-direct-fabric` copies.

## Post-success collection and acceptance

### Verified live result

The following runtime state was captured read-only at `2026-08-01T22:34:12Z`.
GPU node 2 was applied and locally validated before GPU node 1. Both nodes retain the
management default route through `wlP9s9`; neither fabric interface has a
default route.

| Node | Function label | Interface | Fabric IPv4 | HCA | RoCEv2 GID | MTU | Physical-link state |
| --- | --- | --- | --- | --- | ---: | ---: | ---: |
| GPU node 1/head | 100 | `enp1s0f1np1` | `192.168.100.10/24` | `rocep1s0f1` | 3 | 1500 | 200000 Mb/s |
| GPU node 1/head | 101 | `enP2p1s0f1np1` | `192.168.101.10/24` | `roceP2p1s0f1` | 3 | 1500 | 200000 Mb/s |
| GPU node 2/worker | 100 | `enp1s0f1np1` | `192.168.100.11/24` | `rocep1s0f1` | 3 | 1500 | 200000 Mb/s |
| GPU node 2/worker | 101 | `enP2p1s0f1np1` | `192.168.101.11/24` | `roceP2p1s0f1` | 3 | 1500 | 200000 Mb/s |

Each recorded GID is IPv4-mapped, type `RoCE v2`, and has `gid_attrs/ndevs`
bound to the interface in the table. At `2026-08-01T22:34:27Z`, normal and
non-fragmenting `-M do -s 1472` pings succeeded 3/3 with zero loss in both
directions on both functions.

Use these exact values for distributed consumers on both nodes:

```bash
export NCCL_SOCKET_IFNAME='=enp1s0f1np1,enP2p1s0f1np1'
export NCCL_IB_HCA='=rocep1s0f1:1,roceP2p1s0f1:1'
export NCCL_IB_GID_INDEX=3
export TP_SOCKET_IFNAME='enp1s0f1np1,enP2p1s0f1np1'
export GLOO_SOCKET_IFNAME='enp1s0f1np1,enP2p1s0f1np1'
```

The head-only fabric key fingerprint is
`SHA256:xAsqCZnOIq34EVQR2O5+z+qaLlXFIdT7Qp9wreg4rfg`. The worker entry uses
`restrict,from="192.168.100.10,192.168.101.10"`; the private key is not on the
worker or Mac. `vonk-node-2-fabric` binds `192.168.100.10`, disables password,
keyboard-interactive, and agent forwarding, uses strict host checking, and
returned `node-2297`. The verified worker host Ed25519 fingerprint is
`SHA256:Q/0cf26vxC6Z+xH6pfB5uoGNXfIEum6KOFVhnl4nngg`.

The controller must capture the following output from both nodes after the
manual configuration completes. It is read-only except for the already-approved
manual operation.

```bash
for host in vonk-node-1 vonk-node-2; do
  ssh -o BatchMode=yes -o ForwardAgent=no "$host" '
    set -euo pipefail
    sudo cat /etc/netplan/99-vonk-node-direct-fabric.yaml
    ip -br link
    ip -br addr
    ip route
    rdma link show
    for d in /sys/class/infiniband/*; do
      for p in "$d"/ports/*; do
        [ -d "$p" ] || continue
        for g in "$p"/gids/[0-9]*; do
          [ -e "$g" ] || continue
          i=${g##*/}
          printf "%s/%s gid[%s]=%s type=%s netdev=%s\\n" \
            "${d##*/}" "${p##*/}" "$i" "$(cat "$g")" \
            "$(cat "$p/gid_attrs/types/$i" 2>/dev/null || true)" \
            "$(cat "$p/gid_attrs/ndevs/$i" 2>/dev/null || true)"
        done
      done
    done
  '
done
```

Run the script's exact bidirectional check from each node after both plans are
accepted; it validates route selection, no fabric default route, IPv4-to-RoCEv2
GID mapping, normal ping, and a non-fragmenting MTU-sized ping.

```bash
ssh -o BatchMode=yes -o ForwardAgent=no vonk-node-2 \
  'sudo bash /tmp/configure-direct-fabric --node node2 --postcheck'
ssh -o BatchMode=yes -o ForwardAgent=no vonk-node-1 \
  'sudo bash /tmp/configure-direct-fabric --node node1 --postcheck'
```

For every configured interface pair, verify the address listed in Netplan maps
to a non-link-local `RoCE v2` GID for that interface/HCA. Record **both**
interface/HCA/GID combinations: a physical Vonk Forge GPU node QSFP link has two
functions, so a single `fabric_ip`, `interface`, `hca`, and `gid_index` field
is insufficient until the inventory model is extended or explicitly represents
both consumers.

Run this from each node for each corresponding peer address and interface,
substituting only values captured above. The IPv4 non-fragmenting payload is
the MTU minus the 20-byte IPv4 and 8-byte ICMP headers.

```bash
iface='<configured-interface>'
peer='<corresponding-peer-fabric-ip>'
mtu="$(cat "/sys/class/net/$iface/mtu")"
ping -I "$iface" -c 3 "$peer"
ping -I "$iface" -M do -s "$((mtu - 28))" -c 3 "$peer"
test -z "$(ip route show default dev "$iface")"
```

The verified values above are recorded in `inventory/cluster.toml` and
`inventory/reports/fabric.json`. Do not replace them using the management LAN
or link-local GIDs.

## RDMA, latency, error-counter, and NCCL acceptance

Run `scripts/validate-fabric --inventory inventory/cluster.toml --output
inventory/reports/rdma-nccl.json` from the controller only after the recorded
postchecks pass. The wrapper is fail-closed: it validates the exact 200000 Mb/s
physical-link state, net-device MTU 1500, both recorded HCA/GID/netdev
consumers, and the RoCE path MTU 1024. It uses `-x 3` with `ib_write_bw`,
`ib_read_bw`, and `ib_write_lat`; runs both write functions simultaneously to
measure the one physical link; captures named RDMA error counters before and
after traffic; and uses the GPU node 1 `vonk-node-2-fabric` alias for every worker
operation. It stops on a failed command, wrong transport/path, a result below
its floor, any monitored counter growth, NCCL `NET/Socket`, or NCCL selecting
only one active HCA. It never enables agent forwarding.

The documented source installation follows NVIDIA `dgx-spark-playbooks` commit
`1fb66f059ee427c5a3678b3117ef73aab042b458`. Each GPU node has OpenMPI packages
`libopenmpi-dev` and `openmpi-bin` at `4.1.6-7ubuntu2`, CUDA 13 nvcc, NCCL
`v2.30.7-1` commit `73cf112295c33aee2b895f329f592f2a9b4b0f97`, and nccl-tests
commit `a0b82b2260cf5152b9f8c061bbf7eaf0ba096432`. Build them normally as the
host user with `/usr/local/cuda/bin/nvcc`,
`NVCC_GENCODE='-gencode=arch=compute_121,code=sm_121'`, and `MPI=1`.
`validate-fabric` does not build, clean, lock, or otherwise stage those source
trees; it verifies the completed pinned artifacts before testing the fabric.

Before a source tree is created, run the non-mutating worker-first gate:

```bash
scripts/validate-fabric --inventory inventory/cluster.toml \
  --output /tmp/rdma-nccl-preflight.json --nccl-preflight-only
```

It verifies both OpenMPI packages at `4.1.6-7ubuntu2`, CUDA 13 nvcc, exact
NCCL/nccl-tests source commits, `libnccl.so`, and MPI-enabled
`all_reduce_perf`, worker-first. The real two-rank `all_reduce_perf` runs
from GPU node 1 with `localhost` plus the documented `vonk-node-2-fabric` alias;
it passes the recorded `NCCL_SOCKET_IFNAME`, `NCCL_IB_HCA`, and GID index 3,
uses `NCCL_DEBUG=INFO`, forces OpenMPI's TCP control paths onto the two fabric
interfaces, and keeps `BatchMode`, `ForwardAgent=no`, and strict host-key
checking. These strict SSH controls are deliberate deviations from helper
scripts that weaken host-key checking or distribute keys. It never uses a
management-plane host list, `sudo -S`, a shared private key,
`StrictHostKeyChecking=no`, Docker, or the docker group.

The accepted run captured at `2026-08-02T10:46:10Z` produced:

| Gate | GPU node 1 to GPU node 2 | GPU node 2 to GPU node 1 | Floor |
| --- | ---: | ---: | ---: |
| Simultaneous two-function RDMA write aggregate | 185.14 Gb/s | 185.14 Gb/s | 184.00 Gb/s |
| Sequential function write range | 108.88–109.00 Gb/s | 108.99–109.02 Gb/s | 98.01 Gb/s each |
| Sequential function read range | 80.42–80.43 Gb/s | 80.43 Gb/s | 72.37 Gb/s each |

The aggregate components each measured 92.57 Gb/s while sharing the same
physical link. Their controller-observed client-call intervals overlapped for
6.90 and 7.23 seconds respectively; only same-interval results were summed.
This is the physical-link proof. Sequential ~109 Gb/s function results remain
diagnostics and are never added together as capacity evidence.

The fixed `ib_write_lat` baseline uses 8-byte messages, 10,000 iterations,
GID index 3, and RoCE path MTU 1024:

| Function / direction | Average | p99 | p99.9 | Maximum |
| --- | ---: | ---: | ---: | ---: |
| 100, GPU node 1 to GPU node 2 | 1.97 us | 2.05 us | 2.52 us | 4.33 us |
| 100, GPU node 2 to GPU node 1 | 1.98 us | 2.03 us | 2.38 us | 4.93 us |
| 101, GPU node 1 to GPU node 2 | 1.80 us | 2.20 us | 2.34 us | 3.82 us |
| 101, GPU node 2 to GPU node 1 | 1.82 us | 2.22 us | 2.30 us | 3.62 us |

All monitored RDMA sequence, retry, timeout, CQE, access, and adaptive-
retransmission counters were zero before and after the run. The accepted
two-node NCCL run selected both `rocep1s0f1:1` and `roceP2p1s0f1:1` through
`NET/IB : Using`, reported 19.308 GB/s average bus bandwidth against the
17.44 GB/s regression floor, and had zero out-of-bounds values. GPU Direct
RDMA-disabled diagnostics were observed but do not invalidate the selected
NET/IB transport. Do not force undocumented `NCCL_NET_GDR_LEVEL` settings.

Primary references: [NVIDIA DGX Spark clustering](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
for the physical topology and [NVIDIA Sync Cluster Assistant](https://docs.nvidia.com/sync/latest/cluster-assistant.html)
for the 200 Gb/s negotiated state and 184 Gb/s lower speed-test bound.
