# `vonkctl` Live Node Health Design

Historical design, not current implementation instructions. Follow
[current API contracts](../../api-contracts.md) for Pydantic/typify generation
and optional-field serialization: omit unused optional-null fields, retain
required nullable fields, and use shared canonical serialization.

Date: 2026-08-02

## Purpose

Add a live, read-only health view for both Vonk Forge GPU nodes without installing a
monitoring agent, creating a database, or moving monitoring services onto the
AI hosts. The developer-machine controller exposes the view as:

```text
vonkctl nodes status
vonkctl nodes status --json
```

The ordinary `vonkctl status` command remains the fast view of persisted
Cluster Profile controller state. It does not initiate SSH calls and does not
embed a stale node-health sample. `vonkctl nodes status` is always a fresh
probe.

## Scope

The live probe answers three questions:

1. Can the controller reach and authenticate to each expected node?
2. Are the host, NVIDIA, and direct-fabric foundations required by AI workloads
   operational?
3. What capacity and thermal signals should a human or later NAS controller see
   before deciding whether to investigate or run profile admission?

The command does not decide whether a target Model Definition fits. Target-
specific memory, disk, placement, fabric, and co-residency requirements remain
the responsibility of `vonkctl validate <profile>` and the admission engine.
An active model may legitimately consume most unified memory, so low available
memory alone does not make a serving node unhealthy.

## Non-goals

- No time-series database, Prometheus, Grafana, DCGM service, or node exporter.
- No background polling, daemon, cron job, or retained health history.
- No `sudo`, package installation, persistent script deployment, or host change.
- No container inventory or arbitrary process listing.
- No replacement for model endpoint health, output-quality checks, or profile
  state.
- No invented CPU, GPU, or SoC temperature limit where the hardware does not
  expose an applicable trip point.

Historical monitoring and dashboards belong on the future external control
host or NAS and require a separate design.

## Command and concurrency

`vonkctl nodes status` probes `node2` and `node1` concurrently. Results are
always rendered in canonical `node1`, `node2` order regardless of completion
order. One slow or unreachable node does not prevent the other node's result
from being returned.

Controller configuration adds:

```toml
[health]
timeout_seconds = 10
cpu_sample_milliseconds = 250
max_output_bytes = 262144
```

Each node has its own 10-second deadline. The complete command therefore does
not serialize two timeouts. The CPU utilization sample reads `/proc/stat`,
waits 250 milliseconds, reads it again, and derives non-idle utilization over
that interval. Load averages remain separate raw pressure indicators.

## Remote collection boundary

The collector is the repository-owned `nodes/bin/collect-health` script. The
controller reads that fixed local file and sends its bytes to the remote
process over standard input:

```text
ssh <strict options> <alias> bash -s -- --json
```

Nothing is copied to or retained on either GPU node. The backend invokes the local
SSH process with an argv vector and `shell=False`. It uses the configured node
alias plus `BatchMode=yes`, `ForwardAgent=no`, `IdentitiesOnly=yes`, strict host
key checking, an explicit connection timeout, bounded stdout/stderr, and the
overall per-node deadline. It never forwards the 1Password agent or reads,
prints, or transfers private-key material.

The backend interface is:

```python
SshBackend.run_script(
    node: str,
    script: bytes,
    argv: tuple[str, ...],
    timeout: float,
) -> CommandResult
```

Only the checked-in collector path may call this interface from the health
service. Workload adapters continue to use their explicit deployed commands;
this does not create a general remote shell feature.

## Collected data

The collector uses `/proc`, `/sys`, `findmnt`, `nvidia-smi`, `systemctl`,
`docker`, and `rdma` only when available to the unprivileged `carst` account.
An optional field that cannot be queried becomes `null` and records a warning;
it is never silently replaced with zero.

| Section | Fields |
| --- | --- |
| Identity | hostname, boot ID, uptime seconds, collection timestamp |
| CPU | logical processor count, 250 ms utilization percentage, load averages for 1/5/15 minutes |
| Memory | total, available, used bytes and used percentage |
| Swap | total, free, used bytes and used percentage |
| Root filesystem | total, available, used bytes, used percentage, and read/write mount state |
| NVIDIA GB10 | query availability, device name, driver, GPU utilization, temperature, performance state, and power draw when supported |
| Thermal | readable thermal-zone type, current temperature, enabled trip-point type and temperature, and whether a trip point is reached |
| Fabric | both inventory-pinned function names, interface, HCA, operstate, carrier, speed, MTU, RDMA link state, and monitored error counters |
| Services | Docker query availability/version and `earlyoom` load, enabled, and active state |

Vonk Forge GPU node unified memory is reported from `/proc/meminfo`. A missing or `N/A`
GPU-memory field from `nvidia-smi` is not treated as an error and is not exposed
as a second memory pool.

The controller receives the expected function/interface/HCA mapping from the
validated controller inventory. GID index `3` is also pinned there and is
supported by the prior accepted fabric evidence; this live command does not
freshly observe or re-prove the GID index. The live probe checks the declared
interface/HCA pairing, MTU, link speed, RDMA state, and monitored counters. The
controller also loads the accepted RDMA counter baseline from
`inventory/reports/rdma-nccl.json`. It does not discover a different fabric or
baseline and quietly accept it.

## Stable JSON contract

`--json` emits one bounded document with `schema_version = 1`:

```json
{
  "schema_version": 1,
  "captured_at": "2026-08-02T12:00:00Z",
  "status": "healthy",
  "nodes": {
    "node1": {
      "status": "healthy",
      "errors": [],
      "warnings": [],
      "identity": {
        "hostname": "node-3542",
        "boot_id": "...",
        "uptime_seconds": 12345
      },
      "cpu": {
        "logical_processors": 20,
        "utilization_percent": 12.3,
        "load_1": 1.2,
        "load_5": 1.0,
        "load_15": 0.8
      },
      "memory": {
        "total_bytes": 130663231488,
        "available_bytes": 120000000000,
        "used_bytes": 10663231488,
        "used_percent": 8.2
      },
      "swap": {
        "total_bytes": 0,
        "free_bytes": 0,
        "used_bytes": 0,
        "used_percent": 0.0
      },
      "root_filesystem": {
        "total_bytes": 4031871553536,
        "available_bytes": 3787009835008,
        "used_bytes": 244861718528,
        "used_percent": 6.1,
        "read_only": false
      },
      "accelerator": {
        "available": true,
        "name": "NVIDIA GB10",
        "driver_version": "580.173.02",
        "utilization_percent": 0.0,
        "temperature_c": 40.0,
        "performance_state": "P8",
        "power_watts": 4.0
      },
      "thermal_zones": [],
      "fabric": {
        "functions": []
      },
      "services": {
        "docker_available": true,
        "docker_version": "29.2.1",
        "earlyoom_load_state": "not-found",
        "earlyoom_enabled": false,
        "earlyoom_active": false
      }
    },
    "node2": {}
  }
}
```

Percentages are rounded to one decimal place. Byte counts, uptime, carrier,
speed, MTU, and counters remain integers. Node keys and function records have
deterministic order. Errors and warnings are stable, sorted machine-readable
codes rather than prose-only messages. The inventory-pinned GID index is not a
field in this live result because the probe does not freshly observe it.

An unreachable node still receives a complete node envelope:

```json
{
  "status": "unreachable",
  "errors": ["ssh_unreachable"],
  "warnings": [],
  "identity": null,
  "cpu": null,
  "memory": null,
  "swap": null,
  "root_filesystem": null,
  "accelerator": null,
  "thermal_zones": [],
  "fabric": null,
  "services": null
}
```

## Health evaluation

Node states are `healthy`, `warning`, `critical`, or `unreachable`.

Critical conditions are limited to evidence that the node cannot safely serve
its declared AI role:

- SSH succeeds but the collector is malformed, truncated, or times out;
- the root filesystem is read-only;
- `nvidia-smi` cannot query the GB10;
- `earlyoom` is enabled or active;
- an expected fabric interface is not up with carrier, speed `200000`, and MTU
  `1500`;
- an expected RDMA link is not active or its HCA mapping differs from inventory;
- a monitored RDMA error counter exceeds its accepted absolute baseline; or
- a readable thermal zone is at or above an enabled hardware critical trip
  point.

An SSH connection failure produces `unreachable`, not `critical`, so the cause
remains explicit.

Warnings include:

- swap use above `1 GiB`;
- root-filesystem free space below `150 GiB`;
- an optional telemetry source is wholly unavailable while the critical
  foundation remains proven; or
- a readable thermal zone is at or above an enabled hardware hot/passive trip
  point below critical.

An individual unsupported optional field, such as power draw, remains `null`
without producing a warning when the surrounding telemetry source succeeds.
CPU load, CPU utilization, accelerator utilization, raw temperature, and
available unified memory are reported but do not use invented generic health
thresholds. Target-specific admission evaluates available memory and disk
against the selected Model Definitions.

Overall cluster status is:

- `healthy` when both nodes are healthy;
- `warning` when at least one node is warning and neither is critical or
  unreachable; and
- `critical` when either node is critical or unreachable.

## Human output and exit codes

The default output is a compact table:

```text
NODE    STATE    CPU    LOAD1   MEM AVAILABLE   SWAP USED   ROOT FREE   GPU   TEMP   FABRIC   UPTIME
node1  healthy  12.3%  1.20    111.8 GiB       0 B         3.44 TiB    0%    40 C   2/2 up   3d 04h
node2  warning  18.1%  2.10    109.4 GiB       1.2 GiB     3.42 TiB    4%    43 C   2/2 up   3d 04h
```

Stable warning and error codes appear below the table with short explanations.
No secrets, environment values, SSH paths, private keys, or unbounded remote
logs are printed.

Exit codes are:

| Code | Meaning |
| ---: | --- |
| 0 | Complete live result; overall state is `healthy` or `warning` |
| 2 | Invalid local arguments or controller configuration |
| 4 | At least one node is `critical` or `unreachable` |
| 5 | The local collector, schema, or inventory cannot be loaded before probing |

`warning` intentionally remains exit code 0 so ordinary capacity observations
do not break automation. Machine consumers must inspect the JSON `status` when
warnings matter to their policy.

## Failure handling

- Both node probes start even if one immediately fails.
- SSH stderr is bounded and normalized to an error code; it is not copied into
  the stable JSON contract.
- Nonzero remote exit, timeout, invalid UTF-8, invalid JSON, schema failure, or
  output truncation creates a `critical` node result with a specific code.
- No failure mutates Cluster Profile state, endpoint publication, maturity
  evidence, or the GPU nodes.
- `vonkctl nodes status` never attempts automatic repair or profile switching.

## Testing

Implementation follows test-first development and includes:

1. collector fixture tests for CPU sampling, memory/swap arithmetic, root mount
   state, nullable NVIDIA fields, thermal trip points, fabric mapping, and
   `earlyoom`;
2. backend tests proving argv-only `shell=False`, strict SSH options, stdin
   script delivery, timeout, and bounded output;
3. a barrier-based concurrency test proving both node probes overlap;
4. service tests for healthy, warning, critical, unreachable, malformed,
   truncated, and partial-cluster results;
5. JSON Schema validation and a frozen representative version-1 fixture;
6. CLI tests for table output, `--json`, deterministic node order, and exit
   codes 0, 2, 4, and 5; and
7. a live read-only smoke test against both configured aliases after offline
   tests pass.

No automated test changes either GPU node.

## Documentation integration

The static [model and profile overview](../../model-profile-overview.md) remains
the human map of intended profiles and Model Definitions. Live node health is
not embedded in that document. The `vonkctl catalog --json` and
`vonkctl nodes status --json` interfaces provide machine-readable catalog and
health views without generating or maintaining separate HTML.
