# Task 6 report — Reactive Fleet frontend

Status: complete locally on `work/control-plane-frontend-ux`. Nothing was pushed and no pull request was opened.

Exact base: `e60f6fa944960d4c9c49f559d96c968e6d9621e5`.

Implementation commit: `e6f531a8f92394a1a0ad2aaf5c92f7b6cf29ab8a` (`feat: add reactive Fleet frontend`).

## Scope

The implementation is confined to `control/web` source, frontend tests, and local-only Playwright fixtures. It does not change controller Python, Rust, generated OpenAPI declarations, MIA/runtime/readiness code, migrations, NAS state, Tailscale, or any Spark.

The source commit contains 32 frontend files with 2,226 insertions and 92 deletions. No dependency or lockfile change remains.

## Deliberate API boundary

The handwritten API now makes visual projection and reconciliation evidence structurally and nominally distinct:

| Method / alias | Endpoint | Contract / consumer |
|---|---|---|
| `visualFleet()` / `VisualFleetSnapshot` | `GET /api/v1/fleet` | Reactive Fleet page only; generated `FleetSnapshot` with `event_cursor` and repository node projection. |
| `nodeStatuses()` / `FleetEvidenceResponse` | `GET /api/v1/nodes/status` | Explicit legacy node-status read; generated `FleetStatusResponse`. |
| `fleetEvidence()` / `FleetEvidenceResponse` | Delegates to `nodeStatuses()` | Profiles, Agents, and reconciliation preview/apply integrity checks that require `commit` and `evidence_digest`. |
| `nodeTelemetryHistory()` / `TelemetryHistory` | `GET /api/v1/nodes/{node_id}/telemetry` | Bounded detail-sheet history only. |

Profiles, Agents, reconciliation checks, update/Fleet tests, auth/shell fixtures, and the admin Playwright fixture were migrated. No `FleetSnapshot` is passed into an evidence-digest path. Client tests assert the exact visual, evidence, node-status, and telemetry-history URLs and query parameters.

## Reactive state and reconciliation

`fleet-stream-state.ts` is a pure reducer. `use-fleet-stream.ts` owns effects and transport lifecycle.

- The page opens native same-origin `EventSource("/api/v1/fleet/stream")`.
- SSE IDs are accepted only as nonnegative safe numeric cursors.
- Initial and reset snapshots replace state; an authoritative `cursor-ahead` reset may lower the cursor.
- Requested REST snapshots are rejected when their committed `event_cursor` is lower than current streamed state.
- Node telemetry patches one stable keyed node and ignores stale or duplicate increments.
- Sparse `recipe-state` and `operation-state` signals advance cursor state and coalesce one projection refresh after 75 ms; no rank-local multi-node state is inferred.
- Stream errors expose reconnecting state and start one ten-second polling interval. Stream recovery stops the poll and reports live state.
- A one-second clock tick recomputes freshness from sample timestamps, so silent nodes age from live to delayed to stale without receiving an event.
- Retry creates a fresh transport generation.
- Unmount and retry close the EventSource, remove all listeners, clear freshness/poll/coalescing timers, abort all active REST work, and prevent late completion updates.

Focused tests prove initial/reset reconciliation, backward reset, duplicate suppression, sparse refresh coalescing, poll/recovery behavior, stale in-flight poll rejection, no-EventSource fallback, retry generation, and zero timers/listeners plus aborted requests after cleanup.

## Fleet experience

The Fleet home uses the existing dark neutral/mint shell and shared `StatusPill`, with code-native presentation and no chart or icon dependency.

- The summary reports live, delayed, stale, and offline nodes, live unified memory, unique loaded recipe runs, repository node count, active attention count, repository commit prefix, event cursor, and visible connecting/live/reconnecting/polling state.
- Repository nodes are rendered as stable keyed cards. Offline remains independent of telemetry freshness.
- Cards expose accelerator/performance state, GPU utilization, unified/host/GPU memory, disk, CPU/load, temperature, power, network receive/transmit rates, relative update age, absolute UTC observation time, and explicit offline reason.
- Missing and non-finite metrics render `Not reported`; they are never converted to zero.
- Loaded/running and installed recipe groups are distinct accessible regions.
- Multi-node evidence uses the complete backend group projection: `Complete`/`Partial` installation rank counts and `Healthy`/`Degraded` running rank counts plus the bounded reason vocabulary.
- Certificate and clock/offline reason labels are textual. Every status combines text with color.
- The non-modal complementary detail sheet preserves card focus/state during stream patches, autofocuses Close on entry, returns focus to the trigger on close, and becomes an in-flow section on narrow layouts.
- Overview, recipe evidence, active events, and technical identifiers are separated. History choices are fixed to 1, 6, and 24 hours with 360, 720, and 1,440 maximum points respectively, below the generated 1,500-point API ceiling.
- Dependency-free SVG sparklines preserve null gaps and provide `<title>`, `<desc>`, and visible latest/range/sample-count summaries. A range change clears the prior range until the new response arrives.
- The summary live region announces the first nonempty state immediately and coalesces further announcements to at most one per five seconds.
- Reduced-motion rules cover the only loading animation. The deferred decorative-icon issue is fixed by making `aria-hidden="true"` and `focusable="false"` non-overridable by callers.

Responsive styles use intrinsic grids and `minmax(0, …)` containment. The local fixture proves exact document width at 360, 768, 1280, and 1920 pixels. Detail is in flow at phone width and sticky at wide desktop width; the wide node grid has multiple columns. The fixture also captures browser console warnings/errors and page errors and observed none.

## Strict TDD evidence

Baseline:

| Phase | Evidence |
|---|---|
| Existing frontend baseline | 17 passed and 1 skipped Vitest files; 80 passed and 1 skipped tests. |
| Expected contract RED | `npm run build` failed only at the old `fleet(): FleetStatusResponse` implementation because generated `/fleet` now returns `FleetSnapshot` without legacy `commit`/`evidence_digest`. This matched the controller-confirmed baseline. |

Principal RED/GREEN cycles:

| Surface | RED evidence | GREEN evidence |
|---|---|---|
| API split | Client and affected-page tests required distinct visual/evidence methods and exact paths; the old method could not typecheck against generated `/fleet`. | API and migrated consumer slice passed; production build became clean. |
| Freshness/domain | Boundary, certificate/offline, null-metric, group-state, and summary tests preceded `fleet.ts`. | 8 domain tests passed in the focused cycle. |
| Reducer | Cursor ordering, backward reset, keyed telemetry patch, sparse refresh, and reconnect cases failed before reducer implementation. | 5 reducer tests passed. |
| Stream hook | Native EventSource, coalescing, polling, recovery, stale poll, retry, and cleanup cases preceded effects. A later no-EventSource case reproduced `EventSource is not a constructor`. | 7 hook tests passed, including unavailable-EventSource fallback. |
| Announcements | Coalescing and first-nonempty behavior were specified before the hook. | 2 announcement tests passed with timer cleanup. |
| Sparklines/cards/detail | Null-gap paths, accessible summaries, complete telemetry, installed/loaded separation, degraded groups, bounded ranges, errors, focus, and abort behavior were asserted before components. | Component tests passed. A review RED additionally proved stale previous-range charts remained visible; clearing history during a range request made all 3 detail tests pass. |
| Fleet integration | Summary, stream status, keyed focus preservation, timestamp-only aging, initial error/retry, and empty state assertions failed against the old page. | All 4 Fleet page integration tests passed. |
| Responsive browser | The local fixture initially failed because Chromium was absent, then exposed authentication fixture drift, a strict locator ambiguity, missing sticky detail CSS, and 360/768 overflow from the off-canvas hero glow. | After installing the matching test browser and fixing the local fixture/layout, both Fleet scenarios passed at all four widths with no browser-console warnings. |
| Decorative icons | A caller override produced `aria-hidden="false"`. | Attribute order now enforces decorative semantics and the regression passes. |

## Final verification

All commands were run from `control/web` unless noted.

| Command | Exact result |
|---|---|
| Focused affected Vitest slice | 15 files passed; 95 tests passed. |
| `npm test -- src/components/node-detail.test.tsx --run` after final review fix | 1 file passed; 3 tests passed. |
| `npm test -- --run` | 25 files passed, 1 skipped; 115 tests passed, 1 skipped; exit 0. |
| `npm run build` | `tsc --noEmit` and Vite production build succeeded; 53 modules transformed; exit 0. |
| `npm run test:e2e` | 5 Playwright tests passed using local routes only; exit 0. |
| `git diff --check` before source commit | Exit 0. |
| `git diff --cached --check` before source commit | Exit 0. |
| Base/branch check | `HEAD` and merge-base were exactly `e60f6fa944960d4c9c49f559d96c968e6d9621e5` before the implementation commit, on `work/control-plane-frontend-ux`. |

`package.json` has no lint script. The available TypeScript check is `tsc --noEmit`, which ran successfully as the first half of `npm run build`.

The one skipped Vitest is the pre-existing opt-in `admin-equivalence.live.test.tsx` live API crossing test. It remains disabled unless its explicit environment gate is enabled, consistent with the instruction not to contact live infrastructure.

## Concerns and limitations

1. Playwright's process output includes the existing Node warning that `NO_COLOR` is ignored while `FORCE_COLOR` is set. This is outside browser page execution; the Fleet fixture separately captured zero browser `warning`/`error` console messages and zero page errors.
2. The local SSE fixture sends a valid initial snapshot and then closes, which exercises visible reconnect state without holding a server connection open. Unit tests provide deterministic open/error/poll/recovery and cleanup coverage; no live controller, NAS, Tailscale, or Spark was contacted.
3. History is intentionally capped at 24 hours and 1,440 points for Task 6. Longer rollup windows remain outside this task and are not implied by the UI.
