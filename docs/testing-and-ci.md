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

### Per-test time budget

Every test in the repository, Controller and Compose suites must finish its
setup and body within 10 seconds. `tools/pytest_budget.py` checks this after
each test and fails the test that went over, with its measured time. Setting up
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
of sleeping, shrink fixtures to the boundary under test). On a machine that is
knowingly overloaded, `--test-budget-scale=2` (or `0` to disable) relaxes the
check locally. CI runs at the default scale but, because shared runners vary in
speed, fails a test only above twice its budget and reports one between the
budget and twice it as a warning (summary line and `::warning::` annotation).

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
scripts/pull-test-images postgres caddy step-ca tailscale python
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

The PostgreSQL, step-ca, and security-boundary lane tests do not need a Spark or
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
  control/tests/test_step_ca.py control/tests/security
```

### Lint, format, type and generation checks

For implementation changes, run Python lint and types and the TypeScript build.
Each uses the pinned toolchain.

```bash
export UV_CACHE_DIR=/private/tmp/vonk-example-change-uv-cache

# Python lint; ruff is the repository's formatting authority too.
uv run --project control --frozen ruff check .

# Python types. Pyright is locked in the control dev group and resolves imports
# from that same environment.
scripts/build-control-wheel
scripts/check-python-types

# Web behavior and types; the build runs tsc --noEmit before bundling.
npm ci --prefix control/web
npm test --prefix control/web -- --run
npm run build --prefix control/web
```

Generated contracts are checked by ordinary tests, not `--check` scripts:
`tests/test_generated_contracts.py` renders the agent wire schema, the
installer release schema, the qualification campaign schemas and the Controller
OpenAPI documents in memory and fails when a committed copy is stale, naming the
generator to run; the `vonk-wire-codegen` crate's test does the same for
`generated.rs` against the committed `wire.json`. The "Generated control
clients" CI job regenerates the Python and TypeScript clients with Node when a
client input changed and rejects any drift.

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
`control/tests/coordination_boundaries.py`, which the control suite runs over
`control/src` in `test_coordination_boundaries.py`. It
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

Provenance is never compared as identity. `control/tests/content_identity_boundaries.py`,
run over `control/src` by `test_content_identity_boundaries.py`, flags every
comparison (`==`, `!=`, `in`, a query `.where(...)`, `.in_()` or `.filter_by()`)
that touches a provenance field: `build_id`, `recipe_revision_id`, `recipe_id`,
`distribution_publisher`, `distribution_slug`, `distribution_content_sha256`,
`runtime_adapter*`, a slug beside the rest of a catalog identity, and the
builder binary inside a build-input comparison. Sameness of an image, a model
file or a build is decided in `vonk_control/content_identity.py`, which the scan
does not read. Anywhere else a site must be named in
`tools/content-identity-allowlist.json` with a written reason (a security edge,
or ownership, cancellation, retention or navigation by id). Entries are keyed on
file, function and expression, never a line number, an unlisted site fails, and
an entry whose site is gone fails as stale. `test_content_identity_gates.py`
shows the other half: one image presented through different provenance passes
review, install plan, install, start and the profile checks. Comparisons of a
literal (`is None`, `== "vllm"`) are existence and selector checks and are not
sites.

Lifecycle state has one writer. `vonk_control/lifecycle/` holds the shared
lifecycle core: one pure `transition(row, event, adapter, now)` that encodes
the rules of the blocker audit (retry never-executed and idempotent work,
observe an uncertain effect first, no operator wait without an advertised
action, a cancel always completes, supersede instead of block, fail open and
fence closed), one `KindAdapter` protocol per kind, and one reconcile loop that
is off until a kind has moved onto the core. Two scanners guard it, both run by the control suite.
`control/tests/lifecycle_writer_boundaries.py` finds every write to a
lifecycle state (`x.state = ...` on a lifecycle row, a `["state"]` store in a
module that owns one, `update(Model).values(state=...)`, a constructor,
`_set_application_state`) outside the core and allows none: there is no
allowlist, and a new writer fails with the place to move it. `control/tests/blocker_boundaries.py`
finds every place that produces `needs-operator` (or the agent's `waiting-for-operator`) (Python and the Rust
agent) and every fail-closed raise in all of `control/src` (a raise of any
exception class defined there, found through the class bases), and compares them
with `tools/blocker-allowlist.json`: an operator wait needs a verdict and an
advertised action (`KEEP` needs an irreversible effect), a raise belongs to a
`security-edge`, `input-validation`, `already-retried` or `bookkeeping-debt`
family, and `max_debt` / `debt_ceiling.total` only fall. The ten audited modules
(`scope.audited_paths`), where the debt is being paid down, keep
`debt_ceiling.total`; the bookkeeping debt of every other module has its own
`debt_ceiling.unaudited`, which also only falls. Both scanners take
`--list`; the blocker scanner's `--write-baseline` lowers the recorded counts after a fix; a new
site always needs a reviewed entry by hand.
`control/tests/blocker_classifier.py` proposes the category of a new raise by
rule (exception class, then message): `--classify-new` appends one family per
module and category for the sites the allowlist does not list, `--summary` counts
every raise in `control/src` by category and names the ones that are not a family
(builtin `ValueError`/`KeyError`/`TypeError`, `HTTPException`, factory functions).
Read the proposal and move any site it got wrong before committing it.
The same scanner guards the error categories: a `raise` in a lifecycle or
operation path (`scope.guard_paths`: `control/src/vonk_control/lifecycle/` and the
modules that own an operation) must use a class derived from
`SecurityRefusalError`, `InvalidRequestError` or `UnknownOutcomeError`
(`vonk_agent_protocol`), not a bare `RuntimeError`, `ValueError` or a family
`Conflict`. Raises that predate the rule are grandfathered per module in
`categorized_raises.grandfathered`; a module's count and
`categorized_raises.ceiling` only fall, and an unlisted module may not raise an
uncategorized error. Converting a raise lowers the count (`--write-baseline`).

The ways a bookkeeping raise leaves a lifecycle path, in the order to try them:
a reader of a stored document *returns* `Damaged` (`lifecycle/evidence.py`) and
`read_or_rebuild` rebuilds it from evidence or retires it as `Residue`, so no
exception crosses the caller; a request that meets a try-lock refusal repeats its
whole transaction through `bounded_attempts` (`bounded_retry.py`, with
`admission_attempts` for admission contention) and re-raises the last refusal
only after the attempts are spent; and a loop that genuinely retries or reports
an unknown outcome to the lifecycle core is declared in `retry_loops`, with the
reason it retries, so the AST proof in `control/tests/blocker_retries.py` moves
the raises it reaches to the module's `proven-retry` family. A raise that decides
a destructive effect (a removal gate, an uninstall or stop authority, a lease
fence, a reviewed-intent integrity check) stays a `bookkeeping-debt` entry.

A PR that touches lifecycle, raise or allowlist files reports the movement.
`scripts/lifecycle-counts` prints the four numbers (writers, operator waits,
raises, debt) read from the two allowlists, which the ratchets above hold equal
to the code; `scripts/lifecycle-counts report --base origin/main` prints the
`before:` / `after:` block for the PR description (see
`.github/pull_request_template.md`). The `Lifecycle counts` workflow runs
`scripts/lifecycle-counts check` on every PR: if the diff touches a covered file
the description must carry both lines and they must equal the counts at the
merge base and at the head. It checks the report is true; the ratchets decide
whether a rise is acceptable.

A third ratchet keeps the vocabulary the contract owns out of hand-written code.
`control/tests/vocabulary_literals.py` finds string literals equal to a word of
`vonk_agent_protocol.lifecycle_vocabulary` in the Python sources
(`control/src`, `agent_protocol/src`, `src/cluster_profiles`) outside the contract
modules and the Controller's legacy adapter: a *distinctive* word (it contains `-`,
`_` or `.`, such as `waiting-for-operator`, `stop-unconfirmed`, `operation_cancelled`)
or one of the stored state words `queued`, `running`, `succeeded`, `failed`,
`cancelled`. `tools/vocabulary-literals-baseline.json` records what predates the
guard per file, and a new literal, a higher count or an unlowered count fails; the
same test requires the web app (`control/web/src`, except generated files and tests)
to spell none and to import `vocabulary.generated.ts` instead.

Two more tiers are flat at zero and have no baseline. `reason_code` finds a string
equal to a member of a reason-code enum (`vonk_agent_protocol.reason_codes`:
blockers, refusals, warnings and attention codes grouped by domain), or a message
that starts with one (`"run-switch.plan_blocked: ..."`), anywhere in `control/src`
and the web app. `code_position` finds a *free-string code*: a string constant or
f-string in the `code=` / `*_code=` keyword of a call, the first argument of
`make_blocker`, the code argument of an error class or helper that takes one, a
`code` / `*_CODE` class or module attribute, or a `code` parameter default. It
catches a new code that is not in any enum yet, so the code has to be added to its
domain enum first. The CLI ships without the contract package and spells the codes
it renders; `test_the_cli_spells_only_contract_codes` keeps those equal to members.

The `legacy_state` tier keeps the retired state spellings that are ordinary
words (`waiting`, `partial`, `cancelling`, `expired`) out of lifecycle code: a literal
equal to one of them in a statement that also names a state (`row.state`, `state=`,
`*_STATES`, `State.X`) counts. The migration is finished, so its baseline is empty and
`test_no_lifecycle_code_spells_a_retired_state_word` keeps it so. A fourth tier,
`machine_state`, applies the same context rule to the plain words of the contract's
other state machines (`installed`, `uninstalled`, `withdrawn`, `published`,
`stopped`, `verified`, ...; see
`agent_protocol/src/vonk_agent_protocol/state_machines.py`); its baseline is empty,
and the distinctive words of those machines (`withdrawal-pending`, `not-yet-valid`)
are caught by the first tier. There is no exception list: a certificate, an
enrollment grant, an installation record, a distribution assignment, an endpoint or a
catalog sync speaks its contract enum like everything else. `waiting-for-operator` is
found in any context by the distinctive tier. Only the contract's alias table
(`STATE_ALIASES`) may spell the retired words; the CLI, which ships without the
contract, keeps its one copy in `cli_states.py`, and a test keeps it equal to the
contract.

The Rust agent has
the equivalent check in `rust/crates/vonk-agent/tests/protocol_literals.rs`: no
vocabulary word (the lifecycle enums and every enum `ReasonCodeVocabulary` publishes,
including `RuntimePreflightFindingCode` and `HelperErrorCode`) in a string literal of
the agent, helper or protocol crates, no `json!` result body outside tests, and one
struct literal of the runtime preflight finding (the constructor that takes a
`RuntimePreflightFindingCode` member), so a new free-string finding code fails.
Its two residues (`VOCABULARY_RESIDUE`, `TOOL_OUTPUT_WORDS`) only fall.

The lifecycle also has a hardware canary that nothing in CI runs:
`scripts/lifecycle-canary` drives one load, one cancel during start and one
Controller-restart-tolerant wait through `vonkctl` and asserts that no operator
wait lacks an action and no admission is stuck. It is run by hand after
lifecycle releases; see [the runbook](runbooks/lifecycle-canary.md).

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
by the pre-commit hook. See the
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

### Acceptance lane code proves itself on hardware

The Spark upgrade-carry lane runs on every release candidate and gates its
promotion, so a bug in the lane's own code stops every Controller release until
it is fixed. A pull request that changes that code (`LANE_PREFIXES` and
`LANE_EXACT` in `scripts/select-ci-areas`: `tests/acceptance/`, the lane
workflow and the scripts it runs) therefore starts the `lane-proof` job, which
calls the lane on the pull request's merge commit against the current promoted
release and the one before it. `CI gate` requires it green, so the lane change
cannot merge, and with auto-merge it merges on its own once the hardware run
passes. A failed run is re-run from the CI run ("Re-run failed jobs") after the
cause is understood; a push supersedes it.

The lane judges two Controllers older than its own source. It reads each
release's published `control/openapi.json` and refuses to send a request that
Controller's contract does not accept, naming the field (`ControllerContract`).
Send only fields that are set; never serialise an unset optional field as null.

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
