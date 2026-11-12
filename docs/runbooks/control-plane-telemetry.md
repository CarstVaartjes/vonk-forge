# Control-plane telemetry runbook

## What is collected

Authenticated node reports are intended to arrive every two seconds. Fields
are nullable because a platform may not expose a value at a given sample:

| Metric | Unit | Meaning |
| --- | --- | --- |
| Memory total/available | bytes | Unified host memory capacity and currently available memory. |
| Disk total/free | bytes | Filesystem capacity used for recipe/model admission. |
| GPU utilization | percent | GPU utilization when exposed by the driver. |
| GPU memory total/free | bytes | Dedicated GPU memory; not reported on a shared-pool accelerator such as GB10. |
| GPU unavailable reason | code | Why the GPU readings are missing, when they are. |
| GPU temperature | °C | Reported GPU temperature. |
| GPU clock | MHz and state | Current, maximum and locked GPU clock, with the helper's apply receipt. |
| CPU clock avg/min/max | MHz | Current average and minimum core clock; hardware maximum. |
| GPU power | watts | The GPU's power draw (`nvidia-smi power.draw`), not the whole host. |
| Uptime | seconds | Whole seconds since boot (`/proc/uptime`). |
| GPU process memory ("Models") | bytes | Memory the driver says GPU compute processes hold: weights, KV cache and CUDA workspace. Reported only on a shared-pool accelerator, where it is part of memory total minus available. |
| CPU time busy/total | seconds (counters) | `/proc/stat` jiffies over USER_HZ since boot. Utilization is a Prometheus rate of busy over total. |
| Load average | 1m | `/proc/loadavg`. |
| LAN receive/transmit | bytes (counters) | The default-route interface's netdev counters. |
| Fabric receive/transmit, active ports | bytes (counters), count | RDMA port counters of ACTIVE ports (RoCE bypasses the netdev counters), and how many ports are ACTIVE. |
| Disk read/written | bytes (counters) | The disk that holds the model store. |
| OOM kills | count (counter) | `/proc/vmstat` `oom_kill` since boot. |
| Board, NVMe, NIC temperature | °C | Hottest unlabelled `acpitz` zone (never called "CPU"), NVMe Composite, hottest ConnectX ASIC. |
| Fan | RPM | Fastest board fan. |

Protocol 4.4 adds the rows from CPU time down and the per-run engine readings below.
Every one is optional: an older agent omits them and the Controller stores NULL
and exports nothing for them (a gap, never a zero). An invalid reading is
dropped alone, and the optional-evidence recovery starts from the groups the
validation error names, so many invalid readings cost a few validations. The agent collects the host readings
(M5a) and the engine readings (M5b) in later changes; until then the Rust
literal reports them as absent.

### Engine readings

`engines` (one per running endpoint) carries each run's metrics as
the monitor reads them from the engine's own `/metrics` on loopback. An entry
with an invalid `run_id` or `format` is dropped alone, and a repeated `run_id`
keeps its first entry. Counters are cumulative since the engine started:
prompt, generation and cached prompt tokens, preemptions, speculative draft and
accepted tokens. Gauges: requests running and waiting, KV cache usage ratio.
Histogram
sums and counts (no percentiles; the gateway carries those): time to first
token, time per output token, prefill, decode and end to end. The Controller
stores them in `node_engine_samples` (one row per sample and run, removed with
the sample after 24 h) and never puts them on the Fleet point.

Prometheus exports them from control-api `/metrics` as `vonk_node_*` (host) and
`vonk_engine_*` series with labels `{node_id, alias}`. The alias is the run's
published route name, joined once from the Controller's run table; a run with
no published route is not exported. Each series is the newest reading of that
(node, run) received within 30 seconds, not only the latest sample, so a slow engine
does not gap the series. Rates are computed in Prometheus, once.

The Controller derives one more value for the fleet view, `system_memory_bytes`
("System"): memory total minus available minus GPU process memory, never below
zero, present only when all three are reported. It is everything in use outside
GPU processes, including the model servers' own CPU-side memory, shared memory
(/dev/shm) and other non-reclaimable cache; reclaimable page cache counts as
free. It is not stored.

DGX Spark GB10 nodes use unified memory. GPU-memory fields and host-memory
fields are not interchangeable: admission uses the resource evidence declared
by the recipe and the node profile. A missing field is omitted or shown as
unknown; it is never converted to zero.

Telemetry contains operational node measurements only. It is not a recipe
secret store, does not include prompt or response content, and must not be
copied into support tickets with credentials or private keys.

## Freshness and delivery

Fleet labels samples using the configured live and delayed thresholds. A
missing, delayed, or stale sample is actionable evidence: web and `vonkctl`
show an online Spark with one as "needs attention" and name the reason. The web client uses
the Fleet SSE stream, reconnects after interruption, and falls back to polling
without mutating node state. The two-second reporting intent is not a promise
that every sample reaches the browser.

### How a Spark reports

A separate `vonk-monitor` process (`vonk-forge-monitor.service`, shipped in
the agent package) samples the host and uploads telemetry; the control agent
never does. A monitor fault therefore cannot stop command polling or corrupt
control progress. The monitor uses the agent's configuration and reloads the
current identity for each upload, so a certificate rotation needs no restart;
a missing or rotating credential skips that upload and is logged.

Every two seconds it takes one fresh snapshot and attempts at most one upload
with a two-second timeout. Missed ticks are skipped, and a failed upload drops
its snapshot: there are no retries, queues or local history. The Controller
stamps receive time and node identity and owns ordering.

Presence comes from successful control-lane contact and telemetry freshness is
reported separately, so a node can be online while its metrics are stale.
Inventory is sent at startup, after enrollment or reconnection, and on an
explicit refresh or change, not on a timer.

### Run memory attribution

While one run holds every active memory claim on a shared-pool Spark, the
Controller derives that run's observed memory peak from the new samples: free
memory at the run's admission minus the lowest available memory since. The peak
only rises, on the run's rank and in `measured_role_peaks`. A peak of zero or
less (the Spark has as much free as at admission: the run has not loaded or has
exited) records nothing.

Attribution runs in a savepoint inside the sample-ingest transaction. If it
fails, only the attribution rolls back; the samples still commit, and the
failure is logged once per process for each node and failure type (`warn_once`).
The attribution warning is therefore not a telemetry outage.

## History and retention

The Controller keeps each node's latest sample for the Fleet view and stream,
and keeps raw samples for 24 hours. It does not keep rollups or serve a
history API. Metric history lives in Prometheus, which scrapes the
Controller's `/metrics` gauges. Fleet
events expire at their expiry time; a bounded worker pass prunes both.

## Troubleshooting

- **No telemetry:** check agent activity, last-seen, certificate expiry, and
  the node's authenticated connection. Do not restart or reconfigure a live
  node merely to make a chart non-empty.
- **Stale telemetry:** inspect the Fleet evidence and stream reconnect state;
  retry the browser request after the agent is healthy.
- **Empty metric history:** check that Prometheus is scraping the
  Controller's `/metrics` endpoint.

Use the local fixture for reproduction. Do not experiment with retention,
agent cadence, migrations, or worker settings against NAS/Spark production
data.
