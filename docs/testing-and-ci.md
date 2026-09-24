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

Choose checks that exercise the changed boundary. Documentation-only changes
need a diff/whitespace review, validation of changed local links and anchors,
and the checks selected by CI; they do not need application suites merely to
assert prose. Implementation changes use the pinned lint, type, and generation
checks below plus the affected behavioral tests. Run the full relevant suites
and acceptance lanes for release-affecting changes. Report unavailable inputs
and unrun checks explicitly.

Run `git diff --check` before committing. Use the active task worktree for all
commands and a writable task-specific cache. Keep `tests` and `control/tests`
in separate invocations.

### Local Linux and container testing

On macOS, use OrbStack for container-backed tests before treating a Linux-only
path as unavailable:

```bash
docker context show
docker info
```

The active context and `docker info` output must identify the intended OrbStack
engine. Switch explicitly with `docker context use orbstack` when needed. Run
Compose, Linux/systemd harnesses, and disposable NAS acceptance in OrbStack or
in the designated CI lane; do not declare them impossible merely because the
host is macOS. OrbStack can catch container, Compose, installer, systemd, and
readiness regressions. Real NVIDIA hardware, NCCL/fabric behavior, model
quality, and physical Spark acceptance still require the designated Linux/ARM64
or Spark lane.

Use a writable, task-specific uv cache. Replace `vonk-example-change` in these
cache paths with the task name. Run from the active task worktree:

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes

# Fast tier: hermetic and parallel. No Docker, PostgreSQL, cargo or host tool.
UV_CACHE_DIR=/private/tmp/vonk-example-change-control-cache \
  uv run --project control --frozen --with-editable . pytest -q \
    control/tests -m "not lane" -n auto --dist loadfile
UV_CACHE_DIR=/private/tmp/vonk-example-change-acceptance-cache \
  uv run --python 3.14 --frozen --with pytest==9.1.1 \
    --with pytest-xdist==3.8.0 \
    --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" \
    pytest -q tests -m "not lane" -n auto

# Lane tier: the same trees without the marker filter. Run it in OrbStack or
# the designated CI lane; it needs Docker, PostgreSQL, cargo, dpkg and a
# Linux/ARM64 host.
UV_CACHE_DIR=/private/tmp/vonk-example-change-control-cache \
  uv run --project control --frozen --with-editable . pytest -q control/tests
UV_CACHE_DIR=/private/tmp/vonk-example-change-acceptance-cache \
  uv run --python 3.14 --frozen --with pytest==9.1.1 \
    --with-editable "$VONK_RECIPE_LIBRARY_ROOT/contracts" pytest -q tests

# Compose lane. These tests import the Controller, so they run in the control
# environment, not the root one.
TMPDIR=/tmp/vk UV_CACHE_DIR=/private/tmp/vonk-example-change-control-cache \
  uv run --project control --frozen --with-editable . pytest -q deploy/compose/tests
```

The compose lane needs the control environment because the container config it
loads imports `pydantic`; the root project deliberately has neither. On macOS,
also point `TMPDIR` at a short directory: several of these tests bind a Unix
socket under `tmp_path`, and the default `/private/var/folders/...` prefix plus a
long test name exceeds the 104-byte `sun_path` limit, which fails the whole
Tailscale group with `OSError: AF_UNIX path too long`.

The `lane` marker is applied automatically at collection time for a PostgreSQL
fixture or a `*_wire_bridge.py` Rust probe module. A test that starts Docker
carries `@pytest.mark.lane` itself, so the reason stays visible where the
container starts. Either way `-m "not lane"` stays honest. Investigate every
failure. Distinguish a reproduced defect from a missing lane dependency; neither
a skip nor an unavailable environment proves the behavior.

The fast tier is also hermetic about the developer's environment. The root and
control `conftest.py` files configure `git` to ignore global and system
configuration, so a test that creates a throwaway repository cannot be broken by
a personal `commit.gpgsign`, a signing agent, a hook, or `init.defaultBranch`.
Keep that isolation: a fixture that only passes on one machine is not evidence.

Run the two trees in separate pytest invocations. Both contain modules with the
same basename, so a single invocation over `tests control/tests` mis-collects
them.

Run the root `tests/` suite in the standalone environment CI uses: `pytest`
plus an editable install of the recipe contracts package. The root project is
the `vonk-cluster-profiles` package and its lint tooling; it deliberately has
no dependency on `pydantic`, `vonk_control`, or the contracts package, so a
bare `uv run pytest` from the root environment cannot import what the
acceptance and contract tests need. The control environment is a superset and
can also run that tree for a quick check, but CI parity is the standalone form.

`VONK_RECIPE_LIBRARY_ROOT` is a path to the sibling recipe-library checkout, not
a secret. Catalog, canonical-consumer, and acceptance-recipe tests read the real
library through it and fail at collection when it is unset, so export it before
running the root or control suites.

The native Rust agent and its wire contract build only for Linux. The
`control/tests/*_wire_bridge.py` suites consume probes produced by
`scripts/tests/run_agent_wire_contracts.py`; on macOS `cargo build` fails on
platform-gated code such as `rustix::fs::openat2`, so run those suites in the
Linux/OrbStack or designated CI lane instead of reading the failure as a
regression.

### Focused PostgreSQL and security lanes

The PostgreSQL, step-ca, and security-boundary lane tests do not need a Spark or
a release: with OrbStack running they execute locally in about a minute each and
are worth running before claiming a Controller change works.

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
UV_CACHE_DIR=/private/tmp/vonk-example-change-control-cache uv run --project control \
  --frozen --with-editable . pytest -q -m lane -n auto --dist loadfile \
  control/tests/test_catalog_documents_postgres.py \
  control/tests/test_agent_jobs_postgres.py \
  control/tests/test_agent_job_lock_order_postgres.py \
  control/tests/test_run_switch_postgres.py \
  control/tests/test_telemetry_postgres.py \
  control/tests/test_recipe_operations.py \
  control/tests/test_step_ca.py control/tests/security
```

### Lint, format, type and generation checks

For implementation changes, run Python lint and types, the TypeScript build,
and the generated-wire check below. Each uses the pinned toolchain.

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes

# Python lint; ruff is the repository's formatting authority too.
UV_CACHE_DIR=/private/tmp/vonk-example-change-uv-cache uv run --frozen ruff check .

# Python types. Pyright reads [tool.pyright] and resolves imports from the
# control virtualenv, so sync that project once first.
UV_CACHE_DIR=/private/tmp/vonk-example-change-control-cache \
  uv sync --project control --frozen
UV_CACHE_DIR=/private/tmp/vonk-example-change-uv-cache scripts/check-python-types

# Web behavior and types; the build runs tsc --noEmit before bundling.
npm ci --prefix control/web
npm test --prefix control/web -- --run
npm run build --prefix control/web

# Rust wire structures: fail if they no longer match the Pydantic schemas.
# This runs the typify code generator with cargo, then compares its output.
# The generator runs on macOS too; it does not build the Linux-only agent.
UV_CACHE_DIR=/private/tmp/vonk-example-change-control-cache \
  uv run --project control --frozen --with-editable . \
  python scripts/generate-agent-wire --check
```

`control/.venv` cannot satisfy the ruff pin because `openapi-python-client`
requires `ruff<0.14`; always lint through the root project. CI runs the same
version via `uvx --from ruff==0.16.1 ruff check .`.

The repository does not type-check cleanly yet, but every surviving error is a
reviewed one. `scripts/check-python-types` treats
`tools/pyright-baseline.json` as an allowlist: each entry names a file, a
pyright rule, the accepted count and the reason it is accepted. An error that
is not listed fails even when the file's total count is unchanged; a listed
entry whose count moves in either direction fails, so a second error of the
same rule cannot hide and a fixed error must be removed; an entry that no
longer occurs fails as stale; and an entry without a reason fails, so
`--update` is not a way to accept an error without saying why. Run `--update`
to write the current errors, then write the reason for anything it adds.
`pyright` runs over `control/src`, `src`, `tests` and `control/tests` in basic
mode; generated clients and virtualenvs are excluded.

The coordination boundaries are checked by
`control/tests/coordination_boundaries.py`, which is pure stdlib and runs as
`python3 control/tests/coordination_boundaries.py` (CI runs the same step). It
detects defined syntax patterns for transactions spanning external work and
artifact locks acquired inside transactions, blockingly, or more than one at a
time. Passing this scan establishes only its checked patterns; real PostgreSQL
and process tests establish the exercised concurrency and recovery behavior.
`tools/coordination-baseline.json` is a reviewed allowlist like
the pyright baseline: an unreviewed site fails, a baseline site that no longer
occurs fails as stale, and every entry carries a written reason. A stale entry
means its recorded violation no longer occurs, so delete it after reviewing the
change; it does not prove every runtime path safe.
Run `--write-baseline` after a fix to see exactly which entries disappeared.
The scanner's one-lock and nesting rules apply to locks that guard managed
storage. Two distinctions keep that honest. An in-process concurrency guard --
one that serialises this process's own work -- is a different resource class and
is listed by name in `GUARD_LOCK_NAMES` with its justification in the plan; a
lock that is not listed is still scanned as an artifact lock, so a new lock
cannot silently opt out. Separately, a blocking `flock` on a descriptor from an
`O_EXCL` create is provably private and cannot contend, so it is not reported;
a blocking `flock` on a shared file still is.

There is no separate ESLint or Prettier configuration. TypeScript formatting
follows the surrounding files, and `npm run build` is the type gate.

When acceptance inputs are available, run the actual harness through the same
OrbStack Docker context, not only its unit tests:

```bash
UV_CACHE_DIR=/private/tmp/vonk-example-change-acceptance-cache \
  uv run python tests/acceptance/test_fresh_nas_install.py
UV_CACHE_DIR=/private/tmp/vonk-example-change-acceptance-cache \
  uv run python tests/acceptance/test_spark_lifecycle.py run
```

Those commands require the candidate, compose, Controller, and acceptance
environment described by the acceptance workflow. Never substitute synthetic
success for missing environment inputs.

### Supply-chain verification

Run `scripts/verify-supply-chain --json` for generated release inputs; CI runs
the same verifier. Regenerate curated inventory when its inputs change and
expect a no-op for changes outside that set. See the
[development workflow](runbooks/development-workflow.md#generated-artifacts-and-release-evidence).

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
to `main` runs the required checks and the suites selected for that change. Concurrency cancels
superseded pull-request runs so a stale commit does not consume another
complete check cycle.

If a change needs a longer check, run it locally and attach its bounded report
to the pull request. Use `workflow_dispatch` only when hosted evidence itself
is required.

## Dependency acquisition failures

`scripts/retry-dependency-fetch` permits only `uv sync`, `skopeo inspect`,
`docker pull`, and `docker buildx imagetools inspect`. The Controller and wire
jobs resolve locked dependencies before running checks. Controller image tests
reuse local base images or pull missing inputs from `images.lock.json` through
the helper before building; they pass those same pins as build arguments.
Published-image verification uses the same helper around remote reads, leaving
digest, revision, platform, provenance and SBOM validation outside the retry
boundary.

A recognized transient network or Git-fetch failure gets at most three
attempts, with two- and four-second delays. Authentication, certificate, missing
ref, missing manifest, dependency-solver and build failures fail immediately;
unknown failures also fail immediately. Failed reads never contribute partial
stdout to the successful metadata response. Tests and other commands cannot be
wrapped, so a retry cannot turn a failing test into a passing job.

## Reusing accepted component images

Development publication reuses Hermes and LiteLLM by their exact build inputs.
`scripts/dev-image-inputs` hashes Git paths, file modes and blob identities for
an isolated build context plus the complete build/acceptance policy. BuildKit
receives only that context: a new `COPY` cannot silently depend on an unhashed
Controller file. Dockerfile/base-image pins, copied protocol code, mounted
LiteLLM smoke inputs and acceptance-tool changes invalidate the relevant key.
Controller-only changes keep both keys stable.

The Actions cache contains only the accepted registry digest, original build
commit and original signed provenance bundle. It has no prefix restore keys.
A hit must pass ancestry/input equality, GitHub signature/source/workflow
verification, and exact remote digest, both architectures, original revision,
BuildKit provenance and SBOM checks. Invalid evidence fails closed. A missing
or evicted entry takes the normal build, smoke and deep-scan path; an entry is
saved only after the complete publication succeeds. Cold builds also exercise
the signed-cache consumer before saving the entry.

A reused image keeps its original digest and build revision. The current
`dev-sha-` tag and acceptance receipt bind that image to the current assembled
release; they do not rewrite its original build provenance. Stable promotion
checks the current acceptance signature and the same source-equivalence gate.
Compose validation and fresh NAS/Spark installer acceptance still run for the
assembled release. API and worker continue using BuildKit layer caches and
embed their current Controller source identity, so they are rebuilt on each
qualifying generation.
