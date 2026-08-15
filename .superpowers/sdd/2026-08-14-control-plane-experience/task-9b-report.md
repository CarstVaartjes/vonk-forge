# Task 9B — retained telemetry history API and operator UI

## Status

DONE on `work/control-plane-frontend-ux`, based on approved Task 9A commit
`fc00cf1`. No push was performed and no live NAS, Spark, or Tailscale system
was contacted.

## Delivered

- Added required `resolution=raw|minute|fifteen-minute` telemetry history
  selection with honest 24-hour, 30-day, and 365-day windows.
- Preserved raw point shape, bounded responses at 1,500 points, and kept
  results ordered. Added typed rollup points with bucket bounds, resolution,
  source sample count, gap count, and nullable per-metric count/minimum/mean/
  maximum summaries.
- Updated Fleet projection, FastAPI route/models, OpenAPI, generated Python
  and TypeScript clients, and handwritten web wrappers.
- Added node history controls for raw and rollup windows through one year,
  plain-language resolution status, min/max range plus mean, loading,
  empty/error/retry/stale states, accessible descriptions, focus preservation,
  live-update stability, and 320px-safe responsive layout without a chart
  dependency.
- Added RED-first backend and Vitest coverage for resolution/window contracts,
  exact boundaries, nullable metrics, ordering/caps, generated contracts,
  UI semantics, retry/empty behavior, focus, and responsive overflow.
- Applied pinned Ruff formatting to all six touched Python files. The extra
  hunks are formatting-only and remain within the intended Task 9B files.

## Verification evidence

Commands were run from the repository root unless noted.

| Check | Exact command | Result |
| --- | --- | --- |
| Full Vitest | `npm test -- --run` in `control/web` | 29 files passed, 1 skipped; 183 tests passed, 1 skipped |
| Web build | `npm run build` in `control/web` | Passed; TypeScript check passed, Vite transformed 69 modules |
| Fixture browser journey | `npm exec playwright test e2e/fleet-library.spec.ts` in `control/web` | 6 passed, including desktop/mobile node history |
| Focused backend/generated clients | `uv run --project control --frozen pytest -q control/tests/test_telemetry.py control/tests/test_fleet_projection.py control/tests/test_operation_api.py tests/control/test_openapi_clients.py` | 95 passed, 33 warnings |
| Ruff lint | `uvx --from ruff==0.16.1 ruff check --force-exclude control/src/vonk_control/api.py control/src/vonk_control/fleet_projection.py control/src/vonk_control/telemetry.py control/tests/test_fleet_projection.py control/tests/test_operation_api.py control/tests/test_telemetry.py` | All checks passed |
| Ruff format | `uvx --from ruff==0.16.1 ruff format --check --force-exclude control/src/vonk_control/api.py control/src/vonk_control/fleet_projection.py control/src/vonk_control/telemetry.py control/tests/test_fleet_projection.py control/tests/test_operation_api.py control/tests/test_telemetry.py` | 6 files already formatted |
| Compile | `control/.venv/bin/python -m compileall -q control/src/vonk_control control/tests src/cluster_profiles/generated_control tests/control/test_openapi_clients.py` | Passed |
| Diff whitespace | `git diff --check` | Passed |
| Scope allowlist | Read-only status audit against the 26 intended Task 9B paths | `allowed Task 9B paths only` |
| Forbidden scope | Read-only scan for MIA/runtime/readiness paths | No matches |

Generated clients were regenerated with `scripts/generate-control-clients`.

## Warnings and limitations

The 95-test backend run emitted 33 non-failing warnings: existing generated
OpenAPI Python `SyntaxWarning: invalid decimal literal` messages and Darwin
pytest temporary-directory cleanup warnings. No test, lint, format, compile,
build, browser, whitespace, or scope check failed in the final pass.

The browser verification uses the repository's local Playwright fixture; no
live cluster telemetry was queried or changed. This patch does not include
live deployment, NAS/Spark validation, or a push.

## Committed scope

The commit contains the API/OpenAPI and generated-client changes, Fleet
projection/service and telemetry tests, node-detail/sparkline UI and tests,
fixture Playwright coverage, styles, and this report. It excludes MIA recipe or
runtime files, readiness work, Rust telemetry collection, and live systems.

## Review round 2 — ccd2bed findings

Implemented on top of `ccd2bed` without changing the public telemetry
contract, OpenAPI, or generated clients:

- Rollup history now selects the newest 1,500 buckets in descending query order
  and reverses them before returning, preserving chronological output. Tests
  cover both 30-day minute and 365-day fifteen-minute histories with 1,501
  buckets.
- Node history choices now match the 2-second agent cadence and 1,500-point
  cap: hour-scale windows use minute buckets, 7 days uses the full 672
  fifteen-minute buckets, and longer windows explicitly say they show the
  newest 1,500 fifteen-minute buckets within the selected window.
- Sparkline visible and accessible means are weighted by each rollup metric's
  count. Unequal-count coverage verifies 10% with count 1 plus 30% with count
  3 renders a 25% mean.
- A changed live telemetry sample refreshes the selected history while keeping
  the selected range and keyboard focus intact.

### Review-round RED → GREEN evidence

The new backend and UI tests were run before implementation and failed on the
four findings: oldest rollups were returned, the old raw/minute mappings were
observed, the unequal-count mean was 20% rather than 25%, and live telemetry
did not trigger a refresh. After implementation the focused review tests passed:

- `uv run --project control --frozen pytest -q control/tests/test_telemetry.py -k history_rollup_cap_keeps_newest_points_chronological`:
  2 passed.
- `npm test -- --run src/components/sparkline.test.tsx src/components/node-detail.test.tsx`:
  10 passed.

### Review-round verification

- Full Vitest: `npm test -- --run` → 29 files passed, 1 skipped; 185 tests
  passed, 1 skipped.
- Build: `npm run build` → TypeScript and Vite build passed; 69 modules
  transformed.
- Local fixture Playwright: `npm exec playwright test
  e2e/fleet-library.spec.ts` → 6 passed, including desktop/mobile history.
- Task 9B backend/generated-client suite:
  `uv run --project control --frozen pytest -q control/tests/test_telemetry.py
  control/tests/test_fleet_projection.py control/tests/test_operation_api.py
  tests/control/test_openapi_clients.py` → 97 passed, 33 warnings.
- Pinned Ruff 0.16.1 lint: passed.
- Pinned Ruff 0.16.1 format check: 6 files already formatted.
- Python compile check and `git diff --check`: passed.
- Forbidden-scope scan and intended-path audit: passed; no MIA, runtime,
  readiness, Rust, NAS, Spark, or live-system paths changed.

The requested full `control/tests` run was also attempted separately from
`tests/control`. The Task 9B-relevant subset passed, while the complete control
run ended with 1,923 passed, 64 skipped, 65 failures, and 42 errors from
pre-existing Docker-required PostgreSQL races and Darwin-only/Linux host
assumptions (`/proc`, `memfd`, ownership, runtime, signing, and development
fixture tests). Those unrelated failures were not changed.
