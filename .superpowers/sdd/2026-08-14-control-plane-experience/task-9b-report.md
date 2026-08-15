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
