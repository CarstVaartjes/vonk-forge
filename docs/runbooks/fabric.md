# Direct ConnectX-7 fabric

The local `scripts/validate_fabric.py` SSH boundary uses the same
developer-machine transport selection as `vonkctl`: `ssh` on macOS and native
Linux, `ssh.exe` on WSL when available, and `VONK_SSH_BIN` as an explicit
override. This applies only to local-to-GPU node commands. The pinned
GPU node-1-to-GPU node-2 fabric SSH command remains a nested remote command and is
not replaced by the developer-machine selector.

This runbook records a two-Vonk Forge GPU node, directly cabled ConnectX-7
fabric. The existing site was configured before the current NVIDIA Sync
workflow, using the official NVIDIA DGX Spark CLI/manual fallback from NVIDIA
`dgx-spark-playbooks` commit `1fb66f059ee427c5a3678b3117ef73aab042b458`.
The direct fabric must never receive a default route.

NVIDIA Sync owns fresh fabric configuration and node-to-node SSH. Use its
Cluster Assistant, review its proposed changes, and retain its generated
validation report. The historical Netplan and SSH commands have been removed
from the operational path so they cannot be mistaken for fresh-install
instructions. Regardless of how addresses are configured, Vonk's
current container workload contract uses one declared direct-fabric address
per node. Bridge-mode workloads use address-bound TCP publication. The narrow
connected-multinode host mode passes `/dev/infiniband` and permits peer-only
TCP/UDP on that selected interface/address for native NCCL/RoCE; it does not
grant arbitrary devices or claim GPUDirect RDMA.

`inventory/reports/fabric.json` is committed as explicitly-labelled
preconfiguration/staging evidence. Do not populate `inventory/cluster.toml` or
replace its null post-configuration values until the probes below have been
captured.

## Evidence and safety gate

The historical change record retained all of the following:

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

The cable PN remains an undocumented OEM/customer identifier. Any cable or
link warning, failed NVIDIA Sync validation, route change, or failed postcheck
is a hard stop. Do not apply a manual workaround.

## Historical helper constraint for this manual rollout

NVIDIA Sync/Cluster Assistant was excluded from this already-completed manual
rollout. The reviewed helper revision at the time copied
`~/.ssh/id_ed25519_shared` private material to every node and appended a
`Host * IdentityFile` rule, which did not meet this site's key separation.
That historical finding is not a blanket ban on current Sync releases: for a
fresh site, review the current generated SSH and Netplan changes, use Cluster
Assistant when they meet policy, and stop for operator review when they do not.

## Historical manual CLI rollout record

This section records the values used in 2026; it is not an installation
procedure. `nodes/bin/configure-direct-fabric --apply` is retired and refuses
to write Netplan. The helper retains read-only emit/check/postcheck modes and a
narrow rollback for a file that an older revision of this helper itself owned.

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

The archived helper emits the former
`/etc/netplan/99-vonk-node-direct-fabric.yaml` shape for comparison and refuses
to mix ownership with `99-nvidia-sync-cluster.yaml` or any other Netplan
document that mentions either selected CX-7 interface. Its rollback uses
`netplan try` and applies only when its exact managed file exists. It has no SSH
or private-key handling.

The current two lab nodes predate NVIDIA Sync Cluster Assistant and still have
`/etc/netplan/99-dgx-spark-direct-fabric.yaml`. That is a foreign owner by
design: the fallback helper must refuse to add its own file while this legacy
document exists. The nodes also retain a narrow historical head-to-worker key.
Keep both untouched during workload rollout. Migrate network and node-to-node
SSH ownership together in a separate console-backed maintenance window through
NVIDIA Sync, which will own `99-nvidia-sync-cluster.yaml`; never rename the
legacy file to bypass the guard or recreate the old key flow on a fresh cluster.

### Manual rollback

This recovery applies only to
`/etc/netplan/99-vonk-node-direct-fabric.yaml`, a file created by an older
helper revision. It intentionally refuses the current lab's foreign
`99-dgx-spark-direct-fabric.yaml`; migrate that file through NVIDIA Sync.

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

For an existing site, capture the following read-only output from both nodes
and retain the NVIDIA Sync validation report when Sync owns the cluster. Do not
copy, rename, or rewrite any Netplan file during evidence collection.

```bash
for host in vonk-node-1 vonk-node-2; do
  ssh -o BatchMode=yes -o ForwardAgent=no "$host" '
    set -euo pipefail
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

This is a historical deep-validation path for the existing manually configured
site. A fresh cluster uses NVIDIA Sync's generated network inspection and speed
report; do not install host CUDA, MPI, NCCL, or benchmark source trees merely
to reproduce this old evidence.

On the existing site, run `scripts/validate-fabric` with `--inventory
inventory/cluster.toml`, both ordered `--expected-node
<SPARK_NODE_ID>=<INVENTORY_SSH_ALIAS>` bindings, and `--output
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

The recorded source installation followed NVIDIA `dgx-spark-playbooks` commit
`1fb66f059ee427c5a3678b3117ef73aab042b458`. Each GPU node has OpenMPI packages
`libopenmpi-dev` and `openmpi-bin` at `4.1.6-7ubuntu2`, CUDA 13 nvcc, NCCL
`v2.30.7-1` commit `73cf112295c33aee2b895f329f592f2a9b4b0f97`, and nccl-tests
commit `a0b82b2260cf5152b9f8c061bbf7eaf0ba096432`. The accepted historical build
used the host user and `/usr/local/cuda/bin/nvcc`,
`NVCC_GENCODE='-gencode=arch=compute_121,code=sm_121'`, and `MPI=1`.
`validate-fabric` does not build, clean, lock, or otherwise stage those source
trees; it verifies the completed pinned artifacts before testing the fabric.

Before a source tree is created, run the non-mutating worker-first gate:

```bash
scripts/validate-fabric --inventory inventory/cluster.toml \
  --expected-node '<SPARK_1_NODE_ID>=vonk-node-1' \
  --expected-node '<SPARK_2_NODE_ID>=vonk-node-2' \
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
