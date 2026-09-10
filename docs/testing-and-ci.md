# Testing and CI policy

Vonk Forge keeps pull-request CI small and deterministic. GitHub Actions is a
merge gate, not the place to run every hardware, browser, Docker, or long-lived
acceptance test on every change.

## Required on every pull request

The protected `main` ruleset requires exactly these checks (verified against the
live repository ruleset, not the dated protection report under `inventory/`):

| Check | Purpose |
| --- | --- |
| `Ruff` | Lint changed Python files (or the whole tree for a release tag). |
| `Generated control clients` | Rebuild OpenAPI clients and reject generated drift. |
| `Compose integration` | Exercise the Compose and ingress boundaries. |
| `CI gate` | Aggregate the suites selected for the change. |

These checks are intentionally bounded. The change selector chooses ownership
areas, so an unrelated pull request does not start Playwright, real model
services, multi-GPU node jobs, or the full Python and web matrices.

## Local verification before requesting review

Run the fast tier while iterating, then the lane tier and the complete suite for
a release-affecting change:

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes

# Fast tier: hermetic and parallel. No Docker, PostgreSQL, cargo or host tool.
uv run --project control --frozen --with-editable . \
  pytest -q control/tests -m "not lane" -n auto --dist loadfile
uv run --python 3.12 --frozen --with pytest==9.1.1 --with pytest-xdist==3.8.0 \
  --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" \
  pytest -q tests -m "not lane" -n auto

# Repository and protocol contracts, in the standalone environment CI uses.
uv run --python 3.12 --frozen --with pytest==9.1.1 \
  --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" pytest -q tests

# Control-plane/API/worker tests, including the container and PostgreSQL lane.
uv run --project control --frozen --with-editable . pytest -q control/tests

# Browser/admin UX
npm ci --prefix control/web
npm test --prefix control/web -- --run
npm run build --prefix control/web

# Compose and ingress boundaries
uv run --frozen pytest -q deploy/compose/tests

# Release evidence and generated supply-chain inventory
scripts/verify-supply-chain --json
```

The `lane` marker is applied at collection time to any test that needs a
PostgreSQL fixture, a Rust wire probe (`*_wire_bridge.py`), a Docker build, or a
Linux host tool such as `dpkg`. `-m "not lane"` therefore stays honest as tests
are added. Run `tests` and `control/tests` in separate pytest invocations: the
two trees contain modules with the same basename.

Hardware-dependent lifecycle, thermal, NCCL, real model-quality, physical
replacement, and encryption-drill evidence stays on the designated local
hosts. It is never replaced by a green hosted smoke test.

## When the longer jobs run

Container publication and release metadata are protected by the release
environment and external gates. Ordinary pushes do not run CI; a pull request
to `main` runs only the three required checks above. Concurrency cancels
superseded pull-request runs so a stale commit does not consume another
complete check cycle.

If a change needs a longer check, run it locally and attach its bounded report
to the pull request. Use `workflow_dispatch` only when hosted evidence itself
is required.
