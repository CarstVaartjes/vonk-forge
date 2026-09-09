# Spark monitoring redesign

## Purpose

The control agent owns durable control commands, operation journals, package
activation, enrollment, certificate rotation, and readiness. A separate
`vonk-monitor` process owns host/runtime sampling and best-effort telemetry
delivery. A monitor fault therefore cannot stop command polling or corrupt
control progress.

## Runtime contract

`vonk-monitor` takes the same agent configuration and uses the active identity
and CA paths. It reloads the current identity for each upload, so a completed
certificate rotation is picked up without restarting the control agent. A
missing, unreadable, or transiently rotated credential causes that interval's
upload to be skipped and is logged; it does not terminate the monitor or the
agent and does not alter TLS validation.

Each loop takes one fresh snapshot and then attempts at most one HTTP upload.
The interval is two seconds, the upload timeout is two seconds, and only one
upload can be in flight. A slow collection or upload advances the monotonic
schedule past all missed ticks; missed ticks are skipped rather than replayed.
An upload failure drops the snapshot. The next interval collects a new sample;
there are no retries, queues, backlog replay, local telemetry history, or
durable telemetry sequence state. Collector counters may retain the previous
sample in process memory solely to calculate rates.

The Controller stamps receive time and node identity and deduplicates or
orders history from those server-owned values. The Spark sample has no durable
sequence bookkeeping. The latest two-second sample is the complete telemetry
unit; aggregation remains a Controller concern.

Presence is derived from successful control-lane contact. Telemetry freshness
is reported independently, so a node can be online while its metrics are
stale. Inventory is sent at startup, after enrollment and reconnection, and on
an explicit refresh/change signal. It is not sent on a fixed 60-second timer.

## Packaging and observability

The Debian package ships and enables `vonk-forge-monitor.service` alongside
`vonk-forge-agent.service`. The monitor has its own process, sandbox, and
read-only configuration/data mounts; it receives no control journal paths.
The agent unit no longer starts a telemetry lane. Grafana and the frontend
consume Controller history and freshness/presence projections.

The protocol remains one canonical nested Pydantic telemetry model. Rust
wire types are generated from its schema and connected producer/consumer
tests exercise the exact JSON request. No legacy telemetry reader or schema
is retained.
