# Testing and CI policy

Vonk Forge keeps pull-request CI small and deterministic. GitHub Actions is a
merge gate, not the place to run every hardware, browser, Docker, or long-lived
acceptance test on every change.

## Required on every pull request

The protected `main` ruleset requires exactly these checks (verified against the
live repository ruleset, not the dated protection report under `inventory/`):

| Check | Purpose |
| --- | --- |
| `Ruff` | Whole-tree Python lint, formatting and type checks on every PR and release. |
| `Generated control clients` | Regenerate OpenAPI clients twice and compare their bytes. |
| `Compose integration` | Exercise the Compose and ingress boundaries. |
| `CI gate` | Aggregate the suites selected for the change. |

Every PR runs complete sharded Controller and repository suites, repository
guards, web tests, Rust tests/lint, generated clients and Python lint/types.
Integration jobs follow a conservative transitive dependency closure in
`scripts/select-ci-areas`; unknown inputs run every integration. PostgreSQL,
wire and Rust platform checks run on every PR because their dependencies span
these boundaries. Image/package builds may be selective.

Integration PRs must accept their own candidate before merge. CI applies the
`release-acceptance` label to same-repository PRs; `CI gate` requires their exact
candidate's acceptance. See [the contributor guide](../contributor.md#ci-and-pre-merge-release-acceptance)
for dispatch by ref and protected-environment configuration. The PR and main
publishers call the same candidate assembly and acceptance workflows.

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

For the complete local repository and Controller pytest suites, run:

```bash
scripts/test-local
```

While iterating, run only the test files that directly exercise your change:

```bash
scripts/test-local --changed          # changes since the merge base with origin/main
scripts/test-local --changed HEAD~3   # or since an explicit base
```

`scripts/select-changed-tests` maps each changed path (committed, staged,
unstaged or untracked) to test files: a changed test file selects itself, a
changed Python module selects the test files that import it directly, and any
other file selects the test files that name its path. It ignores transitive
imports, so it is a pre-check; run the full suites before requesting review.

### Fresh checkout build and contract smoke checks

Install uv, Node/npm and the pinned Rust toolchain (on macOS, also prepare the
`vonk-ci` OrbStack VM for the Linux workspace check), then run:

```bash
scripts/check-fresh-contract-build
```

This prepares the locked Python environment, generates clients and wire types,
checks deterministic generation, builds the web app and CLI wheel, runs the
Python contract tests, and checks the Rust workspace. Generated clients and wire
schemas are ignored build inputs, never reviewable sources. `npm run build`
generates web inputs in `prebuild`; the wheel hook generates its Python client
and bundled schemas; Cargo's `build.rs` exports Pydantic and renders Rust into
`OUT_DIR`. Pytest prepares missing inputs before collecting consumers. The
Controller Dockerfile generates in its contracts stage before packaging.

### Per-test time budget

Every test in the repository, Controller and Compose suites must finish its
setup and body within 10 seconds. `tools/pytest_budget.py` checks this after
each test and records overruns with their measured time. Setting up
a fixture shared beyond one test (session, module or class scope, such as the
PostgreSQL server or the installed CLI) is not charged to the test that first
requests it: doing expensive work once is the intended fix. It does
not interrupt the test: an alarm at an arbitrary point can leave shared state
half-built and fail unrelated tests. pytest-timeout's `timeout = 120` in the
pytest configuration (`timeout_method = "thread"`: a hang dumps every stack and
ends the worker, whatever the test is blocked on) is only the hang guard.

A test that needs longer because its subject is inherently expensive (a real
process death, the whole published corpus) carries `@pytest.mark.slow(<seconds>)`
with at most 60 seconds and a comment saying why. Tests that drive the
installed `vonkctl` as separate processes against an HTTPS Controller peer get
20 seconds from one rule in `control/tests/conftest.py`. Keep that set small: first
remove repeated work (build or start once per session, inject clocks instead
of sleeping, shrink fixtures to the boundary under test).

A budget is wall time on a reference machine: the plugin scales it by a
calibration measured at session start (a short pure-Python benchmark, between
1x and 5x; `VONK_TEST_BUDGET_CALIBRATION` pins it), so a slow
or loaded host gets proportionally more time. `--test-budget-scale=2` (or `0`
to disable) multiplies it further on a machine that is knowingly overloaded.
One overrun never fails a run: at the end of the session every test over its
scaled budget is rerun once, together in one fresh process, and fails the run
only if the overrun reproduces there, regardless of its initial magnitude
(the rerun applies locally too, not only in CI). An inconclusive rerun is
warning-only too. The run reports warnings in its summary and through
`::warning::` annotations.

The repository scanners (vocabulary, blocker, lifecycle, coordination, content
identity) parse each Python file once per session through
`control/tests/parsed_sources.py`; a new scanner reads the tree through it and
treats the trees as read-only.

No test builds a container image. Checks that need the real Controller or
worker image carry `@pytest.mark.built_image` and take the image from
`VONK_TEST_CONTROLLER_IMAGE` or `VONK_TEST_WORKER_IMAGE`: each is a quick
`docker run` against an image that is already built, well inside the normal
budget. The "Controller image build tests" CI job builds both targets of
`control/Dockerfile` once (buildx with the GitHub Actions cache), then runs
`pytest -q -m built_image control/tests`; the control suite shards deselect
them with `-m "not built_image"`, and the CI gate requires that job whenever
the control suite is selected. Without the variable such a test skips locally
and fails in CI. `scripts/test-local --with-image-build` builds the two images
first and includes the checks; by default they are skipped. Prefer a static
check of the Dockerfile or build inputs where that proves the property.

The local `control` project refers to the ignored protocol wheel under
`inventory/wheels`. `scripts/test-local` builds and verifies it automatically.
For direct `uv sync --project control` or `uv run --project control` commands,
run `scripts/build-control-wheel` once first; it also checks the wheel against
the hash locked in `control/uv.lock`.

On Linux, this runs both suites in full. On macOS, it runs portable tests on
the host and sends tests marked `linux_only`, `needs_systemd`,
`needs_rust_probe`, `needs_repair_probe` or `postgres` to the `vonk-ci` OrbStack
VM when available.
It prints an explicit prerequisite skip if the VM is unavailable; CI must
provide its equivalent Linux lane and required recipe checkout, and fail when
either is missing. The VM uses task-specific uv and Cargo directories under
`$HOME` to isolate Python and Rust artifacts. Set
`VONK_RECIPE_LIBRARY_ROOT` when the sibling
checkout is outside `/opt/vonk-forge-recipes`.

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

Run the lane in the VM from a worktree with `scripts/test-vm-lane <ref>
[-- PYTEST_ARGS...]` (default: the installed-CLI tests). A worktree's gitdir
lives under `/opt`, which the VM cannot see, so the script clones `<ref>` (a
committed ref in this repository) into a unique `/private/tmp/vonk-vm-lane.*`
directory, builds the VM-side environments under unique names in the VM's
`$HOME`, runs pytest there and removes only those directories afterwards. Never
point the VM at the host checkout, and never delete other agents' directories.

Every Python suite, lint and type check runs in one locked environment: the
`control` project with its `dev` dependency group (`control/uv.lock`). It
contains the root `vonk-cluster-profiles` package (editable), the Controller,
the locked recipe contracts, pytest and its plugins, the build backend and
pyright and ruff. Nothing is added with `uv run --with` or `uvx`: a new test or
tool dependency goes into a dependency group and the lock.

Tests never download, build an environment or pull an image. A test that needs
a wheel builds it with the locked `hatchling` in the running interpreter; one
that installs a wheel into a scratch venv installs it `--no-deps` and links the
CLI's own locked dependencies, which `scripts/sync-cli-dependencies` prepares
(`scripts/test` runs it first); Docker-backed tests use the pinned images
`scripts/pull-test-images` pulls beforehand (CI does the same, with retries).

The CLI dependency environment holds compiled packages (rpds, ...), so it is
keyed by OS and architecture: `.cli-dependencies/<os>-<arch>` (for example
`darwin-arm64` and `linux-arm64`), stamped with the platform and interpreter
that built it. The macOS host and the `vonk-ci` VM see the same files, so they
must never share one environment; `scripts/sync-cli-dependencies` rebuilds any
environment whose stamp does not match, and the tests and the
`needs_cli_dependencies` prerequisite refuse one (`tools/cli_dependencies.py`,
guarded by `tests/test_cli_dependencies_platform.py`). A `No module named
'rpds.rpds'` failure in an installed-CLI test means a foreign environment was
linked; it is no longer possible without `VONK_CLI_DEPENDENCY_ENV` pointing at
one.

Use a writable, task-specific uv cache. Replace `vonk-example-change` in these
cache paths with the task name. Run from the active task worktree:

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
export UV_CACHE_DIR=/private/tmp/vonk-example-change-uv-cache
scripts/build-control-wheel
scripts/sync-cli-dependencies

# Fast tier: hermetic and parallel. No Docker, PostgreSQL, cargo or host tool.
uv run --project control --frozen pytest -q control/tests -m "not lane" -n auto --dist loadfile
uv run --project control --frozen pytest -q tests -m "not lane" -n auto

# Lane tier: the same trees without the marker filter. Run it in OrbStack or
# the designated CI lane; it needs Docker, PostgreSQL, cargo, dpkg and a
# Linux/ARM64 host.
scripts/pull-test-images postgres caddy tailscale python
uv run --project control --frozen pytest -q control/tests
uv run --project control --frozen pytest -q tests
uv run --project control --frozen pytest -q -n auto deploy/compose/tests
```

`scripts/test SUITE` is the one entry point for the Python suites
(`repository`, `control`, `compose`, `images`); CI calls it verbatim, with
`--shard I/N` and the measured per-file durations. `--markers "not lane"`
narrows any suite to the fast tier, and arguments after `--` go to pytest.

Prerequisites have one registry and one policy, `tools/pytest_prereqs.py`,
shared by every suite. A test that needs something beyond the locked Python
environment carries a prerequisite marker (`postgres`, `needs_docker`,
`linux_only`, `needs_recipe_library`, ...); the Controller suite infers
`postgres`, `needs_cli_dependencies`, `needs_rust_probe` and
`needs_recipe_library` from what a test requests. A missing prerequisite skips
locally with its reason and fails in CI when the runner provides it (or the job
names it in `VONK_CI_PREREQUISITES`). Every test with a prerequisite marker is
also marked `lane`, so `-m "not lane"` stays honest. Investigate every
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

The root project is the `vonk-cluster-profiles` package; it deliberately has
no dependency on `pydantic`, `vonk_control` or the contracts package. The
installed-CLI tests prove that boundary by installing the built wheel into a
separate venv; the `tests/` suite itself runs in the control environment like
every other suite.

`VONK_RECIPE_LIBRARY_ROOT` is a path to the sibling recipe-library checkout, not
a secret. Catalog, canonical-consumer, and acceptance-recipe tests read the real
library through it and fail at collection when it is unset, so export it before
running the root or control suites. The recipe repository does not commit its
catalog index or packages: generate them once per checkout revision with
`scripts/build-recipe-library "$VONK_RECIPE_LIBRARY_ROOT"`, as every CI job
that checks out the library does.

The native Rust agent and its wire contract build only for Linux. The
`control/tests/*_wire_bridge.py` suites consume probes produced by
`scripts/tests/run_agent_wire_contracts.py`, which builds every probe in one
Cargo invocation and exports their paths. The tests never build probes
themselves: without the exported path they skip locally and fail in CI. On
macOS `cargo build` fails on platform-gated code such as
`rustix::fs::openat2`, so run those suites in the Linux/OrbStack or designated
CI lane instead of reading the failure as a regression.

PostgreSQL tests share one disposable server per pytest session, including
across xdist workers. It starts when collection finishes (only if a selected
test needs it), so its start-up is not charged to a test, and each test still
gets its own database. The server keeps its data on tmpfs with `fsync` off
because it is thrown away after the session; the process-death tests kill
client processes, not the server. Each server is labelled with its owning
session, which stops it at the end; the next session stops any server whose
owner died without teardown.

### Focused PostgreSQL and security lanes

The PostgreSQL, local CA, and security-boundary lane tests do not need a Spark or
a release: with OrbStack running they execute locally in about a minute each and
are worth running before claiming a Controller change works.

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
UV_CACHE_DIR=/private/tmp/vonk-example-change-uv-cache uv run --project control \
  --frozen pytest -q -m lane -n auto --dist loadfile \
  control/tests/test_catalog_documents_postgres.py \
  control/tests/test_agent_jobs_postgres.py \
  control/tests/test_agent_job_lock_order_postgres.py \
  control/tests/test_run_switch_postgres.py \
  control/tests/test_telemetry_postgres.py \
  control/tests/test_recipe_operations.py \
  control/tests/test_local_ca.py control/tests/security
```

### Lint, format, type and generation checks

For implementation changes, run Python lint and types and the TypeScript build.
Each uses the pinned toolchain.

```bash
export UV_CACHE_DIR=/private/tmp/vonk-example-change-uv-cache

# Python lint; ruff is the repository's formatting authority too.
uv run --project control --frozen ruff check .
uv run --project control --frozen ruff format --check .

# Python types. Pyright is locked in the control dev group and resolves imports
# from that same environment.
scripts/build-control-wheel
uv sync --project control --frozen
scripts/check-python-types

# Web behavior and types; the build runs tsc --noEmit before bundling.
npm ci --prefix control/web
npm test --prefix control/web -- --run
npm run build --prefix control/web
```

Generated clients and wire outputs are ignored build inputs. Test setup prepares
them, and `tests/test_generated_contracts.py` compares them with their Pydantic
producers alongside the remaining checked-in installer and qualification
schemas. CI runs `scripts/check-generated-determinism` for control clients and
`--wire` for Rust outputs: both compare two generation runs, including Python
file membership. Existing contract round-trip tests still exercise the real
producer and consumer boundaries.

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
With no file arguments, `pyright` runs over `control/src`, `src`, `tests` and
`control/tests` in basic mode; generated clients and virtualenvs are excluded.
The commit hook passes changed Python paths and checks only their diagnostics
and reviewed exceptions. CI retains the full check, including errors in
unchanged consumers. Partial checks cannot update the repository baseline.

New syntax is checked by `scripts/check-added-lines [PATCH_FILE|-]`, which consumes
an externally supplied unified diff. CI creates that patch against its explicit
PR/merge-group base. Only added lines can fail. The adapter, scanners and fixture
tests never read Git.
There are no debt counts, category ledgers, baseline updates, or PR count reports.

The guards reject new handwritten contract literals, untyped mapping annotations,
provenance comparisons, security refusals outside ingress owners, unbounded waits,
read refusals, imperative remedy text, missing retention policy, anti-principle
test assertions, and the existing coordination syntax violations. Small fixtures
prove that added violations fail, untouched old occurrences do not, and bounded
waits and security ingress remain valid. Detection is a syntax check; behavior,
process and PostgreSQL tests establish recovery and fresh admission.

The model registry, pyright exceptions, Rust serde and TypeScript shape registries,
and generated wire/client/vocabulary checks retain their existing contracts.
Controller startup and sparse-serialization exceptions remain semantic registries.
Lifecycle writer ownership remains checked without a baseline.

The lifecycle also has a hardware canary that nothing in CI runs:
`scripts/lifecycle-canary` drives one load, one cancel during start and one
Controller-restart-tolerant wait through `vonkctl` and asserts that no operator
wait lacks an action and no admission is stuck. It is run by hand after
lifecycle releases; see [the runbook](runbooks/lifecycle-canary.md).

TypeScript uses pinned Biome for formatting and focused correctness lint rules.
The commit hook and pull-request CI check changed authored TypeScript files.
`npm run typecheck --prefix control/web` checks both the application and browser
tests/configuration; `npm run build` also checks application types. Generated
contracts remain owned by their generators. Rust hooks run pinned formatting and
offline Clippy/compiler checks, using the Linux VM on macOS. Commit checks prepare missing or stale locked Python and TypeScript dependencies
automatically, with serialized preparation and a ten-minute deadline.

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

### Cleaning up in the VM and other shared machines

Remove only what you created, and only through a path from `mktemp -d`: assign
it once, check it is non-empty (`"${dir:?}"`), and never build a path under
`$HOME` from a variable that may be empty. `tests/test_shell_destructive_guard.py`
rejects an unguarded `rm -rf` in repository scripts and runs `shellcheck` with
SC2115 and SC2086 as errors; CI installs `shellcheck` for it.

### Supply-chain verification

Run `scripts/verify-supply-chain --json` to validate authored lockfiles, the
third-party contract wheel digest, and image pins. CI builds the protocol
wheel, generates SPDX documents and a digest manifest, and uploads them as
verified release evidence. These generated files are not committed or staged
by the pre-commit hook, which runs only lint, format, and types. See the
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
  minutes so it is genuinely the thing you run while iterating, and every test
  stays within the per-test time budget above.

## When the longer jobs run

Container publication and release metadata are protected by the release
environment and external gates. Ordinary pushes do not run CI; a pull request
to `main` runs the required checks and the suites selected for that change. Concurrency cancels
superseded pull-request runs so a stale commit does not consume another
complete check cycle.

### Repository guards run for every change

`Repository guards` runs the fast cross-area checks on every CI invocation,
including documentation-only and control-only PRs, merge queues, and main/release
pushes through the release workflow. `CI gate` requires its success independently
of `scripts/select-ci-areas`. The sharded repository suite also always runs and excludes the same files/nodes
to avoid running guards twice.

`tests/repository_guards.py` owns the guard selection and the reviewed scope of
all other test files under `tests/`. The inventory test rejects new or stale
unclassified files, including scans delegated to helpers. A new whole-tree guard
belongs in `GUARDS`; a narrower test needs its owning boundary in `SCOPED`.
Mixed integration files select their cross-area guard nodes individually. The
local command `scripts/test guards` runs them. The runner
`python3 scripts/repository-guards` prints that selection; `--exclusions`
prints its complement's pytest options. No test reads Git history for this decision.

### Release acceptance before merge

Every integration PR, including acceptance-harness changes, builds an immutable
candidate from its own reviewed merge commit and runs clean NAS, clean Spark and
Spark upgrade-carry acceptance. `CI gate` requires this run. The workflows reuse
`installer-candidate.yml` and `release-acceptance-core.yml`; main runs the same
checks before promotion. The promoted release is the upgrade baseline, never a
substitute for the PR candidate. A workflow dispatch accepts an explicit `ref`.

The acceptance lanes use disposable synthetic Spark services on ARM64 CI hosts;
physical GPUs and model quality still require their designated qualification.

A new lane phase can start in `OBSERVED_PHASES` (`tests/acceptance/spark_upgrade_carry.py`)
when the platform change it needs is not in a promoted release yet: it runs and
reports in the lane's report and as a warning annotation, but its failure does
not stop promotion. Making it gating is deleting its name from that set, a
lane change that needs its own green `lane-proof`.

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


### Script subprocess time budgets

Developer and CI helpers bound each external command independently of the job
watchdog. Local metadata, version and signature tools use the publication
30-second budget. Dependency acquisition allows three five-minute attempts
within the 20-minute acquisition lane, preserving partial diagnostics on stderr
and exposing the next retry; denied access and invalid pinned inputs still stop.
Generators, Cargo probe builds and each wire test selection use the wire lane's
20-minute budget. Fabric diagnostics and apt index work use ten minutes, while
the canary restart command shares its configured scenario duration.

Object publication allows a maximum one-GiB artifact at one MiB/s plus the
30-second setup allowance. Expired uploads reconcile the exact object digest
before the existing bounded retry. Streaming object reads additionally bound
connection and socket idle time to 30 seconds and use the same total transfer
budget. These budgets bound attempts; they do not change document, argument,
artifact size, authorization or integrity contracts.

The Admin web CI job uses the digest-pinned Playwright image matching its locked
version. Chromium and its system libraries are already installed, so the job
does not perform apt or browser downloads. npm keeps its lockfile-keyed cache.
