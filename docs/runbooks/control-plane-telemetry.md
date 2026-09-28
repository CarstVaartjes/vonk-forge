# Control-plane telemetry runbook

## What is collected

Authenticated node reports are intended to arrive every two seconds. Fields
are nullable because a platform may not expose a value at a given sample:

| Metric | Unit | Meaning |
| --- | --- | --- |
| CPU utilization | percent | Host CPU utilization. |
| Load average | 1m | Unix load average over one minute. |
| Memory total/available | bytes | Unified host memory capacity and currently available memory. |
| Disk total/free | bytes | Filesystem capacity used for recipe/model admission. |
| GPU utilization | percent | GPU utilization when exposed by the driver. |
| GPU memory total/free | bytes | GPU memory evidence when exposed. |
| Temperature | °C | Reported device/host temperature. |
| Power | watts | Reported power draw. |
| Network receive/transmit | bytes/s | Agent-observed network throughput. |
| Gap samples | count | Missing sample evidence carried by the telemetry source. |

DGX Spark GB10 nodes use unified memory. GPU-memory fields and host-memory
fields are not interchangeable: admission uses the resource evidence declared
by the recipe and the node profile. A missing field is omitted or shown as
unknown; it is never converted to zero.

Telemetry contains operational node measurements only. It is not a recipe
secret store, does not include prompt or response content, and must not be
copied into support tickets with credentials or private keys.

## Freshness and delivery

Fleet labels samples using the configured live and delayed thresholds. A
missing, delayed, or stale badge is actionable evidence. The web client uses
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

## History and retention

The Controller keeps each node's latest sample for the Fleet view and stream,
and keeps raw samples for 24 hours. It does not keep rollups or serve a
history API. Metric history lives in Prometheus, which scrapes the
Controller's `/metrics` gauges and backs the Grafana Fleet dashboard. Fleet
events expire at their expiry time; a bounded worker pass prunes both.

## Troubleshooting

- **No telemetry:** check agent activity, last-seen, certificate expiry, and
  the node's authenticated connection. Do not restart or reconfigure a live
  node merely to make a chart non-empty.
- **Stale telemetry:** inspect the Fleet evidence and stream reconnect state;
  retry the browser request after the agent is healthy.
- **Empty Grafana history:** check that Prometheus is scraping the
  Controller's `/metrics` endpoint.

Use the local fixture for reproduction. Do not experiment with retention,
agent cadence, migrations, or worker settings against NAS/Spark production
data.
