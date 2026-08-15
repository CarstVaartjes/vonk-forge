# Vonk Forge Control Plane Experience Design

**Date:** 2026-08-14
**Status:** Approved by delegated operator judgment

## Context

Vonk Forge has strong control-plane primitives—repository-backed desired state,
authenticated outbound agents, immutable recipe revisions, capacity-aware
admission, durable jobs, and auditable mutations—but the browser presents those
primitives as separate administrative tables. Operators currently have to
translate UUIDs, digests, raw timestamps, repository paths, and recipe JSON into
answers to simple questions:

- Which nodes are available and healthy now?
- What are they doing and how close are they to capacity?
- Which recipes belong to a model?
- Where is each recipe installed, and where is it loaded?
- Can this recipe fit on one node or a complete multi-node group?
- If it cannot fit, what would need to change?

The existing Rust agent reports authenticated capacity inventory every 60
seconds and reports recipe readiness around its claim loop. A richer shell
health collector exists, but an explicit `node.probe` projects only a small
subset of its result into the generic `observations` table. The Fleet endpoint
then loads every historical health observation before choosing the latest in
Python. The browser performs one-shot reads and displays a wide table. This
cannot provide a fast, near-real-time operator experience by refreshing more
often.

The new control website will borrow sparkDash's useful visual hierarchy and
immediacy, but not its SSH probes, unauthenticated trusted-LAN API, or direct
browser-to-node access. Forge remains the sole authenticated authority.

## Goals

- Make Fleet the useful home page for understanding the cluster in seconds.
- Show authenticated node telemetry in the browser approximately two seconds
  after observation under normal conditions.
- Preserve detailed recent telemetry and useful long-term rollups.
- Present models, their multiple recipes, and recipe availability/load state
  across nodes in one understandable workspace.
- Make catalog, install, load, unload, uninstall, and remove operations easy
  while retaining explicit capacity and impact previews.
- Treat fixed multi-node recipe placement as one atomic group operation.
- Keep human names and outcomes primary; keep IDs, digests, certificates, and
  raw JSON available through progressive disclosure.
- Improve control-suite query bounds, frontend maintainability, accessibility,
  tests, and operator documentation as part of the feature.
- Deliver only repository and branch changes. Do not modify the live NAS or any
  Spark node.

## Non-goals

- Changing the MIA recipe implementation, MIA runtime behavior, or readiness
  logic.
- Replacing repository authority, immutable recipe revisions, admission,
  reservations, jobs, or audit records with browser-local state.
- Adding SSH polling, node inbound ports, direct browser-to-agent traffic,
  TimescaleDB, Redis, Grafana, or another required service.
- Automatically unloading a running recipe merely because another recipe was
  selected.
- Assuming a node can fit a recipe from total capacity alone.
- Supporting several alternative node-count shapes for one recipe in this
  release. Each deployment profile has one fixed required node count.
- Hiding advanced controls from operators who need exact evidence.

## Product model

The browser presents a simple relationship while preserving existing
authoritative records:

```text
Model (repository-backed identity)
  1 -> many Recipe revisions (`workload.family` is the model key)

Recipe revision
  -> one fixed deployment profile and node count for a placement
  -> installed on zero or more complete node groups
  -> loaded as zero or more recipe runs

Node
  -> stores multiple recipes while disk and retention policy permit
  -> loads multiple recipes while live memory, reservations, and safety margin permit
```

“Available” has one precise meaning in the primary UI: the exact recipe image
and required artifacts are installed on that node. A compatible but uninstalled
recipe is labelled “Can install,” never “Available.” “Loaded” means an admitted
recipe run exists and the authenticated readiness observation is fresh. A
multi-node recipe is healthy only when every assigned rank is ready.

## Information architecture

Primary navigation contains four destinations:

1. **Fleet** — live node state, capacity, loaded/installed recipes, trends, and
   node detail.
2. **Library** — the Model → Recipes → Nodes workspace plus visual recipe
   details and guided recipe creation/import.
3. **Activity** — running and failed operations, deployments, updates, jobs,
   and audit history, ordered by what needs attention.
4. **System** — agents, enrollment, profiles, repository evidence, package
   internals, and other infrequent administration.

Existing routes remain reachable during migration. The first implementation
replaces Fleet, adds Library, and groups existing navigation links without
rewriting unrelated administrative pages.

## Fleet experience

Fleet is a card collection rather than a horizontally scrolling table. The
page heading contains a compact cluster summary: online nodes, loaded recipes,
available unified memory, and active warnings. Each node card shows:

- display name, role, online/stale/offline state, and last update in human time;
- GPU utilization, unified/GPU memory, CPU utilization, temperature, and power;
- disk usage and network throughput;
- short sparklines for utilization, memory, and temperature;
- loaded recipe chips and installed-but-unloaded recipe count;
- warning text for memory pressure, disk pressure, thermal pressure, stale
  telemetry, incomplete multi-node runs, or failed operations.

Color never carries state alone. Every state has text and, where useful, an
icon or shape. Values retain stable layout while updating so telemetry does not
make cards jump. Updates announce a compact status summary through an ARIA live
region rather than moving keyboard focus.

Selecting a node opens an in-page detail panel with Overview, Recipes,
Performance, and Events sections. Technical details disclose node ID, agent
version, certificate expiry, timestamps, evidence digests, and raw bounded
telemetry. On small screens cards become a single column and details become a
normal page section; no primary task requires horizontal page scrolling.

## Library experience

Library is a responsive three-pane workspace:

```text
Models                    Recipes                    Nodes / placement
selected model       ->   selected recipe       ->  recommended group
recipe count              node count                 installed / loaded state
fleet availability        requirements               live capacity and conflicts
```

The model pane is searchable and uses repository-defined display names and
capabilities. The recipe pane shows every local recipe whose canonical
`workload.family` matches the selected model, including lifecycle, fixed node
count, per-node memory, disk requirement, installed coverage, loaded runs, and
blocking validation. A recipe with no known model appears in an explicit
“Unlinked recipes” group rather than disappearing.

The placement pane ranks complete compatible node groups. Each result explains
why it is recommended and shows:

- all member nodes with roles/ranks;
- fresh available memory and disk per node;
- recipes already installed and loaded on every member;
- fabric or capability constraints;
- whether the exact recipe is installed, partially installed, loaded, or
  blocked;
- the smallest safe operator action.

Selection is reflected in the URL so refresh, browser history, and shared links
preserve context. Keyboard users move among panes with ordinary links and
buttons; focus alone never changes the active model or recipe.

## Visual recipe details and editing

A recipe detail page leads with a visual summary:

- name, purpose, publisher, immutable revision, and lifecycle;
- linked model and exposed capabilities;
- a topology diagram with node count, roles, and ranks;
- per-node and total memory/disk requirements;
- fleet compatibility and current availability/load state;
- source/build provenance and test evidence;
- active/recent jobs and actionable failures.

The default editor is a guided form grouped into Identity, Model & capabilities,
Topology, Resources, Source & artifacts, Runtime, and Review. Raw JSON editing
and JSON upload remain in an Advanced section. Parsing and schema errors map to
human field paths, the last valid visual preview remains visible, and saving a
draft never resolves or deploys it implicitly. The operator reviews the visual
diff before producing a new immutable revision.

## Capacity-aware actions

Every action has one obvious primary control and a server-authored preview.
Buttons describe the next outcome: **Install on 2 nodes**, **Load recipe**,
**Unload**, or **Remove from nodes**.

### Install

The preview evaluates exact artifact bytes, current free disk, existing
reservations, read-only stores, compatibility, and required node count. If the
recipe fits, the default recommendation selects the best complete group. If it
does not fit, Forge may propose garbage collection, but protects loaded
artifacts, active generations, in-progress downloads, and rollback material.
The operator explicitly approves the cleanup impact.

### Load

The preview evaluates current authenticated memory telemetry, resource
reservations, startup peaks, safety margin, required capabilities, and complete
multi-node topology. Existing loaded recipes coexist when they fit. Forge does
not automatically unload them. When memory is insufficient, the preview names
the exact recipe runs that could be unloaded and the resulting capacity; the
operator selects and approves any unloads.

### Multi-node operations

Install and load plans reserve all required nodes before any mutation begins.
The UI represents the plan as one group with per-rank progress. Partial
installation is reported honestly and offers Resume, Repair, or Roll back.
The recipe is never labelled loaded when only some ranks are ready.

### Destructive actions

Unload is reversible and receives a concise impact sheet. Uninstall and catalog
removal explain affected installations, runs, routes, and rollback evidence.
The final control includes the recipe's human name. UUID typing is not used as
a substitute for a clear impact explanation.

## Telemetry architecture

### Collection

The Rust agent gains a focused `telemetry` module. It samples lightweight,
bounded sources every two seconds:

- `/proc/stat` and `/proc/loadavg` for CPU/load;
- `/proc/meminfo` for unified/host memory;
- `statvfs` for the agent artifact store;
- one bounded `nvidia-smi` query for utilization, temperature, power,
  performance state, and accelerator memory where available;
- `/proc/net/dev` deltas for aggregate management/network throughput.

Static inventory remains at 60 seconds. Recipe-run readiness remains on its
existing authenticated endpoint and is joined by the control projection; the
telemetry change does not alter recipe runtime or readiness files.

Each sample contains a schema version, agent boot ID, monotonic sequence,
timezone-aware observed time, fixed core metrics, and bounded optional details.
The agent sends the newest sample every two seconds over its existing mTLS
origin. A bounded in-memory retry queue retains at most 30 seconds of unsent
samples, drops oldest samples first, and reports the resulting gap. Telemetry
failure never prevents operation claims or result delivery.

### Ingestion and storage

`POST /agent/v1/telemetry` accepts at most 16 ordered samples and 64 KiB. The
controller binds the authenticated certificate identity to the node, rejects
future/stale observations and duplicate or regressing sequences within one
boot, and records receive time independently.

PostgreSQL stores:

- `node_telemetry_latest`: one row per node, updated only by a newer sample;
- `node_telemetry_samples`: typed core values plus bounded optional JSON;
- `node_telemetry_rollups`: one-minute and fifteen-minute min/mean/max values.

Core dashboard queries never scan history. The latest table and composite
`(node_id, observed_at DESC)` indexes make Fleet cost proportional to fleet
size. A worker compacts and prunes in bounded batches:

- two-second samples: 24 hours;
- one-minute rollups: 30 days;
- fifteen-minute rollups: 365 days.

Plain PostgreSQL tables are sufficient for the current fleet and keep tests
portable. Daily range partitioning becomes a documented scale trigger if raw
history approaches host memory or retention deletion creates unacceptable
vacuum load; PostgreSQL's own guidance notes that partitioning is most valuable
once tables are very large.

### Browser delivery

`GET /api/v1/fleet/stream` is an authenticated Server-Sent Events endpoint.
It sends an initial fleet snapshot, named `node-telemetry`, `recipe-state`, and
`operation-state` events, a monotonically increasing event ID, and a keepalive
at least every 15 seconds. EventSource reconnect semantics and `Last-Event-ID`
allow recovery without inventing a bidirectional protocol. If streaming is
unavailable, the browser uses bounded 10-second polling and visibly labels the
connection “Reconnecting”; stale node timestamps remain truthful.

`GET /api/v1/nodes/{node_id}/telemetry` returns bounded history for a requested
window and server-selected bucket size. The browser never downloads raw
24-hour samples merely to draw a small chart.

## Control projections and performance

The dashboard projection is split by responsibility:

- `FleetProjection` joins repository nodes with latest agent, telemetry,
  inventory, recipe installation/run, certificate, and operation state using
  latest-row subqueries and bounded aggregations.
- `LibraryProjection` joins repository model definitions with local recipe
  revisions through `workload.family`, then projects installation, run, and
  compatible placement summaries.
- `TelemetryRepository` owns ingestion, latest-row ordering, history buckets,
  rollups, and retention.

No projection returns ORM rows directly. Public response models use stable,
human-oriented fields and include technical identifiers only where subsequent
API actions require them. Collection and projection code share typed contracts,
not ad-hoc dictionary traversal.

The initial performance acceptance targets are:

- Fleet JSON p95 below 200 ms for 128 nodes and one million retained samples;
- Library JSON p95 below 300 ms for 250 models, 1,000 recipe revisions, and 128
  nodes;
- a Fleet update visible within four seconds of observation under normal LAN
  conditions;
- no browser route with document-level horizontal overflow at 360, 768, 1280,
  or 1920 CSS pixels;
- no history endpoint returning more than 1,500 points per metric series.

## Error and stale-state behavior

- **Live:** latest sample is at most 6 seconds old.
- **Delayed:** 6–20 seconds; values remain visible with a delayed label.
- **Stale:** over 20 seconds; charts stop animating and capacity-sensitive
  primary actions require refreshed evidence.
- **Offline:** agent presence exceeds its configured online window.
- **Stream disconnected:** existing values remain visible, connection status
  changes, and bounded polling begins.
- **Partial multi-node state:** the group is degraded and never collapsed into
  a false healthy state.

Errors use a plain title, what remains safe, and a next action. A raw HTTP code
may appear in Technical details but is not the only explanation. Long jobs show
durable phases and determinate progress where the operation can calculate it;
stalled phases explain recovery choices.

## Visual system and accessibility

The interface uses a calm dark neutral surface, high-contrast text, one mint
accent for selection/primary actions, semantic warning/error colors, generous
spacing, and restrained motion. Cards, panes, meters, chips, skeletons, empty
states, status badges, and action sheets are reusable components with typed
props. The implementation does not add a component framework for this scope.

The target is WCAG 2.2 AA:

- semantic landmarks, headings, lists, tables, buttons, dialogs, and meters;
- complete keyboard operation and visible focus;
- text labels in addition to color;
- status updates announced without stealing focus;
- reduced-motion support;
- touch targets and spacing suitable for tablet use;
- focused controls are not obscured by sticky navigation or panels.

This follows Apple's guidance to integrate passive status near the item it
describes, interrupt only for consequential warnings, provide clear recovery,
and use progressive disclosure. It follows WCAG's requirements for
programmatically determinable status messages and visible, unobscured focus.

## Testing strategy

All behavioral changes use red-green-refactor.

- Rust unit tests cover telemetry parsing, deltas, bounds, queue gaps, cadence,
  and failure isolation. Client tests verify exact authenticated request shape.
- Control unit and PostgreSQL tests cover identity binding, ordering,
  idempotency, time bounds, latest-row queries, history point limits, rollups,
  retention, and scale fixtures.
- API contract tests cover authorization, SSE event IDs/reconnect, stale state,
  library joins, and capacity summaries.
- React tests use real components and complete API fixtures to cover loading,
  live updates, reconnect fallback, keyboard selection, responsive information
  order, recipe visualization, and guided action previews.
- Playwright tests cover primary Fleet and Library journeys at desktop and
  mobile viewports, accessibility semantics, and absence of document overflow.
- Existing browser, control, agent, and Rust suites remain green. The known
  macOS `/proc/self/fd` portability failure is fixed in its own tested control
  boundary rather than ignored.

## Documentation

Update operator documentation with:

- metric definitions and unified-memory interpretation on DGX Spark;
- telemetry cadence, retention, gaps, and stale thresholds;
- Fleet and Library task walkthroughs;
- install/load capacity and multi-node recommendation semantics;
- stream troubleshooting and degraded polling behavior;
- privacy/security statement listing exactly what telemetry is collected;
- local development and browser test instructions using `uv` and the existing
  Vite toolchain.

## Delivery boundaries

Implementation remains on `work/control-plane-frontend-ux` and is pushed at
coherent checkpoints. No command targets the live Tailscale application, NAS,
or Sparks. Read-only browser inspection may verify the existing site; final
visual verification uses local fixtures or a local control process.

Files under MIA recipe definitions, MIA runtime implementation, and the former
`fix/mia-worker-readiness` work are excluded. Recipe linkage uses the existing
canonical `workload.family`; recipe readiness is consumed as-is. New telemetry
agent work is isolated in a new module and generic client/control endpoints.

## Primary references

- Apple Human Interface Guidelines, Feedback:
  <https://developer.apple.com/design/human-interface-guidelines/feedback>
- Apple Human Interface Guidelines, Design principles:
  <https://developer.apple.com/design/human-interface-guidelines/design-principles>
- Apple Human Interface Guidelines, Layout:
  <https://developer.apple.com/design/human-interface-guidelines/layout>
- WCAG 2.2:
  <https://www.w3.org/TR/WCAG22/>
- MDN, Using server-sent events:
  <https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events>
- PostgreSQL, Table partitioning:
  <https://www.postgresql.org/docs/current/ddl-partitioning.html>
- PostgreSQL, Materialized views:
  <https://www.postgresql.org/docs/current/rules-materializedviews.html>
