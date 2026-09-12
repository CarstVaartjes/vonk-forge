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

# Rust wire structures must still match the Pydantic schemas. Typify generation
# is a text comparison, so this needs no cargo and runs on any host.
uv run --project control --frozen --with-editable . \
  python scripts/generate-agent-wire --check

# Compose and ingress boundaries. The control environment, not the root one,
# because the container config imports pydantic. TMPDIR must be short on macOS:
# see the note below.
TMPDIR=/tmp/vk uv run --project control --frozen --with-editable . \
  pytest -q deploy/compose/tests

# Release evidence and generated supply-chain inventory
scripts/verify-supply-chain --json
```

The `lane` marker is applied at collection time to any test that needs a
PostgreSQL fixture, a Rust wire probe (`*_wire_bridge.py`), a Docker build, or a
Linux host tool such as `dpkg`. `-m "not lane"` therefore stays honest as tests
are added. Run `tests` and `control/tests` in separate pytest invocations: the
two trees contain modules with the same basename.

The PostgreSQL, step-ca, and security-boundary lane tests do not need a Spark or
a release: with OrbStack running they execute locally in about a minute each and
are worth running before claiming a Controller change works.

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
UV_CACHE_DIR=/private/tmp/vonk-forge-control-cache uv run --project control \
  --frozen --with-editable . pytest -q -m lane -n auto --dist loadfile \
  control/tests/test_catalog_documents_postgres.py \
  control/tests/test_agent_jobs_postgres.py \
  control/tests/test_agent_job_lock_order_postgres.py \
  control/tests/test_run_switch_postgres.py \
  control/tests/test_telemetry_postgres.py \
  control/tests/test_recipe_operations.py \
  control/tests/test_step_ca.py control/tests/security
```

Only the Rust wire probes (`*_wire_bridge.py`) still require a Linux `cargo`
build, so they stay in CI or a Linux container. The Compose lane likewise runs
locally in OrbStack, but on macOS it needs `TMPDIR` pointed at a short directory:
several tests bind a Unix socket under `tmp_path`, and the default
`/private/var/folders/...` prefix plus a long test name exceeds the 104-byte
`sun_path` limit, failing the whole Tailscale group with `OSError: AF_UNIX path
too long`.

Hardware-dependent lifecycle, thermal, NCCL, real model-quality, physical
replacement, and encryption-drill evidence stays on the designated local
hosts. It is never replaced by a green hosted smoke test.

## What earns a test

The suites are a merge gate, so every test costs review time on every future
change. A test earns its place by catching a defect that a reviewer would
otherwise have to reason about by hand:

- **Name the defect.** Before keeping a test, say which wrong implementation it
  fails on. If nothing can make it fail except deleting the code it calls, it is
  ceremony, not coverage.
- **Prefer the boundary to the middle.** The valuable cases are the ones where
  behaviour changes: an empty or absent value, a malformed or hostile one, the
  first and last accepted element of a range, a replayed or expired grant, a
  permission that must be refused. A test that only walks the happy path already
  covered elsewhere is duplication.
- **One authority per fact.** Do not re-assert a constant, a field list, a
  literal set, or a schema shape that another test already pins; two copies
  drift, and the drift costs more than the second copy ever catches. This is why
  the contract tests consume the canonical Pydantic model instead of duplicated
  key lists.
- **A bug fix ships with the test that fails without it.** Write the test,
  confirm it fails against the old behaviour, then fix. A regression test that
  was never seen failing is an assumption.
- **Test the real seam.** Exercise the actual serialize/store/load path, the
  real marker set, the real CLI parser, the real runner. A stub that replaces
  the very thing under review cannot catch a fault in it — a stubbed
  `QualificationRunner` hid a dead `apply()` path for exactly that reason.
- **Keep the fast tier fast and hermetic.** A host-dependent test belongs in the
  lane tier, which the collection-time marker applies automatically, rather than
  being skipped or made conditional. The fast tier should stay under a couple of
  minutes so it is genuinely the thing you run while iterating.

## When the longer jobs run

Container publication and release metadata are protected by the release
environment and external gates. Ordinary pushes do not run CI; a pull request
to `main` runs only the three required checks above. Concurrency cancels
superseded pull-request runs so a stale commit does not consume another
complete check cycle.

If a change needs a longer check, run it locally and attach its bounded report
to the pull request. Use `workflow_dispatch` only when hosted evidence itself
is required.

## Dependency acquisition failures

`scripts/retry-dependency-fetch` permits only `uv sync`, `skopeo inspect`, and
`docker buildx imagetools inspect`. The Controller and wire jobs resolve locked
dependencies before running checks. Published-image verification uses the same
helper around remote reads, leaving digest, revision, platform, provenance and
SBOM validation outside the retry boundary.

A recognized transient network or Git-fetch failure gets at most three
attempts, with two- and four-second delays. Authentication, certificate, missing
ref, missing manifest, dependency-solver and build failures fail immediately;
unknown failures also fail immediately. Failed reads never contribute partial
stdout to the successful metadata response. Tests and other commands cannot be
wrapped, so a retry cannot turn a failing test into a passing job.
