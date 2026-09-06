# Metrics implementation and visual acceptance contract

Date: 2026-09-04. Addendum to the interface implementation specification. Required by the user's request for all the metrics of Mia Labs SparkDash and NVIDIA PAIR, presented more clearly. This is implementation scope, not a claim of current support. Sol must track every row to collection, persistence/API, CLI, web, and evidence. An unsupported sensor is an explicit capability result, not permission to silently omit an entire adapter.

## Evidence baseline

- MiaAI-Lab/sparkDash main inspected at `cc44d3527e7ddd339f513bb205dce4f37072beff`: [types](https://github.com/MiaAI-Lab/sparkDash/blob/cc44d3527e7ddd339f513bb205dce4f37072beff/src/api/types.ts), README and runtime metrics descriptions.
- NVIDIA/Personal-AI-Router main inspected at `13b68115fa2c9c1d94f1ead1358f8d5a527cfecf`: [metrics](https://github.com/NVIDIA/Personal-AI-Router/blob/13b68115fa2c9c1d94f1ead1358f8d5a527cfecf/desktop/src/shared/types/metrics.ts), [hardware](https://github.com/NVIDIA/Personal-AI-Router/blob/13b68115fa2c9c1d94f1ead1358f8d5a527cfecf/desktop/src/shared/types/hardware.ts), [workloads](https://github.com/NVIDIA/Personal-AI-Router/blob/13b68115fa2c9c1d94f1ead1358f8d5a527cfecf/desktop/src/shared/types/workloads.ts).
- Platform baseline `a31106bb152d3bd9fe60313e544babb7fc0cc377` already collects CPU usage/load, memory availability, aggregate disk capacity, GPU utilization/memory/temperature/power and aggregate network rates. It retains history. These are a starting point, not full parity.

PAIR's useful idea is linking workloads to their actual serving node. SparkDash adds diagnostic depth and inference performance. Vonk must connect both to exact model, recipe, profile and preparation state. Do not reproduce a wall of overlapping CPU, memory and GPU traces with incompatible units.

## Coverage ledger

Existing = baseline has a partial scalar representation; extend it to correct dimensions and provenance. New = requires verified producer and contract. Each field must include unit and availability semantics.

| Group | Required fields | Collection / existing coverage | Display |
|---|---|---|---|
| Identity | Spark name, online state, last observation, uptime; CPU model/cores/threads; GPU IDs/names/chip; physical RAM, storage device/model/capacity; driver/CUDA; runtime engine/version; inference readiness | Agent hardware inventory plus Controller runtime ownership; partly existing | Detail heading and Hardware inventory; abbreviated freshness on Fleet |
| GPU activity | Per-GPU utilization %, temperature °C, measured draw W, power limit W, SM clock MHz/max/%; performance state | Native supported NVIDIA interface; utilization/temp/power partly existing | Fleet GPU/temp/power glance; GPU chart group |
| GPU limits | Thermal, hardware slowdown and power cap flags; active throttle and reason/detail; top GPU processes PID/name/memory bytes | Native interface with bounded process enumeration; new | Contextual limit message, clock/power chart annotations, process table |
| CPU | Utilization %, load averages, temperature °C, measured package draw W where exposed, configured TDP W | proc/sysfs/native sensor; usage/1m load existing; never substitute TDP for consumption | CPU chart group; hardware detail |
| Shared memory | Physical total/used/available bytes and %, GPU-attributed allocations when exposed, CPU-attributed usage only if attribution is reliable; OOM pressure/risk and reason; memory bandwidth current/peak with unit and source | MemAvailable plus supported native counters; basic capacity existing | Single physical pool meter on GB10, allocation breakdown only with supported accounting; memory chart |
| Dedicated GPU memory | Per-GPU used/free/total bytes and % | Native GPU counter; partial scalar existing | GPU detail; never label shared host pool as additional dedicated VRAM |
| Storage | Per-device/mount capacity, used/free/available bytes and %, read/write B/s, device presence/disabled state | Native diskstats/filesystem; capacity partial, IO new | Storage charts + compact device table; separate NAS cache storage and Spark storage |
| Network/fabric | Per-interface RX/TX B/s, link speed bps, operstate, primary interface, interface addresses when permitted | Native counters; aggregate existing | Network chart group and interface table; distributed run shows participating links without inferring fabric health from bytes alone |
| Runtime identity | Per runtime/run: backend, port/endpoint, model/version, exact recipe revision, context limit, memory target, serving nodes/ranks, readiness and errors | Controller-owned launch inventory + authenticated runtime adapter; new metrics adapters | Inference heading and runtime selector; distinguish distributed run from replicas |
| Inference throughput | Generation/decode tok/s, prefill tok/s, cached and uncached prefill tok/s where supplied; total output tokens | Runtime counters with reset-aware sampling; new | Inference throughput chart; Fleet active run shows decode rate and queue |
| Inference pressure | Active/total slots, running/waiting requests, KV cache %, cumulative preemptions, prefix cache hit %, MTP acceptance % | Runtime-specific adapters; new | Queue/cache chart group, labelled current counts, limit explanation |
| Inference latency | TTFT p95, end-to-end p95, inter-token latency p95 in ms (wire unit documented); population/window | Runtime histograms or measured request spans; new | Three aligned latency series/cards with sample count; never manufacture p95 from average |
| Daily performance | Local-date daily decode and prefill max and busy-time average; cached/uncached prefill equivalents | Rollups of observed valid samples; new | History summary table, explicit timezone and sampled coverage |
| Requests/jobs | Composite request identity, model/recipe, engine, state, origin/requester, actual serving node(s), created/start/end times, elapsed, sanitized failure; active/completed/failed filters | Controller/router correlation; PAIR source explicitly distinguishes origin and scheduled node | Workload table beneath inference charts; separate lifecycle Operations |
| Optional image runtime | Comfy availability/version/PyTorch/device, queue running/pending; job title/model, steps, dimensions, batch/sampler; progress value/max/% and source, current node label; last outcome/duration; ETA source; installed checkpoints/LoRAs | Capability-specific adapter only when managed recipe exposes it; new | Image workload detail replaces token-specific widgets; unavailable per-job GPU attribution stays unavailable |
| Optional service health | Tailscale installed/online/backend/version/health/key expiry; Hermes installed/version/update state/check time/error | Only enrolled/configured service probes; new optional capabilities | Spark Services tab. No new power controls, update execution or public port scanning implied |
| Benchmark evidence | Existing result per exact model/recipe/config/hardware: concurrency, per-stream and aggregate decode, mean/median/min/max, prefill, target/actual context, TTFT/content TTFT, tokens, duration/errors and recorded hardware samples | Read persisted qualification/benchmark evidence if available; adapter contract when absent | Library model/recipe Performance evidence; label measured hardware/date/config. Running a benchmark is explicit load-generating review, never a side effect of browsing |

Runtime adapters must identify support individually for llama.cpp, vLLM, SGLang and managed recipes using ds4/EXL3 when those expose metrics. Unsupported backend metrics remain named with a reason and capability support summary. A model can run without a metrics adapter; telemetry limitations must not become an installability veto. Model capabilities and runtime-recipe restrictions remain separate.

## Visual design: F2 Metrics workspace

Keep the established graphite surfaces, restrained green actions, system typography and warm review surface. Metrics detail remains graphite: opening charts does not initiate a review or mutation.

Fleet overview: each Spark row/card contains state and observation age, running model + recipe, GPU utilization, physical memory used/total, temperature and power. Use one tidy labelled metric strip; CPU is a secondary compact value. Active run row adds decode tok/s and waiting requests only when supported. Unknown values use an em dash with a textual reason, never zero. High GPU utilization alone means busy, not unhealthy. One explicit throttle/pressure fault overrides decorative utilization coloring. No giant chart above the first Sparks.

Selecting a Spark opens a full-width detail route, preserving back navigation, search/filter context and selected time range. Heading: name, state, observed age, running workload links. Tabs: Overview, Metrics, Workloads, Services, Events. Metrics has one top range toolbar (Live, 1h, 24h, 7d, 31d), timezone, pause/resume, last sample, and export. Live is the default; pause stops visual updates and labels the frozen time. Keep data collection independent of tab visibility.

Desktop Metrics body: 2 columns of aligned chart groups with 24px gutter and at least 220px plot height: GPU activity; physical memory; CPU; power and temperature (separate axes with visible units or separate plots); storage IO; network. At <=900px stack groups. At 360px keep all controls accessible, shorten ticks, let tables scroll within their section. Device/interface selector sits inside its group, not in a global filter forest. One metric family per plot; label series directly in legend, use distinguishable line styles and stable colors. Do not use decorative neon gradients, 3D gauges or animated background glows.

Each chart: title, current value/unit, valid observation time, useful min/max for selected range, visible axis units, keyboard-focusable points, textual/table alternative, explicit gaps for missing data. Synchronize hover/focus time across visible charts; no tooltip as the only way to read a value. Cursor labels remain in viewport. Never interpolate across offline intervals. History resolution and downsampling must be returned by the server and shown in export.

Selecting a running model opens Inference metrics, scoped to the exact run identity. Top row shows model/version, recipe revision, participating Sparks/ranks and ready/starting/failed state. Below: Throughput, Latency, Queue and cache chart groups; request/job table last. Distributed ranks are inspectable, but the group headline comes from coordinator metrics and does not sum the same request across ranks. Replica aggregates require explicit semantics. A model comparison links to recorded benchmark evidence, not misleading instantaneous speeds from different workloads.

Exceptions are contextual: stale banner once per detail view, sensor absence near affected chart, runtime authentication failure with an actionable connection diagnosis, empty history with collection status. A stopped run retains its historical metrics. Fresh host telemetry cannot make a failed runtime appear healthy.

## Shared API and CLI contract

Sol owns exact route/type names in the contract ledger. Native agent collection goes through existing authenticated Controller ingestion. Extend the active contract explicitly; do not invent a schema-1 fallback. No browser-direct runtime access, SSH scraper, arbitrary endpoint scanning or unrestricted URL probing.

Every series identifies node, device/interface or run, metric key, unit, source, measurement kind (measured/derived/estimated/configured), observed_at, received_at, freshness threshold, support status, unavailable reason, and aggregation/window. Capability discovery must expose all supported/unsupported metric keys. Preserve observation times on delayed ingestion. Validate finite values/ranges, bound cardinality/retention and payload sizes, redact secrets and sensitive prompts. Runtime process names do not include command-line credentials. Poll on a bounded schedule with backoff and timeouts; one broken sensor must not suppress the rest.

Use null plus a stable reason for unsupported, permission-denied, not-running, stale or collection-error. Estimated total system power must be labelled estimated and cannot be confused with a measured wall-power sensor. CPU TDP, GPU power limit and actual power draw are distinct. Risk heuristics disclose their basis. Counter resets/restarts yield a sampling gap, never a negative spike. Bandwidth capability maximum is not current measured bandwidth. Shared GB10 memory cannot count RAM and GPU pool twice.

History supports the same metric/device/run filters as current state, bounded pagination, requested/actual interval and coverage counts. Aggregate rates over elapsed valid time. Busy-time averages exclude idle samples and are null if no busy samples exist. Percentiles aggregate histogram buckets or raw spans, never average percentiles. Daily maxima are maxima of sampled observations, not guaranteed physical peaks.

CLI must expose current state, capability inventory, historical series/ranges, per-device and per-run filters, requests/jobs filters, export and optional benchmark evidence. JSON includes raw canonical units and the same availability/provenance as web; watch mode has bounded polling and interruption. Agent users must be able to answer why an apparently slow run is queued, throttled, copying, or waiting for memory using CLI alone. Metric reads never mutate or start workloads.

## Acceptance additions

- A17 Coverage ledger: every row has actual producer/adapter evidence or explicit unsupported capability with reason, plus current/history/API/CLI/web mapping. Stub fields alone do not constitute implemented support.
- A18 Physical shared-memory fixture: GB10 128GB pool displayed once; GPU allocation and host availability do not become 256GB or an invented residual split.
- A19 Counter restart, stale/out-of-order telemetry, offline gap and one failing sensor retain correct freshness and rates; no fabricated zero or negative bandwidth.
- A20 Runtime adapter fixtures prove decode/prefill, queue/KV, histogram percentiles and unsupported fields for distinct backends. No duplicated distributed token counts; origin differs from actual executor in workload fixture.
- A21 Keyboard/mobile/current/history/export journeys; chart data matches CLI JSON for identical scope/range. Labels, units and device identity survive resize and theme contrast checks.
- A22 Busy 95% GPU with healthy runtime remains healthy; thermal throttling or runtime failure remains visible despite healthy host heartbeat. Estimated power and unavailable sensor examples are visibly distinguished.
- A23 Optional image/service adapters have truthful support states; opening Fleet cannot execute benchmarks, upgrades, service changes or arbitrary probes.

Repository fixtures and CI prove contracts and rendering. Physical sensor availability, native NVIDIA accuracy and performance overhead need separate Spark evidence; report that boundary without pretending unsupported fixtures prove hardware acceptance.
