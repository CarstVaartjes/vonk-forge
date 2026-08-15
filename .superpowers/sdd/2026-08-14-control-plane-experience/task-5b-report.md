# Task 5B report: typed Fleet projection, telemetry history, and SSE delivery

## Status and commits

Implemented Task 5B only from exact base
`955ca35478cf6231e7c9bbdf228060ac1c79a319` on
`work/control-plane-frontend-ux`.

- Source/tests commit: `00ce035c1d1c291e5ac018c187943dca4ea31d5b`
  (`feat: project and stream live fleet state`).
- Generated contract/client commit:
  `73e8a066b76061baabe5a4da0de41ae402ce0923`
  (`chore: regenerate fleet API clients`).
- Report commit: committed separately after this file was written; its immutable
  hash is returned in the final handoff because a commit cannot contain its own
  hash.

All commits are local. Nothing was pushed and no pull request was opened.

## Exact implementation scope

The source/tests commit contains exactly these eight files:

- `control/src/vonk_control/fleet_projection.py` — strict bounded DTOs,
  repository-authoritative projection, freshness/group derivation, and bounded
  telemetry history.
- `control/src/vonk_control/fleet_stream.py` — strict cursor parser and durable,
  resumable, naturally backpressured SSE generator.
- `control/src/vonk_control/api.py` — separate visual Fleet injection, history
  and stream routes, cookie-only stream authentication, and production
  composition.
- `control/src/vonk_control/operation_api.py` — stable operation IDs and OpenAPI
  browser-session security registration.
- `control/src/vonk_control/telemetry.py` — caller-session latest reads and one
  bounded sample-ID hydration query.
- `control/tests/test_fleet_projection.py` — literal projection, freshness,
  complete-group, bounds, ordering, and history assertions.
- `control/tests/test_fleet_stream.py` — literal parsing, replay/reset, hydration,
  cadence, keepalive, failure, authentication, and header assertions.
- `control/tests/test_operation_api.py` — visual/legacy endpoint split, telemetry
  API, stable registry, response-schema, and security assertions.

The generated contract/client commit contains exactly 25 files: regenerated
`control/openapi.json`, `control/web/src/api/generated.d.ts`, Python operations
`get_fleet_status.py`, `get_node_telemetry_history.py`, and
`stream_fleet_events.py`, the generated model registry plus the 18 new Fleet,
inventory, connection, reason, recipe/run presence, reservation, and telemetry
model files, and `tests/control/test_openapi_clients.py`.

No handwritten frontend, Rust, MIA recipe/runtime/readiness, worker-authority,
metrics, planning/apply, live-system, NAS, Sparks, or unrelated source was
changed. The generated TypeScript declaration is the explicitly requested
frontend artifact exception.

## Compatibility boundary

`GET /api/v1/fleet` now returns the new typed `FleetSnapshot` from an independently
injected `FleetProjection`. `GET /api/v1/nodes/status` continues to return the
legacy `FleetStatusResponse`, including its reconciliation evidence digest.
`dashboard.fleet` remains the source for `/nodes/status`, planning/apply evidence,
worker authority, reconciliation, and metrics.

Literal API and OpenAPI tests prove that `/fleet` references `FleetSnapshot` and
contains a numeric `event_cursor` without `evidence_digest`, while
`/nodes/status` references `FleetStatusResponse`, retains its evidence digest,
and has no event cursor.

The OpenAPI, Python client, and TypeScript declaration were regenerated and
committed. Generated Python `getFleetStatus` now returns `FleetSnapshot`, while
`getNodeStatuses` still returns `FleetStatusResponse`; generated history and
stream operations are present. The idempotence test regenerates twice and
asserts this DTO split plus `BrowserSession` stream security. Task 6 still owns
the handwritten frontend `visualFleet` versus `fleetEvidence`/`nodesStatus`
call-site migration.

## Projection and history contract

- `FleetProjection.read()` captures the committed Fleet event high watermark
  before reading repository or database state. The REST snapshot exposes it as
  signed-BIGINT-bounded `event_cursor`; initial/reset SSE snapshots use the same
  cursor in both the SSE `id` and nested snapshot.
- Repository `inventory/fleet.toml` membership and immutable commit are
  authoritative. Nodes are sorted and capped at 500 before state queries.
- One read transaction uses a fixed seven-query state set: agents, latest
  inventory, latest telemetry pointer/sample, selected installations, selected
  nonterminal runs, exact mapping ranks, and grouped active reservations. With
  the preceding watermark read, an instrumented snapshot executes exactly eight
  SELECT statements independent of Fleet size.
- Current operational groups are bounded to the newest 512 installations and
  newest 512 nonterminal runs; rank/member reads are bounded to 8,192 rows and
  fail closed through the typed limits.
- Telemetry is `live` at age `<=6s`, `delayed` at `>6s` through `20s`, and
  `stale` above `20s`. Agent online state and 300-second inventory freshness are
  independent signals.
- Installed and loaded groups are separate. Every group exposes expected rank
  count, present ranks, member node IDs, per-node rank/role/state,
  complete/healthy state, and a precise degraded reason. Completeness requires
  exact contiguous mapping ranks and exact `(rank, node, role)` membership;
  installed bytes/state or run/rank freshness and published route must also be
  complete.
- Only active reservations are aggregated by node and kind.
- Telemetry history validates repository membership, timezone-aware ordered
  windows, a hard 24-hour raw-history window, and `1..1500` points. It delegates
  to the Task 3 bounded history repository and returns chronological typed
  points; it never scans raw history in the Fleet snapshot.

## SSE delivery contract

- Native EventSource uses only the existing same-origin `vonk_session` cookie.
  Bearer headers and URL credentials are not accepted by the stream route.
- `Last-Event-ID` is read through `headers.getlist()` and accepts either no value
  or exactly one unsigned ASCII decimal from zero through signed BIGINT maximum.
  Duplicate, empty, signed, whitespace, Unicode-numeral, comma-joined, negative,
  and overflowing values return 400 before streaming.
- No cursor receives an initial snapshot at captured watermark `H`. A valid
  cursor replays only larger unexpired IDs. Retention gaps, cursor-ahead values,
  and missing referenced telemetry samples emit replacement snapshots at a
  newly captured committed watermark.
- Events committed after snapshot watermark capture are replayed and may safely
  duplicate state already observed by the snapshot; no committed event is lost.
- Reads are capped at 128 events and at most once per second. All referenced
  telemetry samples in a batch are hydrated with one `WHERE id IN (...)` query.
- Recipe and operation outbox payloads are intentionally sparse. Their stream
  data sets `projection_refresh_required: true` and wraps the public change; it
  does not imply that a full installed/loaded group can be recomputed as a delta.
- Frames use canonical compact single-line JSON and exact event names. The first
  frame advertises `retry: 2000`; keepalive comments are emitted by 15 seconds
  without advancing the cursor.
- The generator owns no queue or producer task. Repository methods close their
  sessions before every `await` or `yield`; cursor advancement occurs only after
  a complete yielded frame resumes. Disconnect cleanup is therefore structural,
  consumer backpressure is natural, and database failures terminate the stream.
- The route emits `text/event-stream; charset=utf-8`,
  `Cache-Control: no-cache, no-transform`, and `X-Accel-Buffering: no`.

Stable operation IDs are `getFleetStatus`, `getNodeStatuses`,
`getNodeTelemetryHistory`, and `streamFleetEvents`. OpenAPI documents only the
stream with `BrowserSession`; the visual Fleet, legacy node status, and history
routes retain `BearerAuth`.

## TDD RED/GREEN evidence

| Phase | Observed RED | GREEN proof |
|---|---|---|
| Baseline | None: focused pre-change baseline was green. | `89 passed`. |
| Projection module | Collection failed because `vonk_control.fleet_projection` did not exist. | First literal Fleet snapshot test passed. |
| Freshness | Literal warnings/freshness assertion failed. | Exact 6/20/150/300-second boundaries and independent states passed. |
| Groups | Exact multi-node fixture failed before group projection existed. | Complete/partial installation and healthy/degraded run literals passed. |
| History | Projection lacked `telemetry_history`. | Membership, cap, ordering, and invalid-window cases passed. |
| API composition | `create_app` rejected `fleet_projection`. | Typed `/fleet`, legacy `/nodes/status`, history, registry, and OpenAPI tests passed. |
| Stream module | Collection failed because `vonk_control.fleet_stream` did not exist. | Parser, replay/reset, hydration, cadence, keepalive, failure, and route tests passed. |
| Stream composition | `create_app` rejected `fleet_stream`. | Real browser-cookie route and exact response-header tests passed. |
| Review bound: groups | A 513-group fixture overflowed the response list. | Newest 512 groups are selected and the literal bound test passes. |
| Review bound: Fleet | A 501-node repository reached state queries before typed failure. | Projection now rejects before state queries; the literal query trace contains only the watermark SELECT. |
| Generated-client drift | Dedicated suite was `6 passed, 1 failed`: its old security assertion required bearer auth for every non-auth operation. | It now explicitly requires `BrowserSession` for `streamFleetEvents`, bearer for the remaining protected operations, and literal `/fleet`/`/nodes/status` response refs; `7 passed`. |

Production behavior was introduced through focused expected failures and literal
response/event/database assertions.

## Final verification

| Command | Result |
|---|---|
| Focused Fleet/event/API/telemetry/migration suite shown below | `132 passed, 1 skipped, 0 failed` in 7.59s. The skip requires Docker for PostgreSQL Fleet event ordering. |
| Adjacent package/catalog/recipe API integration slice shown below | `23 passed, 0 failed` in 3.64s. |
| `pytest tests/control/test_openapi_clients.py` | `7 passed, 0 failed` in 6.60s; generator ran twice and artifacts remained byte-identical. |
| `scripts/generate-control-clients` | Exit 0; recreated `control/.venv` with CPython 3.12.13 and generated OpenAPI, Python, and TypeScript artifacts. |
| Ruff 0.16.1 over source/tests and CI-equivalent `--force-exclude` over all changed Python | `All checks passed!`; generated Python remains excluded by repository policy. |
| `python -m compileall -q` over source/tests and generated Python | Exit 0. |
| `git diff --check` and staged `git diff --cached --check` | Exit 0; no whitespace errors. |
| Exact-base, branch, and changed-file inspection | Base and branch matched; source commit contains only the eight files listed above. |

Exact focused test command:

```bash
control/.venv/bin/pytest control/tests/test_fleet_events.py control/tests/test_fleet_events_postgres.py control/tests/test_fleet_projection.py control/tests/test_fleet_stream.py control/tests/test_operation_api.py control/tests/test_api.py control/tests/test_telemetry.py control/tests/test_admission_migration.py control/tests/test_migrations.py -q -rs --disable-warnings
```

Exact adjacent API command:

```bash
control/.venv/bin/pytest control/tests/test_package_api.py control/tests/test_catalog_api.py control/tests/test_recipe_api.py -q --disable-warnings
```

Exact generated-client commands:

```bash
scripts/generate-control-clients
control/.venv/bin/pytest tests/control/test_openapi_clients.py -q --disable-warnings
```

Exact static commands:

```bash
uvx --from ruff==0.16.1 ruff check control/src/vonk_control/fleet_projection.py control/src/vonk_control/fleet_stream.py control/src/vonk_control/telemetry.py control/src/vonk_control/api.py control/src/vonk_control/operation_api.py control/tests/test_fleet_projection.py control/tests/test_fleet_stream.py control/tests/test_operation_api.py
changed_python_files=(${(f)"$(git ls-files --modified --others --exclude-standard | rg '\.py$')"})
uvx --from ruff==0.16.1 ruff check --force-exclude "${changed_python_files[@]}"
control/.venv/bin/python -m compileall -q control/src/vonk_control/fleet_projection.py control/src/vonk_control/fleet_stream.py control/src/vonk_control/telemetry.py control/src/vonk_control/api.py control/src/vonk_control/operation_api.py control/tests/test_fleet_projection.py control/tests/test_fleet_stream.py control/tests/test_operation_api.py
control/.venv/bin/python -m compileall -q src/cluster_profiles/generated_control tests/control/test_openapi_clients.py
git diff --check
```

## Remaining concerns

- Docker is unavailable on this host, so the existing Task 5A PostgreSQL event
  ordering test was collected but not executed. Task 5B's replay behavior is
  otherwise covered with the portable durable repository contract.
- The 12 pytest warnings are pre-existing macOS temporary-directory cleanup
  warnings and are unrelated to Task 5B.
- Generated clients now describe the server contract, but Task 6 must migrate
  handwritten frontend consumers: visual state uses `/fleet`, while profiles,
  agents, and reconciliation evidence continue to use `/nodes/status`.
- The generated Python stream operation is a generic buffered HTTP helper using
  the generated authenticated-client abstraction. It is not a browser
  EventSource implementation and must not be treated as proof of cookie or
  incremental streaming behavior; the route/OpenAPI and real browser-cookie
  integration tests provide that proof.
- The full controller suite was not run; the requested focused suites plus
  adjacent API composition/integration slices were run. No live network or
  external system was accessed.
