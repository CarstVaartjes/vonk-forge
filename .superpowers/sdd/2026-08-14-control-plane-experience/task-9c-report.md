# Task 9C — operator documentation and local acceptance

## Scope

Added operator-facing control-plane operations and telemetry runbooks, local
fixture acceptance instructions, and README navigation. Documentation covers
metrics and units, missing values, two-second reporting intent, freshness and
SSE fallback, history resolutions/retention, Fleet/Library workflows,
multi-node rank groups, disk/memory admission, previews, retry/partial failure,
and no-implicit-unload/no-implicit-stop behavior.

## Verification

The Task 9A/9B fixture-backed acceptance evidence remains the authoritative
local UI baseline:

- backend/generated-client suites: 97 passed;
- Vitest: 185 passed, 1 skipped;
- fixture Playwright: 9 passed, including Fleet, history, desktop/mobile
  reflow, Library drill-down, action preview/retry, Advanced JSON recovery,
  and empty/error journeys;
- web build: passed;
- pinned Ruff lint and format, Python compile, and `git diff --check`: passed;
- forbidden-scope scan for the Task 9C/control-plane slice: no MIA,
  runtime/readiness, Rust telemetry, live NAS, Spark, or Tailscale files. The
  branch baseline also contains pre-existing near-real-time Rust telemetry
  commits (`b70f724`, `6f88e8e`, `1d95dea`) from before the control-plane merge;
  this work did not modify those files.

The full control test suite is not a valid Darwin-local completion gate: its
remaining failures/errors are pre-existing Linux-only and Docker-required
tests. Operators should run the complete suite on the Docker-capable Linux
review host before integration. No live system was used for acceptance.
