# Control Plane Experience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a modern, reactive Vonk Forge website that exposes near-real-time node telemetry and makes model, recipe, placement, and lifecycle state easy to understand and operate.

**Architecture:** Add a bounded authenticated telemetry lane from the Rust agent into typed PostgreSQL latest/history projections, deliver live browser updates through SSE with polling fallback, and replace administrative tables with reusable responsive cards and a Model → Recipes → Nodes workspace. Preserve existing recipe, admission, job, audit, and repository authority; compose them through new read projections instead of changing MIA runtime behavior.

**Tech Stack:** Rust/Tokio/Reqwest, Python 3.13/FastAPI/SQLAlchemy/Alembic/PostgreSQL, React 19/TypeScript/Vite, Vitest/Testing Library/Playwright, Cargo test, pytest, CSS custom properties.

## Global Constraints

- Work only on `work/control-plane-frontend-ux`; push coherent checkpoints to `origin/work/control-plane-frontend-ux`.
- Do not modify or deploy to the live NAS or any Spark node.
- Do not edit MIA recipe definitions, MIA runtime implementation, or readiness behavior.
- A model has many recipes through the existing canonical `workload.family` value.
- Every selected recipe profile has one fixed required node count.
- Never automatically unload an existing recipe; coexist when memory permits and preview explicit unload choices when it does not.
- Installation is disk-capacity-aware and protects loaded, active, in-progress, and rollback artifacts.
- Multi-node install/load is one complete placement with per-rank progress and honest partial state.
- Telemetry target cadence is two seconds; static inventory remains 60 seconds.
- Raw telemetry retention is 24 hours, one-minute rollups 30 days, and fifteen-minute rollups 365 days.
- No new required infrastructure service or frontend component framework.
- Target WCAG 2.2 AA and no document-level horizontal overflow at 360, 768, 1280, or 1920 CSS pixels.

---

### Task 1: Bound Existing Dashboard Queries

**Files:**
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/src/vonk_control/dashboard.py`
- Create: `control/migrations/versions/0022_observation_latest_index.py`
- Modify: `control/tests/test_dashboard.py`

**Interfaces:**
- Consumes: existing `Observation(node_id, kind, payload, observed_at)` rows.
- Produces: `DashboardService.fleet()` with one latest health row per fleet node and `ix_observations_kind_node_observed`.

- [ ] **Step 1: Write the failing scale test**

Add a test that inserts several health rows per node plus another observation kind, captures SQL statements, calls `fleet()`, and asserts the returned health is the newest while the health query contains a latest-row window/subquery rather than returning all rows.

```python
def test_fleet_queries_only_latest_health_per_node(repository, sessions, clock):
    # Three health rows for node A and noise for node B.
    result = DashboardService(repository, sessions, clock=clock).fleet()
    assert result["nodes"][0]["health"]["status"] == "healthy-new"
    assert len([row for row in result["nodes"] if row["id"] == NODE_A]) == 1
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `control/.venv/bin/pytest control/tests/test_dashboard.py::test_fleet_queries_only_latest_health_per_node -q`

Expected: FAIL because the current query materializes every historical health row.

- [ ] **Step 3: Implement the latest-row query and composite index**

Use `row_number() over (partition by node_id order by observed_at desc, id desc)` and select only position 1. Add a model index and an Alembic migration for `(kind, node_id, observed_at)`.

- [ ] **Step 4: Run dashboard and migration tests**

Run: `control/.venv/bin/pytest control/tests/test_dashboard.py control/tests/test_migrations.py -q`

Expected: PASS.

- [ ] **Step 5: Commit and push**

```bash
git add control/src/vonk_control/models.py control/src/vonk_control/dashboard.py control/migrations/versions/0022_observation_latest_index.py control/tests/test_dashboard.py
git commit -m "perf: bound fleet health projection"
git push
```

### Task 2: Establish the Responsive Visual System and Navigation

**Files:**
- Create: `control/web/src/components/icons.tsx`
- Create: `control/web/src/components/status-pill.tsx`
- Create: `control/web/src/components/meter.tsx`
- Create: `control/web/src/components/app-shell.tsx`
- Create: `control/web/src/components/app-shell.test.tsx`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/styles.css`
- Modify: `control/web/src/test-setup.ts`

**Interfaces:**
- Produces: `AppShell`, `StatusPill`, `Meter`, and code-native SVG icons shared by Fleet and Library.
- Preserves: existing route URLs and authenticated logout behavior.

- [ ] **Step 1: Write failing shell accessibility and navigation tests**

```tsx
test("groups primary tasks without hiding administrative routes", async () => {
  render(<App api={apiFixture}/>);
  expect(screen.getByRole("link", {name: "Fleet"})).toHaveAttribute("aria-current", "page");
  expect(screen.getByRole("link", {name: "Library"})).toBeVisible();
  expect(screen.getByRole("button", {name: "Open system navigation"})).toBeVisible();
});
```

Name the break: restoring ten equal top-level links or removing the accessible mobile control must fail.

Add a real in-memory `Storage` implementation to the shared test setup only
when jsdom does not expose one. The baseline `updates.test.tsx` cleanup calls
`localStorage.clear()`; its absence currently prevents Testing Library cleanup
and makes later tests render duplicate pages. Add a regression assertion that
`setItem`, `getItem`, `removeItem`, and `clear` provide browser-equivalent
string semantics.

- [ ] **Step 2: Run the test and verify RED**

Run: `cd control/web && npm test -- src/components/app-shell.test.tsx src/pages/updates.test.tsx --run`

Expected: FAIL because `AppShell` and Library do not exist.

- [ ] **Step 3: Implement components and design tokens**

Create a sticky desktop sidebar, compact mobile header, grouped Activity/System disclosure, reusable semantic badges/meters, dark neutral surfaces, mint accent, visible focus, reduced-motion rules, and responsive content widths. Use inline code-native SVGs with `aria-hidden="true"`; do not add raster assets or a UI dependency.

- [ ] **Step 4: Run component tests and production build**

Run: `cd control/web && npm test -- src/components/app-shell.test.tsx src/pages/updates.test.tsx --run && npm run build`

Expected: PASS with no TypeScript or Vite warnings.

- [ ] **Step 5: Commit and push**

```bash
git add control/web/src/app.tsx control/web/src/components control/web/src/styles.css control/web/src/test-setup.ts
git commit -m "feat: add responsive control plane shell"
git push
```

### Task 3: Add Typed Telemetry Persistence and Ingestion

**Files:**
- Create: `control/src/vonk_control/telemetry.py`
- Modify: `control/src/vonk_control/models.py`
- Modify: `control/src/vonk_control/agent_api.py`
- Modify: `control/src/vonk_control/api.py`
- Create: `control/migrations/versions/0023_node_telemetry.py`
- Create: `control/tests/test_telemetry.py`
- Modify: `control/tests/test_agent_api.py`
- Modify: `control/tests/test_admission_migration.py`

**Interfaces:**
- Produces: `TelemetrySampleInput`, `TelemetryRepository.record_batch(node_id, samples)`, `latest(node_ids)`, `history(node_id, start, end, maximum_points)`, and `POST /agent/v1/telemetry`.
- Wire contract: schema version, boot ID UUID, monotonic sequence, observed time, CPU/load, memory, disk, accelerator, temperature/power, network rates, and `gap_samples`.

- [ ] **Step 1: Write failing repository ordering tests**

```python
def test_newer_telemetry_replaces_latest_and_replay_does_not(telemetry):
    telemetry.record_batch(NODE_A, (sample(sequence=4), sample(sequence=5)))
    telemetry.record_batch(NODE_A, (sample(sequence=4),))
    assert telemetry.latest((NODE_A,))[NODE_A].sequence == 5
    assert [item.sequence for item in telemetry.history(NODE_A, START, END, 1500)] == [4, 5]
```

Add separate tests for a new boot ID, future/stale time, NaN/negative/range violations, duplicate samples, batch size 17, history point cap 1,500, and transactional latest/history writes.

- [ ] **Step 2: Run repository tests and verify RED**

Run: `control/.venv/bin/pytest control/tests/test_telemetry.py -q`

Expected: collection failure because the telemetry module does not exist.

- [ ] **Step 3: Implement typed models, repository, and migration**

Use a latest table keyed by node and pointing at its current sample, a sample
table unique on `(node_id, boot_id, sequence)`, typed nullable metric columns,
bounded JSON details, and composite history indexes. For one boot, sequence and
observation time increase together; a new boot may reset sequence but advances
latest only when its observation is newer. Validate finite numeric ranges and
cross-field free/total relationships before opening a transaction.

- [ ] **Step 4: Write failing authenticated API tests**

Verify the body contains no node identity, certificate identity owns every
row, unauthorized requests fail before parsing, stale/future samples return
422, batches above 16 return 422, bodies above 64 KiB return 413 even without a
truthful `Content-Length`, duplicate JSON keys return 422, and valid ordered
samples return 204. Use a five-minute past and 30-second future acceptance
window.

- [ ] **Step 5: Implement and verify the agent endpoint**

Run: `control/.venv/bin/pytest control/tests/test_telemetry.py control/tests/test_agent_api.py -q`

Expected: PASS.

- [ ] **Step 6: Commit and push**

```bash
git add control/src/vonk_control/telemetry.py control/src/vonk_control/models.py control/src/vonk_control/agent_api.py control/src/vonk_control/api.py control/migrations/versions/0023_node_telemetry.py control/tests/test_telemetry.py control/tests/test_agent_api.py control/tests/test_admission_migration.py
git commit -m "feat: ingest bounded node telemetry"
git push
```

### Task 4: Collect and Report Rust Agent Telemetry

**Files:**
- Create: `rust/crates/vonk-agent/src/telemetry.rs`
- Modify: `rust/crates/vonk-agent/src/lib.rs`
- Modify: `rust/crates/vonk-agent/src/client.rs`
- Modify: `rust/crates/vonk-agent/src/main.rs`
- Create: `rust/crates/vonk-agent/tests/telemetry.rs`

**Interfaces:**
- Produces: `TelemetryCollector::sample(previous) -> Result<TelemetrySample, TelemetryError>` and `AgentHttpClient::report_telemetry(&[TelemetrySample])`.
- Isolation: telemetry retries do not delay claim/result traffic and do not touch `oci.rs`, recipe runtime, or readiness code.

- [ ] **Step 1: Write failing parser and delta tests**

Use literal `/proc/stat`, `/proc/loadavg`, `/proc/meminfo`, `/proc/net/dev`, statvfs, and `nvidia-smi` fixtures. Assert exact utilization/rate values, counter reset behavior, GB10 unified-memory fallback, missing optional values, and bounded text.

```rust
#[test]
fn counter_reset_yields_unknown_rate_instead_of_underflow() {
    let sample = collect_with(previous_counters(900), current_counters(100));
    assert_eq!(sample.network_receive_bytes_per_second, None);
}
```

- [ ] **Step 2: Run and verify RED**

Run: `cargo test -p vonk-agent --test telemetry`

Expected: FAIL because `vonk_agent::telemetry` does not exist.

- [ ] **Step 3: Implement the bounded collector**

Reuse the existing safe process runner and inventory parsing style. One NVIDIA query has a 10-second hard bound. Persist only boot ID and sequence required for monotonic reporting; retain at most 15 unsent samples in memory and set `gap_samples` when oldest samples are dropped.

- [ ] **Step 4: Write failing client and loop-isolation tests**

Assert exact path/content type/body, maximum batch 16, a two-second cadence under paused Tokio time, and continued claim attempts after retryable telemetry failure.

- [ ] **Step 5: Implement reporting and verify Rust suites**

Run: `cargo test -p vonk-agent`

Expected: PASS.

- [ ] **Step 6: Commit and push**

```bash
git add rust/crates/vonk-agent/src/telemetry.rs rust/crates/vonk-agent/src/lib.rs rust/crates/vonk-agent/src/client.rs rust/crates/vonk-agent/src/main.rs rust/crates/vonk-agent/tests/telemetry.rs
git commit -m "feat: report near-real-time node telemetry"
git push
```

### Task 5: Add Fleet Projection, History, and Live Delivery

**Files:**
- Create: `control/src/vonk_control/fleet_projection.py`
- Create: `control/src/vonk_control/fleet_stream.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/operation_api.py`
- Create: `control/tests/test_fleet_projection.py`
- Create: `control/tests/test_fleet_stream.py`
- Modify: `control/tests/test_operation_api.py`

**Interfaces:**
- Produces: `FleetProjection.read()`, `GET /api/v1/fleet`, `GET /api/v1/nodes/{node_id}/telemetry`, and authenticated `GET /api/v1/fleet/stream` SSE.
- SSE events: initial `fleet-snapshot`, then `node-telemetry`, `recipe-state`, and `operation-state`, each with numeric database event ID.

- [ ] **Step 1: Write failing projection tests**

Build complete latest telemetry, inventory, installation, run, rank, agent, and repository fixtures. Assert live/delayed/stale/offline thresholds, installed versus loaded labels, complete versus partial multi-node state, and human display values.

- [ ] **Step 2: Run and verify RED**

Run: `control/.venv/bin/pytest control/tests/test_fleet_projection.py -q`

Expected: FAIL because `FleetProjection` does not exist.

- [ ] **Step 3: Implement bounded latest-state joins**

Use fixed latest/subquery projections; no history scan. Keep capacity-sensitive fields timestamped. Return stable response models suitable for complete frontend fixtures.

- [ ] **Step 4: Write failing history and SSE tests**

Assert at most 1,500 points, bucket selection, authenticated stream, correct `text/event-stream`, initial snapshot, keepalive, ordered IDs, `Last-Event-ID` resume, and disconnect cleanup.

- [ ] **Step 5: Implement history and SSE delivery**

SSE may perform a bounded latest-event query once per second; it must not hold a database transaction open between yields. Emit a keepalive every 15 seconds and rely on EventSource retry semantics.

- [ ] **Step 6: Run API/projection tests and commit**

Run: `control/.venv/bin/pytest control/tests/test_fleet_projection.py control/tests/test_fleet_stream.py control/tests/test_operation_api.py -q`

```bash
git add control/src/vonk_control/fleet_projection.py control/src/vonk_control/fleet_stream.py control/src/vonk_control/api.py control/src/vonk_control/operation_api.py control/tests/test_fleet_projection.py control/tests/test_fleet_stream.py control/tests/test_operation_api.py
git commit -m "feat: project and stream live fleet state"
git push
```

### Task 6: Build the Reactive Fleet Page

**Files:**
- Create: `control/web/src/components/sparkline.tsx`
- Create: `control/web/src/components/node-card.tsx`
- Create: `control/web/src/components/node-detail.tsx`
- Create: `control/web/src/hooks/use-fleet-stream.ts`
- Create: `control/web/src/pages/fleet.test.tsx`
- Modify: `control/web/src/pages/fleet.tsx`
- Modify: `control/web/src/api/types.ts`
- Modify: `control/web/src/api/client.ts`
- Modify: `control/web/src/styles.css`

**Interfaces:**
- Consumes: Fleet snapshot, telemetry history, and SSE events from Task 5.
- Produces: responsive Fleet cards, cluster summary, node detail, sparklines, connection state, and 10-second polling fallback.

- [ ] **Step 1: Write failing visible-behavior tests**

```tsx
test("updates a node card without replacing its focused control", async () => {
  render(<FleetPage api={api} stream={stream}/>);
  await user.click(screen.getByRole("button", {name: "View Spark 1 details"}));
  stream.emit(telemetryEvent({gpu_utilization_percent: 73}));
  expect(screen.getByText("73%", {selector: "[data-metric='gpu']"})).toBeVisible();
  expect(screen.getByRole("button", {name: "Close Spark 1 details"})).toHaveFocus();
});
```

Add tests for live/delayed/stale labels, loaded and installed recipes, multi-node degraded warning, stream reconnect, polling fallback, empty state, API error recovery, and status live-region text.

- [ ] **Step 2: Run and verify RED**

Run: `cd control/web && npm test -- src/pages/fleet.test.tsx --run`

Expected: FAIL because cards and stream hook do not exist.

- [ ] **Step 3: Implement stream reducer and components**

Keep event reconciliation in the hook, metric formatting in pure helpers, and visual rendering in focused components. Sparklines are accessible SVGs with a text summary; decorative paths are hidden from assistive technology. Preserve the exact update notice safety semantics.

- [ ] **Step 4: Verify component suite and build**

Run: `cd control/web && npm test -- src/pages/fleet.test.tsx src/components/app-shell.test.tsx --run && npm run build`

Expected: PASS.

- [ ] **Step 5: Commit and push**

```bash
git add control/web/src/components control/web/src/hooks control/web/src/pages/fleet.tsx control/web/src/pages/fleet.test.tsx control/web/src/api control/web/src/styles.css
git commit -m "feat: build reactive fleet dashboard"
git push
```

### Task 7: Add the Model–Recipe–Node Library Projection

**Files:**
- Create: `control/src/vonk_control/library_projection.py`
- Create: `control/src/vonk_control/library_api.py`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/operation_api.py`
- Create: `control/tests/test_library_projection.py`
- Create: `control/tests/test_library_api.py`

**Interfaces:**
- Produces: `GET /api/v1/library`, `GET /api/v1/library/recipes/{recipe_id}`, and placement recommendation summaries.
- Uses: repository model definitions, local recipe revisions, `workload.family`, existing mappings/builds/installations/runs/reservations, and fresh inventory/telemetry.

- [ ] **Step 1: Write failing relationship tests**

Assert one model returns several recipes, unlinked recipes remain visible, immutable revision selection is deterministic, and technical IDs do not replace display names.

- [ ] **Step 2: Write failing placement tests**

Assert a two-node recipe returns only complete compatible pairs; recommendation ranking accounts for install state, available disk/memory, existing loaded recipes, fabric, stale telemetry, and reservations; insufficient capacity includes exact reasons without proposing implicit unloads.

- [ ] **Step 3: Run and verify RED**

Run: `control/.venv/bin/pytest control/tests/test_library_projection.py control/tests/test_library_api.py -q`

Expected: FAIL because the projection/API do not exist.

- [ ] **Step 4: Implement read-only projection and API**

Keep recommendation explanations as typed reason codes plus human copy. Use existing admission services for authoritative action previews; the Library projection only ranks and explains.

- [ ] **Step 5: Verify tests and OpenAPI parity**

Run: `control/.venv/bin/pytest control/tests/test_library_projection.py control/tests/test_library_api.py control/tests/test_operation_api.py -q`

Expected: PASS.

- [ ] **Step 6: Commit and push**

```bash
git add control/src/vonk_control/library_projection.py control/src/vonk_control/library_api.py control/src/vonk_control/api.py control/src/vonk_control/operation_api.py control/tests/test_library_projection.py control/tests/test_library_api.py control/tests/test_operation_api.py
git commit -m "feat: add model recipe node library projection"
git push
```

### Task 8: Build Library, Visual Recipe Details, and Guided Actions

**Files:**
- Create: `control/web/src/pages/library.tsx`
- Create: `control/web/src/pages/library.test.tsx`
- Create: `control/web/src/components/model-list.tsx`
- Create: `control/web/src/components/recipe-list.tsx`
- Create: `control/web/src/components/placement-panel.tsx`
- Create: `control/web/src/components/recipe-overview.tsx`
- Create: `control/web/src/components/action-sheet.tsx`
- Modify: `control/web/src/pages/recipe-editor.tsx`
- Modify: `control/web/src/pages/recipe-editor.test.tsx`
- Modify: `control/web/src/app.tsx`
- Modify: `control/web/src/api/types.ts`
- Modify: `control/web/src/api/client.ts`
- Modify: `control/web/src/styles.css`

**Interfaces:**
- Consumes: Library APIs from Task 7 and existing recipe install/run/stop/uninstall preview and apply APIs.
- Produces: URL-addressable `/library`, `/library/models/{model}`, and `/library/recipes/{recipe}` experiences.

- [ ] **Step 1: Write failing three-pane interaction tests**

Assert one model exposes several recipes, selection updates the URL and next pane only on activation, complete node groups are ranked, partial installation is explicit, technical details are collapsed, and keyboard focus remains predictable.

- [ ] **Step 2: Write failing recipe visualization/editor tests**

Assert topology, resource totals, provenance, capabilities, compatibility, and fleet state are visible without opening JSON; Advanced raw editing/upload remains available; invalid JSON preserves the last valid preview and maps errors to a field path.

- [ ] **Step 3: Write failing action-sheet tests**

Assert install/load previews show complete groups and capacity, coexistence is preferred, insufficient memory names optional explicit unloads, multi-node progress stays grouped, and uninstall requires a human-readable impact confirmation.

- [ ] **Step 4: Run and verify RED**

Run: `cd control/web && npm test -- src/pages/library.test.tsx src/pages/recipe-editor.test.tsx --run`

Expected: FAIL for missing Library and visual recipe behavior.

- [ ] **Step 5: Implement the workspace and progressive disclosure**

At desktop widths use three independently scrollable panes with persistent selected rows; below 900px use a drill-down list preserving browser history. Do not draw decorative connector lines that obscure selection or complicate responsive behavior.

- [ ] **Step 6: Verify browser tests and build**

Run: `cd control/web && npm test --run && npm run build`

Expected: PASS with no console errors or warnings.

- [ ] **Step 7: Commit and push**

```bash
git add control/web/src
git commit -m "feat: add visual model recipe node library"
git push
```

### Task 9: Retention, Documentation, and End-to-End Acceptance

**Files:**
- Create: `control/src/vonk_control/telemetry_maintenance.py`
- Create: `control/tests/test_telemetry_maintenance.py`
- Create: `control/web/e2e/fleet-library.spec.ts`
- Create: `docs/operations/control-website.md`
- Create: `docs/runbooks/telemetry.md`
- Modify: `README.md`
- Modify: relevant worker scheduling file discovered from `control/src/vonk_control/worker.py`

**Interfaces:**
- Produces: bounded rollup/prune maintenance, operator/runbook documentation, and full local browser acceptance coverage.

- [ ] **Step 1: Write failing rollup and pruning tests**

Use a fixed clock and literal samples to assert min/mean/max buckets, idempotent reruns, late samples, bounded deletion, and exact 24-hour/30-day/365-day boundaries.

- [ ] **Step 2: Implement maintenance and verify RED → GREEN**

Run: `control/.venv/bin/pytest control/tests/test_telemetry_maintenance.py -q`

Expected after implementation: PASS.

- [ ] **Step 3: Add operator documentation**

Document every metric, GB10 unified-memory interpretation, collection/privacy boundary, stale thresholds, retention, stream fallback, Fleet/Library workflows, multi-node semantics, and local `uv`/Vite setup with exact commands.

- [ ] **Step 4: Add Playwright journeys**

Cover Fleet live update/reconnect, node detail, Library model/recipe selection, two-node recommendation, visual recipe detail, action preview cancellation, mobile drill-down, keyboard operation, and document overflow at required widths.

- [ ] **Step 5: Run complete verification**

```bash
control/.venv/bin/pytest control/tests -q
cargo test -p vonk-agent
cd control/web && npm test --run && npm run build && npm run test:e2e
git diff --check
```

Expected: all applicable suites pass. Any platform-specific skipped test has a documented reason; failures are diagnosed and fixed, not omitted.

- [ ] **Step 6: Inspect the rendered local site**

Use the connected Chrome/browser tooling against a local fixture-backed server. Verify no console errors, readable hierarchy, stable live updates, responsive layouts, keyboard focus, and all required states. Capture desktop and mobile screenshots for final review.

- [ ] **Step 7: Commit, push, and request review**

```bash
git add control/src/vonk_control/telemetry_maintenance.py control/tests/test_telemetry_maintenance.py control/web/e2e/fleet-library.spec.ts docs/operations/control-website.md docs/runbooks/telemetry.md README.md
git commit -m "docs: complete control website operations and acceptance"
git push
```
