# Live Vonk Forge GPU node health

Use the developer-machine controller for a fresh, read-only view of both
GPU nodes:

```bash
uv run --no-project --with jsonschema -- bin/vonkctl nodes status
uv run --no-project --with jsonschema -- bin/vonkctl nodes status --json
```

The command starts both key-only SSH probes concurrently and always renders
`node1`, then `node2`. It sends the checked-in `nodes/bin/collect-health`
bytes to `bash -s` over standard input. It does not install a collector, use
`sudo`, retain a sample, enumerate model processes, repair a node, or switch a
Cluster Profile. `vonkctl status` remains the separate, fast local view of
persisted profile-controller state.

## What it reports

The schema-version-1 document reports identity and uptime, sampled CPU use and
load, unified system memory and swap, root filesystem capacity/mount state,
NVIDIA GB10 query status, hardware thermal zones/trips, both direct-fabric
functions and RDMA counters, Docker query availability, and `earlyoom` state.
The complete contract is `schemas/node-health.schema.json`.

An unsupported individual accelerator field, such as power draw, is `null`
without a warning when `nvidia-smi` otherwise works. A wholly unavailable
optional source produces a warning. Unknown safety-foundation state, including
root mount mode, a required fabric property, or a monitored RDMA counter,
fails closed. An unreachable node retains a complete envelope with telemetry
sections set to `null`, while the other node's live result is preserved.

No generic threshold is assigned to CPU load, CPU/GPU utilization, raw
temperature, or available unified memory. A serving model may legitimately
consume most of the 128 GB nominal unified memory. Model-specific capacity and
co-residency decisions remain admission checks for the selected Cluster
Profile.

The inventory pins GID index 3 and cross-node fabric addressing. This command
validates those pins as local configuration, but the current collector contract
does not report a live GID field; therefore `nodes status` does not claim to
have observed GID 3. Use the accepted fabric validation in
[the fabric runbook](fabric.md) after changes that could affect GID selection.

## Accepted observation on 2026-08-02

A fresh `vonkctl nodes status --json` exited `0`. Both nodes were `healthy`
with no warnings or errors. Docker was available, each GPU reported 39 C, and
both fabric functions on each node reported speed `200000`, MTU 1500, and RDMA
state `ACTIVE`.

## States and exit codes

- `healthy`: no approved warning or critical condition; exit `0`.
- `warning`: swap above 1 GiB, root free below 150 GiB, a hardware hot/passive
  trip, or an optional telemetry source unavailable; exit `0`.
- `critical`: read-only root, unavailable NVIDIA query, enabled/active
  `earlyoom`, hostname/fabric/RDMA mismatch or unknown foundation state, an
  RDMA counter above the accepted absolute baseline, a critical thermal trip,
  or malformed/timed-out/nonzero/truncated collection; exit `4`.
- `unreachable`: SSH returned connection/authentication status 255; overall
  state is `critical` and the command exits `4`.
- A local collector, schema, inventory, or accepted-baseline preflight failure
  happens before either probe and exits `5`. Invalid command/controller
  arguments continue to exit `2`.

The rc-255 classification follows OpenSSH's process contract. A known SSH
rc255 is `unreachable`. The backend has one overall deadline and no separate
authentication phase signal: if that deadline expires before OpenSSH returns,
the bounded result is the fail-closed `critical` / `collector_timeout`, even
when a later SSH diagnostic reveals a slow authentication refusal. The backend
also cannot distinguish an unusual remote script that itself exits 255; the
fixed repository collector does not use that exit status. An unlocked
1Password vault and an approved SSH signing request are therefore operational
prerequisites for live collection.

## Troubleshooting

1. Preserve JSON first: `bin/vonkctl nodes status --json > /tmp/node-node-health.json`.
2. For `ssh_unreachable`, verify `ssh vonk-node-1 true` and
   `ssh vonk-node-2 true`, unlock/approve the 1Password SSH agent if required,
   and do not weaken strict host-key checking.
3. For `hostname_mismatch`, inspect the SSH alias target; do not accept the
   wrong logical node.
4. For an unavailable Docker query, do not add the login or `vonk-agent` to the
   Docker group. That group is root-equivalent and is not required by the
   supported agent path. Validate the platform separately with
   `sudo docker version --format '{{.Server.Version}}'`, `nvidia-ctk cdi list`,
   and the NVIDIA GPU-container command in
   [Install the Vonk Forge agent](../operations/install-vonk-agent.md). Routine
   workload authority remains the controller-signed helper boundary.
5. For fabric/RDMA errors, run the read-only checks in
   [the fabric runbook](fabric.md) and compare with
   `inventory/reports/rdma-nccl.json`. The health comparison uses the accepted
   absolute `rdma_counters_after` values, not the recorded deltas.
6. For collector errors, run the checked-in collector tests locally. Do not
   copy or leave the collector on a GPU node.

Warnings are observations, not automatic permission or denial for a model.
Investigate unexpected changes before profile activation; never modify the
health rules merely to make a live result pass.
