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

`ci-gate` passes `toJSON(needs)` to `scripts/verify-ci-gate`, which applies one
rule per job and refuses a gated job it has no rule for, so a job added to
`ci-gate.needs` without a rule fails the run. Adding a proof job means the job,
its `ci-gate.needs` entry and its rule in `scripts/verify-ci-gate`.

Every change lands through an integration PR: a same-repository
`integration/*` head, or any PR with the `release-acceptance` label. `full` in
`scripts/select-ci-areas` owns that rule; `ci.yml` reads it, and every proof is a `ci.yml` job behind
`ci-gate`.

| Run | What runs |
| --- | --- |
| Member PR | The quick tier only, a few minutes on standard runners: `Ruff` (lint, format, Python types, the added-line guards), the web typecheck and Biome (`member-web`), and `cargo fmt` plus clippy (`rust-quality`) only when the change touches Rust or an input of the Rust build (`rust_changed` in `scripts/select-ci-areas`, which reads the input list from the wire crate's `build.rs`). A member push costs about 5-6 standard runner-minutes (Ruff, web, and Rust when it changed), which compete with batch CI for the concurrency limit. No full suite, proof or acceptance. The gate reports `CI gate (member PR)`, never the required `CI gate`, so a member PR cannot merge; it leaves no receipt for Release. The quick tier is a fast signal, not proof: check member work locally (`scripts/test changed`); its integration PR proves it. |
| Dependabot PR | Nothing, not even the selector or the gate: every job that could start on its own skips a PR authored by `dependabot[bot]`, so it uses no runner. Its skipped gate keeps the raw name expression, never the required `CI gate`. Fold it into an integration PR, which runs CI once (`tests/test_dependabot_updates.py`). |
| Integration PR | The complete sharded Controller and repository suites, web tests, Rust tests/lint/platform, wire contracts, every selected integration, candidate acceptance with the upgrade carry, and every selected proof (agent repair recovery, client compatibility report, installed CLI, model-cache cancellation, OCI link fallback, privileged helper, profile-effect consumer). |
| Main / Release | `ci.yml` in full, unless Release reuses the integration PR's run for the identical tree, then release acceptance. On a signed `v*` tag `ci.yml` also calls `stable-release.yml` after `ci-gate`; that file holds the stable-channel publication jobs, and the stable package signature identity names it. |

A member PR is done when its review has passed.
Adding the `release-acceptance` label re-runs `ci.yml` in full at once.

Jobs that build Rust set it up with `.github/actions/setup-rust` (toolchain
from `rust-toolchain.toml`, read-only shared cache).

Integration jobs follow a conservative transitive dependency closure in
`scripts/select-ci-areas`; unknown inputs run every integration. PostgreSQL,
wire and Rust platform checks run on every full run because their dependencies
span these boundaries. Image/package builds may be selective.

Ordinary pytest files in the reviewed core test directories run the core suites
without rebuilding unchanged images, packages or release candidates. Shared
fixtures, helpers, acceptance programs and image-consuming tests remain
integration inputs. The selector owns this boundary; unknown or unreadable
inputs remain conservative. The PostgreSQL fault-recovery test runs once in
the complete Controller suite, without a second standalone job.

Integration PRs must accept their own candidate before merge. CI applies the
`release-acceptance` label to same-repository `integration/*` PRs (the branch
alone selects acceptance); `CI gate` requires their exact
candidate's acceptance, with the upgrade carry. A member PR runs no acceptance:
it folds into an integration PR. A merge-queue commit accepts its candidate
when it changes upgrade-sensitive paths (`CARRY_INPUTS`) or the acceptance
programs. A merge without candidate evidence makes Release run the Spark
acceptance (and the recipe refresh only if the change can alter the upgrade)
itself.
See [the contributor guide](../contributor.md#ci-and-pre-merge-release-acceptance)
for dispatch by ref and protected-environment configuration. The PR and main
publishers call the same candidate assembly and acceptance workflows.

Only the `release-acceptance` label starts a new CI run. Any other label runs in
its own concurrency group, so it never cancels the run in progress, and every job
skips it. Only `CI gate` is renamed on that run (`CI gate (label ignored)`), so
no such run reports a `CI gate` that could satisfy the requirement while the real
run is pending; a member PR's gate is likewise `CI gate (member PR)`. Other jobs keep their names: a job skipped by its own `if` shows
its name expression unevaluated, which would break a required check such as
`Compose integration` on pull requests that skip it.

The acceptance's upgrade carry (a workload keeps serving while the Controller
is redeployed and the agent upgraded to the candidate) runs on main's release
for every merge, and on every integration PR. Release refreshes recipes only
for a change to what the upgrade is made of: the agent, its packaging and
installers, the deployment, the Controller-to-agent protocol, and the scripts,
actions and workflows that assemble and accept a candidate (`CARRY_INPUTS` in
`scripts/select-ci-areas`). The `release-acceptance` label makes any PR a full
run with both. The baseline starts every service its installed Compose file
declares except the Tailscale pair (the lane has no tailnet, and the installed
file carries no `profiles:` key to tell them apart); after the upgrade the
candidate's roster is expected. The harness is newer than the baseline it
installs, so before the upgrade it requires only what any release provides
(the services its own file declares, the service names a Spark needs, the
Caddy wrapper it ships); the candidate stays exact after the upgrade.
`test_carry_baseline_phase_accepts_an_older_release_shape` runs those
baseline-phase checks against an older release's shape (a standing registry,
four service names, no runtime-init, a root API), so a candidate-only
expectation fails there in seconds instead of in Release.

The carry waits on behaviour, never on a fixed timer. After the baseline
serves, after the Controller recreate and after the agent upgrade, each phase
ends once the workload answered three consecutive probes in it, its route is
published and, after a restart, the Spark reported the carried run to the
Controller again (rank evidence newer than the restart). A phase that does not
settle within its bound (60, 120 and 300 seconds) fails and names what is
missing, for example an upgraded agent that never reports the run it inherited.

The carry proves only the upgrade. Recipe refresh runs beside it on its own
Spark, wherever the carry runs (`Refresh recipes on the candidate`): on a Spark
installed with the candidate, a new editorial revision replaces the serving
canary, and then two Recipes that build one image install and serve in turn.
Acceptance signing requires it the same way as the carry.

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

**The local check before pushing is `scripts/test changed [--base REF]`** (base
defaults to `origin/main`). It runs the fast guards (`scripts/test guards`), then
only the test files your diff edits and those that import a touched module,
statically (an `ast` import graph over `tests/`, `control/tests/`,
`agent_protocol/tests/` and `deploy/compose/tests/`, followed through test support modules and conftests; a
touched package `__init__` selects importers of anything inside the package).
It prints the selected files first, runs each suite in its own pytest process with
`-n 2` (`agent_protocol/tests` in the installed-wheel consumer environment with
`-c /dev/null`, as CI does), and prints which selected files hold
`built_image`, `corpus_acceptance` or Postgres tests: those run in CI (or
`scripts/test-vm-lane`). An empty selection runs the guards only. Full suites run
in batch CI; do not run them locally.

For the complete local repository and Controller pytest suites (rarely needed), run:

```bash
scripts/test-local
```

`scripts/test-local` and `scripts/test-vm-lane` first place the recipe library's declared
adapter sources (`tools/fetch-adapter-sources` in `VONK_RECIPE_LIBRARY_ROOT`, default
`/opt/vonk-forge-recipes`): a recipes pull deletes files that moved to its mirror, the
synthetic canary package among them. Placing reads the recipes cache and fetches only
what is missing; a failure is a warning, and the tests that read those files fail.

`tools/touched_tests.py` is the only selector of changed tests. Besides the import graph it selects the test files that name a touched non-module path (a fixture, script or data file).

### Fresh checkout build and contract smoke checks

The exact uv release is owned by `.tool-versions`; Actions and container builds
read that file. Each project declares an equal `tool.uv.required-version` floor,
so newer local installations remain usable. The single-source guard rejects
independent pins and floors that differ from the CI release. Container bootstrap
installs use hash-locked PyPI wheels, including uv with its owner-derived version.

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
`OUT_DIR`, from the protocol alone: it reruns on `agent_protocol/src`,
`agent_protocol/pyproject.toml`, the root `uv.lock` and the exporter (its Python call sets
`PYTHONDONTWRITEBYTECODE`, because `__pycache__` files written into `agent_protocol/src`
would make the next cargo invocation rerun it and recompile the dependent crates), and the
Rust jobs (`rust-quality`, `rust-tests`, the agent package jobs) prepare only the
protocol member (`.github/actions/protocol-env`, an inexact sync), not `python-env`. Pytest prepares missing or outdated inputs before collecting
consumers: `tools/generated_contracts.py` stamps the fingerprint of the
generator inputs and outputs under `.state/`, so a tree is generated once per
change and every later pytest start, web hook (`prebuild`, `pretest`) and CI
job finds it current. `scripts/prepare-generated-contracts` runs the same
preparation by hand. The Controller Dockerfile generates in its contracts
stage before packaging.

### Report-only per-test timing

Tests in the repository, Controller and Compose suites have a report-only
threshold of 10 seconds for setup and body. `tools/pytest_budget.py` measures
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
Overruns appear in a `slow tests` terminal summary and, when JUnit output is
enabled, test properties. They never fail a test or the run and never trigger
a rerun. Workflow job-level timeouts remain the guard against hangs alongside
pytest-timeout.

Every job that runs steps sets `timeout-minutes` (typical duration plus margin) and
runs on `ubuntu-24.04` or `ubuntu-24.04-arm`, never `ubuntu-latest`;
`tests/test_workflow_assertions.py` enforces both for every workflow.

### Test tiers and the budget gate

`VONK_TEST_TIER` selects how much of an exhaustive sweep a run performs:
`full` (the default locally and on main, whose release workflow calls this
suite) or `pr` (pull request and merge-queue runs, where `ci.yml` sets it).
A sweep that iterates a whole corpus, such as
`control/tests/test_catalog_dry_run.py` over every recipe of the pinned
library, runs a fixed share of it on the `pr` tier plus every entry with a
known defect, so a strict xfail that starts passing is still caught before
merge. The complete sweep stays the evidence on main. A test owns its own
tiering; the tier never skips a test outright.

The per-test budget is a gate for the tests a change adds or touches.
`scripts/changed-tests` reads the pull request's diff and the tree and lists
every test function with an added line (`path::function`). Each pull-request
and merge-queue suite shard diffs against the base commit, passes that list as
`--test-budget-enforce`, and such a test over its budget without a slow mark
fails. An untouched overrun stays report-only until someone edits that test,
and main's full run enforces nothing because it has no diff.

Shards are whole test files, listed from the tree the way pytest finds them,
so splitting a suite costs no collection pass. Shard weights come from actual
pytest JUnit reports, including the default xunit2 module/class names; a file
without a measurement weighs the measured average. Reuse those measured file
durations instead of letting one long test file dominate a shard.

NAS setup behavior runs as Python tests under `tests/nas_setup` in the normal
repository suite. Preserve real filesystem and process checks for fresh install,
upgrade, lock contention, interrupted publication, replay, credential retention,
secret permissions and PKI matching/renewal. Reuse expensive immutable fixtures
while isolating each test's mutable bundle. Keep production password-derivation
strength and cryptographic validation; there is no NAS Rust crate, dedicated
Rust NAS test job or special NAS crypto test profile.

The setup producer builds one architecture-independent Python NAS wheel and the
native Linux ARM64 Spark installer. It does not maintain native NAS macOS/Linux
build matrices. The acceptance workstation exposes the installer's required
tools through a restricted PATH and reuses the prepared uv cache and Python
directory while isolating HOME and temporary files. It does not inherit runner
secrets or index credentials. Linux Intel and ARM remain supported Controller
hosts. Shared
accepted NAS Docker lanes still exercise the real Compose and installer workflow;
resident Spark, privileged-helper and native ARM64 acceptance remain required
where selected. Record the exercised runner platform with each acceptance
result. Source tests and package checks remain separate from deployment and
physical Spark qualification.

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
them with `-m "not built_image"`, and the CI gate requires that job when
Controller and NAS integration are selected. Ordinary core test edits do not
rebuild images. Without the variable such a test skips locally
and fails in CI. `scripts/test-local --with-image-build` builds the two images
first and includes the checks; by default they are skipped. Prefer a static
check of the Dockerfile or build inputs where that proves the property.

The whole-corpus catalog acceptance
(`test_fresh_postgres_imports_typed_canonical_model_recipe_api`) carries
`@pytest.mark.corpus_acceptance`. It takes 96-105 s beside the shards' `-n 4`
workers and about 71 s alone on a CI runner, against its 60 s budget, so the
control shards deselect it (`-m "not corpus_acceptance"`) and the "Corpus
acceptance" CI job runs only marked tests, alone, with PostgreSQL and the built
recipe library (`scripts/test acceptance`). The budget is unchanged and the
lane reports it without enforcing it (no `--test-budget-enforce`). The job is selected with
the control area and the CI gate requires it.

The protocol is an editable workspace member, so no test or
`uv sync --project control` / `uv run --project control` command needs a wheel.
`scripts/build-control-wheel` only builds the ignored protocol wheel under
`inventory/wheels` that the Controller image and the release carry; every CI job
that builds `control/Dockerfile` runs it first, and the adapter-Python job
installs that wheel into a Python 3.12 venv, which is what proves the protocol's
3.12 support (the workspace lock itself resolves for 3.14). Nothing compares the
wheel with a lock: it is our own build output, not ingress.

On Linux, this runs both suites in full. On macOS, it runs portable tests on
the host and sends tests marked `linux_only`, `needs_systemd`,
`needs_rust_probe` or `postgres` to the `vonk-ci` OrbStack
VM when available.
It prints an explicit prerequisite skip if the VM is unavailable; CI must
provide its equivalent Linux lane and required recipe checkout, and fail when
either is missing. The VM uses task-specific uv and Cargo directories under
`$HOME` to isolate Python and Rust artifacts. Set
`VONK_RECIPE_LIBRARY_ROOT` when the sibling
checkout is outside `/opt/vonk-forge-recipes`.

On the `vonk-ci` OrbStack VM the port forwarder (`machines.forward_ports`, source
`orbstack-agent`) probes every new `127.0.0.1` listener once with `GET /` from
`Go-http-client`; a fixture that accepts first would count it as the client under
test. `127.0.0.2` reaches the same loopback interface and is not probed, so every
Rust TCP test fixture binds there (`FIXTURE_BIND` / `FIXTURE_HOST` /
`FIXTURE_IP` in vonk-agent `client/test_support.rs`; a constant in
`vonk-build-egress/src/tests.rs`). The `loopback-bind` added-line guard rejects a
new `127.0.0.1` bind in Rust test code; Unix sockets bind a path and are not
affected. GitHub runners have no probe.

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
`control` project with its `dev` dependency group, installed from the one
workspace lock (`uv.lock`) into the root `.venv`; `uv lock --check` in the lint job
keeps the lock current. It
contains the Controller and, as a test client only, the root
`vonk-cluster-profiles` package (editable; the Controller image does not install it),
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

Cargo also reads config files from a checkout's parent directories, so a worktree
under the Mac home would hand the host `~/.cargo/config.toml` (a macOS
`rustc-wrapper`) to cargo in the VM, which breaks clippy's
`env!("CARGO_BIN_EXE_*")`. Every script that runs cargo in `vonk-ci` exports
`CARGO_BUILD_RUSTC_WRAPPER=` (guarded by `tests/scripts/test_vm_cargo_hook.py`).

Use a writable, task-specific uv cache. Replace `vonk-example-change` in these
cache paths with the task name. Run from the active task worktree:

```bash
export VONK_RECIPE_LIBRARY_ROOT=/opt/vonk-forge-recipes
export UV_CACHE_DIR=/private/tmp/vonk-example-change-uv-cache
scripts/build-control-wheel
scripts/sync-cli-dependencies

# Fast tier: hermetic and parallel. No Docker, PostgreSQL, cargo or host tool.
uv run --project control --frozen pytest -q control/tests -m "not lane" -n auto --dist worksteal
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
(`repository`, `control`, `corpus`, `compose`, `images`); CI calls it
verbatim, with `--shard I/N` and the measured per-file durations.
`--markers "not lane"` narrows any suite to the fast tier, and arguments after
`--` go to pytest. The `corpus` suite is the consumer corpus boundaries,
`control/tests/test_consumer_corpus_boundaries.py`, run against the native
probe named by `VONK_CONSUMER_CORPUS_PROBE`; the `control` suite leaves that
file out, and CI runs it in the job that builds the probe so the Controller
shards never wait for a Cargo build.

Property tests (Hypothesis) carry `@pytest.mark.property` and pin their known
cases with `@example`. `control/tests/conftest.py` registers two profiles once:
`ci` (the default) is derandomized with 50 examples, so every batch replays the
same inputs and a property can never turn an unrelated batch red; `fuzz`
(`pytest -m property --hypothesis-profile=fuzz`) draws randomly with 20000
examples and is for the scheduled fuzz job and local runs.

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
`scripts/build-recipe-library "$VONK_RECIPE_LIBRARY_ROOT"`. In CI the
`recipe-library` action does it with `build: "true"` and caches the built tree
under the reviewed revision plus the hash of `scripts/build-recipe-library`
(the build's identity); `ci-cache-warm.yml` builds and saves it on main when the revision, the build script or the action changes, and a job that finds it
skips the build without waiting on any other job. The Spark acceptance jobs
(clean Spark in `release-acceptance-core.yml`, the refresh and carry lanes in
`spark-upgrade-acceptance.yml`) use the same action instead of building by hand.
The carry harness receives that root as `--library-root` and reuses it for any
release that names the same revision and the same build script; a release that
names another revision or script is still fetched and built by the harness.

The native Rust agent and its wire contract build only for Linux. The
`control/tests/*_wire_bridge.py` suites consume probes produced by
`scripts/tests/run_agent_wire_contracts.py`, which builds every probe in one
Cargo invocation and exports their paths. The tests never build probes
themselves: without the exported path they skip locally and fail in CI. On
macOS `cargo build` fails on platform-gated code such as
`rustix::fs::openat2`, so run those suites in the Linux/OrbStack or designated
CI lane instead of reading the failure as a regression.

The Rust workspace tests run in parallel (`cargo test --workspace --locked`
with Cargo's default test threads; CI sets no `RUST_TEST_THREADS`). A Rust test
must own its state (a unique `tempfile` directory, an ephemeral port) or
serialise explicitly with a narrow lock. Explicit exceptions: the containerd
fixture's runner-wide `flock` (`vonk-agent/tests/recipe_builder/main.rs`), the
build-egress tests that reach the retained-resolver list (one
static mutex), and `listener_recovery.rs`'s fixed port (alone in its binary).
Held file locks release explicitly on drop (`held_lock.rs`), so a child forked
by another test thread cannot keep one busy.

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
  --frozen pytest -q -m lane -n auto --dist worksteal \
  control/tests/test_catalog_documents_postgres.py \
  control/tests/test_agent_jobs_postgres.py \
  control/tests/test_agent_job_lock_order_postgres.py \
  control/tests/test_run_switch_postgres.py \
  control/tests/test_telemetry_postgres.py \
  'control/tests/test_recipe_operations_*.py' \
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

The web tests include `styles-live.test.ts`: every class a stylesheet selects must be
named by production source, so deleting a component also deletes its CSS.

Generated clients and wire outputs are ignored build inputs. Test setup prepares
them, and `tests/test_generated_contracts.py` compares them with their Pydantic
producers alongside the remaining checked-in installer and qualification
schemas. CI runs `scripts/check-generated-determinism` for control clients and
`--wire` for Rust outputs: both compare two generation runs, including Python
file membership. Existing contract round-trip tests still exercise the real
producer and consumer boundaries.

Python lint, formatting and types share one configuration, the root
`pyproject.toml`, and one locked tool version each (uv.lock, control dev group).
Ruff runs its default rules plus the families in `[tool.ruff.lint] extend-select`;
every ignore, global or per file, states its reason next to it. `extend-include`
makes every extensionless file in `scripts/` and `bin/` Python unless
`extend-exclude` names it as a shell script, so a new script is either linted or
fails Ruff loudly. Imports sit at module level (PLC0415, E402): a cycle is broken
by moving shared definitions into a lower module, never by a function-local or
`TYPE_CHECKING` import, and `tests/test_package_import_direction.py` keeps the
runtime import graph of the four installable packages acyclic. Inside those
packages imports are absolute (TID252); tests, scripts and tools keep relative
ones, whose absolute name depends on how pytest roots them.

`scripts/check-python-types` runs pyright in `standard` mode plus the nine strict
checks listed in `[tool.pyright]`, on the Linux platform, over `include` and the
extensionless scripts Ruff selects; `agent_protocol/src` is checked as Python
3.12. There is no baseline: a justified site carries
`# pyright: ignore[rule]` with its reason, and reportUnnecessaryTypeIgnoreComment
fails an ignore that is no longer needed. The commit hook passes the staged
files Ruff treats as Python; CI checks the whole tree with the same commands.
Both run pyright through `pyright_environment()` in `scripts/check_environment.py`:
the version is the one `uv.lock` installs (`importlib.metadata`), with the nodejs
wheel and no global Node, so hook and CI cannot use different pyright builds.

New syntax is checked by `scripts/check-added-lines [PATCH_FILE|-]`, which consumes
an externally supplied unified diff. CI creates that patch against its explicit
PR/merge-group base. Only added lines can fail. The adapter, scanners and fixture
tests never read Git.
The pre-commit hook (`scripts/check-staged-code`) runs the same guards on the
staged diff with the same path set as CI, preparing the control environment
through the same preparer as the Python checks. A violation fails the commit.
If a staged file also has unstaged edits the hook names it and skips the guards
for that commit (CI still gates).
There are no debt counts, category ledgers, baseline updates, or PR count reports.

In Rust and TypeScript a contract literal is new only when the change spells
that word more often in a file than its removed lines did, so a formatter reflow
or an edit of an old line passes while an extra spelling fails every added line
that carries it. Python keeps reporting every added line the scanner flags: its
scanner also reports reason codes that are not words and skips comments and
docstrings the removed text would count.

The guards reject new handwritten contract literals, untyped mapping annotations,
provenance comparisons, security refusals outside ingress owners, unbounded waits,
read refusals, a captured Docker listing or ranged inspection in the helper (read it line by line),
anti-principle
test assertions, vonkctl tests that assert wording in printed output (`printed-output`), short wall-clock limits on test work, and the existing
coordination syntax violations. The `job-limits` guard refuses an added `MAX_INPUT_*`, `MAX_OUTPUT_*` or `MAX_TIMEOUT_SECONDS` assignment outside `agent_protocol/src/vonk_agent_protocol/job_limits.py`, the one owner of artifact-job limits. The retry guard also rejects attempt/repetition
comparisons that end standing intent or remove its next action; explicit
cancellation and request-local retries remain valid. A broad handler that parks
or defers work must consult `disposition()` or `is_definite()` first
(`retry-dispositions`). Retention never decides "is this the head" by content
digest (`head-digests`), and no reader matches a cached model set by its
first-requester Model digest, nor reads a Spark's installations by Model digest
(`set-first-requester`). A whole-tree policy-user
check requires ongoing-intent scheduling for durable recovery owners. Small fixtures
prove that added violations fail, untouched old occurrences do not, and bounded
waits and security ingress remain valid. Detection is a syntax check; behavior,
process and PostgreSQL tests establish recovery and fresh admission.

The model registry, Rust serde and TypeScript shape registries,
and generated wire/client/vocabulary checks retain their existing contracts.
Controller startup and sparse-serialization exceptions remain semantic registries.
Lifecycle writer ownership remains checked without a baseline.

The lifecycle also has a hardware canary that nothing in CI runs:
`scripts/lifecycle-canary` drives one load, one cancel during start and one
Controller-restart-tolerant wait through `vonkctl` and asserts that no operator
wait lacks an action and no admission is stuck. It is run by hand after
lifecycle releases; see [the runbook](runbooks/lifecycle-canary.md).

TypeScript uses pinned Biome (`control/web/biome.json`: the recommended rules, minus `noNonNullAssertion`, `noDescendingSpecificity` and `noImportantStyles`) for linting and formatting; every control/web CSS file (`src/**/*.css`) is checked by Biome too.
CI runs `npm run lint` (`biome ci .`, the whole tree, on pull requests and main alike); the commit hook runs the same `biome ci` on the staged files. A `biome-ignore` comment states its reason.
`tsconfig.json` carries the strict flag set (`exactOptionalPropertyTypes`, `noUncheckedIndexedAccess`, `erasableSyntaxOnly`, `verbatimModuleSyntax` and the rest); fix a finding in the code.
`npm run typecheck --prefix control/web` checks both the application and browser
tests/configuration; `npm run build` also checks application types. Generated
contracts remain owned by their generators. Rust hooks run pinned formatting and
offline Clippy/compiler checks, using the Linux VM on macOS.

Rust lint levels live in one place: `[workspace.lints]` in the root `Cargo.toml`
(rustc lints, `unsafe_code = "forbid"`, clippy pedantic minus ten threshold or
taste lints, and twelve restriction lints for behaviour), inherited by every
crate through `[lints] workspace = true`, with `clippy.toml` holding the lint
configuration (unwrap, expect and panic allowed inside tests; raw `flock`
disallowed outside `held_lock.rs`). The hook and every CI clippy step run the
identical `cargo clippy --workspace --all-targets --frozen -- -D warnings`
(CI runs it after `cargo fetch --locked`; the hook is offline), so a new
warning fails both. Silence a lint only
with a narrow `#[expect(lint, reason = "...")]` at the item or statement; a
test, example, build-script or generator crate root may carry one crate-level
`#![expect]` for unwrap, expect and panic. `#[allow]` is itself linted. Commit checks prepare missing or stale locked Python and TypeScript dependencies
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
by the pre-commit hook, which runs lint, format, types and the added-line
guards. See the
[development workflow](runbooks/development-workflow.md#generated-artifacts-and-release-evidence).

### Image vulnerability scanning

One scanner stays, because a finding blocks something: the Trivy gate in
`.github/actions/image-scan` (HIGH/CRITICAL, fixable only, OS packages and
libraries, skipping `/usr/local/lib/skopeo`, the upstream binary). The findings
table is in the job log; nothing is uploaded to code scanning, and no workflow
holds `security-events: write`. It gates the `api` and `worker` images in
two places, both on the digest that ships:

- the lane's `Candidate image scan and smoke` job (`release-acceptance.yml`),
  whose failure fails `lane-proof`, so the batch cannot merge; Release's
  `dev-images.yml` reuses that evidence for the identical digests or scans again;
- the `v*` tag's `publish-images` job in `stable-release.yml`, before it promotes
  the accepted dev digests.

`Controller image build tests` scans nothing; it only builds and tests. LiteLLM
is an upstream application with a thin layer of ours: it is smoked on both
architectures but never scanned, because only a LiteLLM release fixes its
findings (`docs/image-pin-refresh.md`). A finding heals by the next dev build:
`DEPENDENCY_DAY` refreshes the OS and library layers daily.

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
- **Remove tests that do not earn their cost.** A low-value test may be deleted
  when it only repeats an implementation detail or behavior already covered at
  its owning boundary. Prefer fast, useful failure feedback over a growing
  suite that is slow yet still incomplete. Share expensive immutable fixtures;
  preserve each test's independent mutable state.
- **A bug fix ships with the test that fails without it.** Write the test,
  confirm it fails against the old behaviour, then fix. A regression test that
  was never seen failing is an assumption.
- **Test the real seam.** Exercise the actual serialize/store/load path, the
  real marker set, the real CLI parser, the real runner. A stub that replaces
  the very thing under review cannot catch a fault in it — a stubbed
  `QualificationRunner` hid a dead `apply()` path for exactly that reason.
- **Decide on data, not on prints.** A vonkctl test asserts the exit status,
  the requests sent, a typed result or the `--json` document (the
  `vonkctl_json` fixture in `tests/cluster_profiles/conftest.py`), which is the
  public client contract. A renderer decision is a small pure view function
  in `cluster_profiles/cli_render` returning an enum, dataclass or typed value,
  tested on data; the renderer only prints it. Wording is not tested. A secret or terminal control in any output is a leak, not wording, and
  a line shown twice is a duplication fault: absence, count and emptiness
  checks on captured stdout/stderr stay valid, new ones included. The
  `printed-output` added-line guard refuses only a new positive wording
  assertion on captured output (a string literal `in` `.out`, `.err` or
  `.getvalue()`) anywhere else in `tests/cluster_profiles`.
- **Keep the fast tier fast and hermetic.** A host-dependent test belongs in the
  lane tier, which the collection-time marker applies automatically, rather than
  being skipped or made conditional. The fast tier should stay under a couple of
  minutes so it is genuinely the thing you run while iterating, and every test
  stays within the per-test time budget above.

## Time targets

`CI gate` appends the run's timeline to its step summary (`scripts/ci-timeline
RUN_ID` prints the same for any run): every job with its runner wait and
duration, the wall-clock time, runner-minutes and queue-minutes. The targets a
pull request run is held to, measured on that summary:

| Metric | Target |
| --- | --- |
| Wall clock to `CI gate`, median | 35 minutes or less |
| Runner-minutes per run | 120 or less |
| Jobs that wait more than 5 minutes for a runner | none in a quiet hour |

The candidate acceptance (`Exact PR candidate acceptance`) starts only after
`Ruff` and `Repository guards` pass: a push that fails either, or is superseded
within minutes, does not spend the candidate's runner-minutes. Its upgrade
carry, about twenty serial minutes after the candidate is published, runs on a
pull request only when the change can alter the upgrade (see
[the selector boundary](#required-on-every-pull-request)); main carries every merge.

## When the longer jobs run

Container publication and release metadata are protected by the release
environment and external gates. Ordinary pushes do not run CI; a pull request
to `main` runs the required checks and the suites selected for that change. Concurrency cancels
superseded pull-request runs so a stale commit does not consume another
complete check cycle.

Development releases reuse successful same-repository source checks only
when the merged Git tree is identical to the tested tree: a pull request run
tests the synthetic merge of the head onto the base, and a merge-queue run
tests the queue commit that becomes main itself. The receipt binds the event,
repository, PR, run and attempt, parents, tree and selected checks. The
resolver verifies those identities against GitHub and Git before allowing
reuse. Missing or expired evidence runs ordinary CI; inconsistent evidence
fails closed. GitHub's merge queue, which would make main's commit the tested
commit, is only offered for organization-owned repositories; this repository
is owned by a user account, so the merge-queue path stays dormant and the
reuse hits when the merged branch already contained main. Stable tags always run fresh CI. Production builds,
signing and acceptance of the assembled release retain their own checks and
upgrade baseline. PR acceptance cannot substitute for production acceptance.

### Repository guards run for every change

`Repository guards` runs the fast cross-area checks on every CI invocation,
including documentation-only and control-only PRs, merge queues, and releases
that need fresh CI. `CI gate` requires its success independently
of `scripts/select-ci-areas`. The sharded repository suite runs on every full run and excludes the same files/nodes
to avoid running guards twice.

A guard in `tests/test_workflow_assertions.py` requires every `uses: owner/action@<sha>` under
`.github/` (workflows and composite actions, `.yml` and `.yaml`) to name the same SHA for the same action;
Dependabot covers `/.github/actions/*` so a bump moves all sites together.

`tests/repository_guards.py` owns the guard selection and the reviewed scope of
all other test files under `tests/`. The inventory test rejects new or stale
unclassified files, including scans delegated to helpers. A new whole-tree guard
belongs in `GUARDS`; a narrower test needs its owning boundary in `SCOPED`.
Mixed integration files select their cross-area guard nodes individually. The
local command `scripts/test guards` runs them in parallel (`-n auto`; they share no state). The runner
`python3 scripts/repository-guards` prints that selection; `--exclusions`
prints its complement's pytest options. No test reads Git history for this decision.

`tests/test_guard_paths_exist.py` fails when a plain repository path string literal names
nothing. It reads the `added_line_guards/` package, `principle_guards.py`, `repository_guards.py`,
`scripts/select-ci-areas`, the boundary and reader tables (`document_read_boundaries.py`,
`untyped_mapping_boundaries.py`, `whole_collection_readers.py`,
`deterministic_retries.py`, `lifecycle_writer_boundaries.py`) and `tools/*.json`. A prefix
passes when it matches at least one path. There is no allowlist. It cannot see a scope
that is composed (`Path / "a" / "b"`, an f-string, a regex), so write a scope as a plain
full-path literal; move or delete the literal with the code it guards.

`tests/test_package_no_reexports.py` fails when an `__init__.py` under `control/src/vonk_control`,
`src/cluster_profiles` (not the generated `generated_control`) or
`agent_protocol/src/vonk_agent_protocol` binds imported names (`from .x import Y`,
`from vonk_control.x import Y`) or declares `__all__`. Import from the module that defines the
name. The only `__init__` files that import anything import modules, not names: `models/`
registers every mapped table on `Base.metadata`, and `vonk_agent_protocol` imports `contracts`
first because the contracts, recipe operations and host helper modules import each other.
The same test fails a `from module import name` in those packages when `module` only imports
`name` (a facade) instead of defining it, so every name has one defining module and one import
path; importing a submodule by name is allowed.

A test module that grows past about 1,500 lines is split by topic into `test_<area>_<topic>.py`
files next to it, never by line count. The helpers, constants and fixtures the files share live
in `<area>_support.py` (a module without `test_` in its name) and are imported by name. Fixtures
are registered once, by importing them into the directory's `conftest.py`, so no test module
rebinds them. A moved test keeps its body, so the per-test time budget does not re-apply to it.

### Cross-repo nightly

Every other lane pins `vonk-forge-recipes` to a reviewed revision.
`.github/workflows/cross-repo-nightly.yml` runs nightly (and on demand) with
`main` of vonk-forge and `main` of vonk-forge-recipes and runs the recipe
validator that `validate-recipe-library.yml` runs. It only reads, and a failed
job is the alarm; the fix is a pin move or a contract fix in the repository that
drifted, never a gate on unrelated pull requests. The recipes repository owns
its `platform_ref` pin (see `docs/runbooks/supply-chain.md`).

### Fuzzing

`rust/fuzz/` is a cargo-fuzz crate outside the Cargo workspace (its own
`[workspace]`; the job copies the root `Cargo.lock` in, so the fuzzer tests the
shipped dependency set), so no batch compiles it and the pinned stable
toolchain is untouched. `scripts/select-ci-areas` treats a change under it as
fast-tier only (no Rust build, carry or acceptance). Its targets cover the hand-written parsers that
untrusted bytes reach; each states its invariant in its first comment:

| Target | Parser | Invariant |
|---|---|---|
| `image_store_head` | loopback image-store HTTP head | never panics; an accepted head has a servable path |
| `engine_metrics` | `parse_engine` (engine Prometheus text) | never panics; every number in a reading is finite |
| `source_bundle` | `source_bundle_digest` then `materialize_source_bundle` | never panics; nothing outside the root, no links |
| `wire` | claim response, compiled plan, helper request and frames | never panics |

`.github/workflows/fuzz.yml` runs daily (and on demand), as one job of at most 25
minutes: 150 s per target after seeding from the repository fixtures
(`rust/fuzz/seed-corpus`). The corpus lives in the Actions cache, restored by
prefix and saved after `cargo fuzz cmin`; it is never in git. The nightly date
and the `cargo-fuzz` version are exact pins in that one file. No other workflow
calls it and it has no pull request trigger (a workflow test checks this), so a
batch pays nothing. The cost of that trade: a signature change in a target
parser turns the next scheduled run red instead of a batch, and the fix is a
tiny PR.

A crash turns the run red. The job summary carries the minimized input as
base64 and the `fuzz-crash` artifact holds it. The fix PR adds exactly one case
to an existing test of that parser, with the bytes inline (or rebuilt with the
test's own tar builder), and no corpus file is committed. Reproduce locally with
`cargo +<nightly> fuzz run --fuzz-dir rust/fuzz <target> <artifact>` from the
repository root.

The same job then runs the Python Hypothesis properties under the `fuzz` profile
(`pytest -x --hypothesis-profile=fuzz -m property` over the files that carry the
marker; about a minute, killed at 5 minutes, which is red like any failure). A
falsified property prints its example; the fix PR adds it as an `@example(...)` on that
property.

Follow-up: the build-egress parser target once its parser is a library.

### Coverage: code no test executes

`.github/workflows/coverage.yml` runs on `workflow_dispatch` only: no pull
request, batch or release calls it, so it costs no runner-minutes there. Run it
before a deletion or refactor wave. It runs the Python control and repository
suites through `scripts/test` with the same shards as `ci.yml`, and the Rust
workspace through `cargo llvm-cov`, all under coverage. The result is one list,
written to the job summary and to the `code-no-test-executes` artifact (with an
HTML report, kept 7 days): the files and functions with no executed line,
as deletion candidates (`scripts/coverage-unexecuted`). It stores and compares
no percentage and gates nothing. `agent_protocol/src` is not listed because its
tests run in their own consumer environment. The tool versions are pinned once, in the
workflow's `env`.

### Release acceptance before merge

Every integration PR, including acceptance-harness changes, builds an immutable
candidate from its own reviewed merge commit and runs clean NAS, clean Spark and
Spark upgrade-carry acceptance. `CI gate` requires this run. The workflows reuse
`installer-candidate.yml` and `release-acceptance-core.yml`; main runs the same
checks before promotion. The promoted release is the upgrade baseline, never a
substitute for the PR candidate. A workflow dispatch accepts an explicit `ref`.

The candidate images are built natively, one job per architecture, and bound
into one index per role with `docker buildx imagetools create`; the index job
verifies both platforms and their provenance and SBOM attestations with
`scripts/verify-published-image` before anything consumes them. Development and
candidate images also carry an `ai.vonkforge.source.tree` label: the Git tree
they were built from. Acceptance and reuse bind an image to the tree of the
commit they verify, while `org.opencontainers.image.revision` and the embedded
`VONK_BUILD_SOURCE_SHA` keep naming the commit that built it, for operators.
Identical content built for a pull request and for main is therefore one image.
`vonkctl platform` shows both: `Controller build` is the commit that built the
Controller (first 12 characters; `--json` has all of it; for a reused image, the pull request's test merge) and `Controller build
tree` is its tree, which equals the release commit's
(`git rev-parse <release commit>^{tree}`).
An image without the tree label is refused.
Main's release reuses the candidate images of the successful pull request CI run
that tested its exact tree (`scripts/resolve-tested-ci`): when all four verify
for main's commit (`scripts/reuse-pr-candidate-images`), `dev-images.yml` skips
its builds and runs every scan, deep scan, smoke test, publication and signature
on those digests. Otherwise it builds. A reused Controller image reports the pull
request's tested merge commit as its `source_sha`; that commit has main's exact
tree and resolves on GitHub. A pull
request's Controller build writes its own registry cache
(`vonk-forge-api:buildcache-pr-<number>`) beside main's read-only one, and the
weekly CI base image mirror run prunes the caches of closed pull requests.

The lane's `Candidate ARM64 agent packages` job is a two-leg matrix (candidate and
lower lifecycle baseline) that builds both packages in parallel, as `agent-release.yml`
does; each leg uploads its own artifact and `candidate` derives the package file names
from the source's versions. The build stage's critical path is its slowest leg of
package, setups and compose, so a leg shortened alone gains at most the slack to the
next leg.

The lane's amd64 Controller leaf starts the runner-disk cleanup in the background
(`free-runner-disk` with `phase: start`) and waits for it (`phase: finish`, bounded to
300 s, reporting the margin and warning when it is low) right before the image build,
so the 76-117 s deletion overlaps the Python environment and mirror steps (about 0.7-1 min gained; errors from the deletion are written to a file and printed, with a warning, at the wait). The Controller api leaf's per-pull-request
registry cache (both platforms) is exported with `mode=min` (final-image layers; main's cache still
supplies the intermediate stages) instead of `mode=max`.

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

`scripts/retry-dependency-fetch` permits only the acquisition commands listed
in `scripts/retry_dependency_fetch.py`; `tests/test_workflow_retry_commands.py`
fails when any workflow or composite action wraps a command it would refuse,
such as `cargo install`. The Controller and wire
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

Development publication reuses LiteLLM by their exact build inputs.
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
