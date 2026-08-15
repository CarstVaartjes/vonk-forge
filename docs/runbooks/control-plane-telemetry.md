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

## History resolutions and retention

The history API requires an explicit resolution:

- `raw`: up to 24 hours, individual samples;
- `minute`: up to 30 days, one-minute buckets;
- `fifteen-minute`: up to 365 days, 15-minute buckets.

The API caps responses at 1,500 points and returns an honest empty result when
no data exists. Long windows use the coarsest suitable resolution. The UI uses
minute buckets through 24 hours, full 15-minute coverage for seven days, and
the newest bounded 1,500 15-minute buckets for longer windows. The labels make
that bounded behavior explicit.

Rollup points include source sample count, gap count, and per-metric count,
minimum, mean, and maximum. Charts show the min–max range and count-weighted
mean; they do not interpolate missing samples. Retention keeps raw samples for
24 hours, minute buckets for 30 days, 15-minute buckets for 365 days, and
expires Fleet events at their expiry time. Maintenance rolls up before pruning
and uses bounded work with node-first locking.

## Troubleshooting

- **No telemetry:** check agent activity, last-seen, certificate expiry, and
  the node's authenticated connection. Do not restart or reconfigure a live
  node merely to make a chart non-empty.
- **Stale telemetry:** inspect the Fleet evidence and stream reconnect state;
  retry the browser request after the agent is healthy.
- **Empty history:** verify the selected node, time window, and explicit
  resolution. Empty is valid when the retention window has no samples.
- **Chart error:** use Retry. The selected window and keyboard focus should be
  preserved; record the request error without pasting secrets.

Use the local fixture for reproduction. Do not experiment with retention,
agent cadence, migrations, or worker settings against NAS/Spark production
data.
