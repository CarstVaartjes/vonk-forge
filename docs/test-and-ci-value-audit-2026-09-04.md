# Test value and CI/CD speed audit

Date: 2026-09-04  
Repository baseline: `origin/main` at `6683ed19`  
Scope: analysis only; this document does not change tests or workflows.

## Executive summary

The test suite has two different problems:

1. A large maintenance-only layer tests the spelling and layout of implementation
   files: workflow step names, exact YAML nesting, exact shell text, path lists,
   checksum files, documentation links, and the absence of historical files.
   These tests do not demonstrate that a package installs, an image starts, a
   service becomes ready, or a release can be consumed.
2. CI spends most of its wall time on repeated dependency installation and
   coarse, unbalanced fan-out. Removing low-value tests will simplify changes,
   but it will not by itself fix the largest runtime bottlenecks.

The desired rule should be:

> Keep a test only when it executes a supported behavior or exercises a real
> artifact/deployment boundary. Do not test orchestration prose.

The highest-value recent example is the LiteLLM regression gate: run the exact
multi-architecture OCI output as the image's non-root user with a read-only root
filesystem, no network, no capabilities, and `no-new-privileges`, and execute the
startup preflight. That test would have caught the actual failure. Assertions
that the Dockerfile contains `USER`, that a Compose environment value starts
with an exact path, or that a workflow contains a named step did not.

Recommended first pass:

- remove roughly 140-160 tests that inspect workflow/YAML/text/checksum/docs
  structure without executing supported behavior;
- remove the redundant `PR contract smoke` job because every selected test is
  already run by the complete suites;
- stop running generated-client work on every PR when the OpenAPI inputs did not
  change;
- avoid two independent `npm ci` installations of the same web dependency tree;
- shard Python by test file and historical duration rather than test-item modulo;
- build only changed development-image roles and add registry cache scopes for
  Hermes and LiteLLM;
- coalesce installer publication triggers while preserving all four real
  NAS/Spark acceptance lanes.

## Evidence and inventory

The repository currently contains approximately:

- 241 test-named files;
- 116,913 lines in those files;
- 1,012 collected root/repository pytest cases;
- 1,937 collected control pytest cases;
- 44 web test/spec files with about 230 declared test cases;
- additional Rust and shell tests outside those pytest counts.

Eleven Python test files directly inspect `.github/workflows` or
`.github/actions`. Together they contain:

- 8,811 lines;
- 239 `test_*` functions.

The orchestration itself is also too large. The workflow/action YAML is about
6,905 lines, led by:

- `.github/workflows/ci.yml`: 1,547 lines;
- `.github/workflows/installer-publication.yml`: 1,204 lines;
- `.github/workflows/dev-images.yml`: 815 lines;
- `.github/actions/agent-package-build/action.yml`: 780 lines.

This is the root cause of much of the brittle testing: important release logic
lives as inline Bash/Python inside YAML, so tests parse the YAML and copy its
text back into a subprocess. A workflow should orchestrate; executable policy
and behavior should live in repository scripts that can be invoked directly.

### Recent PR CI timing

Successful run `33854447958` took about 6m20s wall time and 33 runner-minutes.
The largest jobs were:

| Job | Duration |
|---|---:|
| Admin web browser acceptance | 5m59s |
| Complete admin web suite | 5m35s |
| Generated control clients | 3m12s |
| Slowest control shard | 2m18s |
| PR contract smoke | 1m04s |

Most of the two web jobs was the same dependency installation: `npm ci` took
about five minutes in each job, while the actual unit suite took 24 seconds and
browser acceptance took 23 seconds. On a cache-hit run, the same web `npm ci`
took two to three seconds. This is primarily a dependency-cache/repeated-install
problem, not a test-runtime problem.

The generated-client job repeatedly spent about five minutes installing a very
small TypeScript generator dependency tree. The generated-client tests also run
the generator multiple times within one test file.

The six repository shards each completed in roughly 35-43 seconds in the same
run. Six separate checkouts, Python setups, collections, and process groups are
too much orchestration for that amount of useful test work.

### Recent development-image timing

Development-image run `33853051569` took about 6m11s wall time. Its image build
critical path was:

| Image | Build duration |
|---|---:|
| LiteLLM | 3m57s |
| Hermes | 3m05s |
| API | 1m56s |
| Worker | 1m56s |

All four roles are rebuilt for any matching change. API and worker have targeted
GitHub Actions cache scopes; Hermes and LiteLLM do not. The read-only acceptance
job also reinstalls web dependencies and reruns source/control/web tests that
already passed in PR CI, then spends most of its life waiting for image receipts.

### Recent installer-publication timing

Full installer publication run `33805823937` took about 9m36s wall time and 21.9
runner-minutes. The meaningful work was:

| Acceptance lane | Duration |
|---|---:|
| Clean Spark candidate, ARM64 | 6m48s |
| Clean NAS candidate, native | 4m45s |
| Clean NAS candidate, Docker 29.4.3 | 4m24s |
| Clean Spark candidate, AMD64 | 3m53s |

These lanes are real deployment evidence and should remain. The optimization
target is how often the fan-in starts/cancels publication runs, not removal of
these acceptance lanes.

## Test retention policy

### Keep

Keep tests that do at least one of the following:

- invoke a public function/API and assert externally observable behavior;
- exercise a state transition, retry, failure, rollback, concurrency, or
  idempotency guarantee;
- build the actual wheel, Debian package, OCI image, installer, or source bundle
  and consume that artifact;
- start the real container/service under production-like identity, filesystem,
  network, privilege, and health constraints;
- run a real PostgreSQL, Compose, PKI/TLS, systemd, Podman, browser, or installer
  integration;
- verify a machine-consumed wire or CLI contract through parsing/serialization,
  not by matching source text;
- prove tamper/corruption rejection where digest verification is itself product
  behavior.

Examples worth keeping include:

- the exact non-root LiteLLM OCI startup preflight on AMD64 and ARM64;
- fresh PostgreSQL authority initialization;
- Tailscale reconciliation behavior;
- PKI issuance and hostname-verified TLS;
- native Debian install/upgrade/remove lifecycle;
- rootless Podman OCI import;
- real NAS and Spark acceptance lanes;
- browser flows that represent operator tasks;
- rollback and fail-closed behavior in controller operations.

### Remove

Remove tests whose only proof is one or more of:

- a workflow contains an exact step name or exact command string;
- a job has an exact `needs`, `permissions`, matrix, timeout, path filter, or
  concurrency YAML shape;
- a Dockerfile contains a literal `USER`, `CMD`, or path;
- Compose contains an exact environment path without starting the service;
- an exact number of jobs, actions, architectures, cache entries, or strings is
  present;
- a documentation file contains/omits a token or a runbook URL is non-empty;
- a checksum file exists or is formatted as expected, without consuming the
  protected artifact;
- generated output matches hand-picked source strings already implied by the
  schema/generator;
- historical filenames/modules/commands remain absent;
- test code reimplements GitHub expression semantics or production selection
  logic and compares the copy to expected values.

Checksums require one nuance: a checksum is not evidence that an artifact is
usable. Delete tests that only assert checksum plumbing. Retain a corruption or
wrong-digest test when rejecting corrupt content is a real runtime/security
behavior of an installer, content-addressed store, model manifest, or package
verifier.

## Concrete removal candidates

### Delete entire files

These files add essentially no functional or deployment evidence:

| File | Tests | Reason |
|---|---:|---|
| `tests/test_ci_platform_boundaries.py` | 4 | Mirrors exact CI jobs, step names, runner labels, shard counts, and cache text. CI itself is the proof. |
| `tests/test_agent_repair_workflow.py` | 2 | Asserts workflow prose and path filters; the native repair matrix is the valuable test. |
| `tests/test_agent_upgrade_recovery_workflow.py` | 3 | Asserts workflow and harness source text; the actual recovery harness is already executable. |
| `tests/test_installer_publication_workflow.py` | 22 | Mostly exact YAML topology, matrices, permissions, paths, step environments, and copied inline shell. The real NAS/Spark publication acceptance is the evidence. |

Deleting those four files removes 31 tests immediately without losing product
behavior.

### `tests/test_container_release_workflow.py`

This file has 54 tests and 1,774 lines. Keep only tests that directly execute a
production script or validate a real OCI/receipt parser. The useful core is:

- immutable image publication behavior with a fake registry transport;
- receipt create/aggregate/verify behavior;
- unsafe archive rejection;
- duplicate/wrong identity rejection;
- multi-architecture manifest and attestation binding behavior.

Remove the tests that inspect the exact development or release workflow. In
particular, remove the blocks covering:

- build action counts, platform strings, cache strings, QEMU ordering, and
  concurrency layout;
- exact receipt upload/download step text and exact parallel-group ordering;
- source-contract step names and npm command placement;
- assertions that source files contain `sha256sum`, `jq` expressions, label
  names, or exact path strings;
- release job `needs`, permissions, step ordering, action pins, summary text,
  and exact asset lists;
- checksum-file creation for `vonk-forge-images.env`;
- the final assertion that a checksum-protected release asset exists.

Tests that execute shell extracted from a workflow step should not survive in
that form. If the behavior matters, move the shell into `scripts/` and test the
script. Otherwise delete it.

Expected result: retain roughly 10-14 functional tests and remove about 40.

### `tests/test_agent_release_workflow.py`

This file has 47 tests and 1,478 lines. Retain the test that builds a real agent
package and verifies that site configuration is absent. Move any genuinely
important cryptographic key validation or package-authority behavior into a
standalone production script and test that script directly.

Remove tests that assert:

- exact reusable-action inputs/outputs;
- workflow/job/step names and ordering;
- literal environment boundaries;
- exact artifact paths and package names inside YAML;
- exact concurrency, matrix, permissions, action pins, path filters, cleanup
  step placement, or cache shape;
- presence/absence of inline commands such as `dpkg`, `aptly`, `rclone`, or
  `gh` in a workflow string.

Expected result: remove roughly 40-45 tests. The actual package builder,
verifier, native lifecycle harness, and APT publisher tests remain elsewhere.

### `tests/test_workload_artifact_workflow.py`

Delete the workflow-authority, permission, action, path, cache, and YAML tests.
The runtime-manifest selector and Dockerfile validation cases are functional,
but they are currently extracted from inline workflow text. Move those two
algorithms into normal scripts/modules, retain their functional cases there,
then delete the workflow parser helpers and the remainder of this file.

### `tests/test_fresh_install_legacy_boundary.py`

Keep:

- the functional CLI rejection of retired update commands;
- tests that build wheels and inspect the actual packaged artifact, if absence
  of those modules is still an explicit supported packaging boundary.

Remove:

- lists of obsolete files that must remain absent;
- tests that modules are not importable solely because files were deleted;
- Compose/YAML checks for absent historical environment names;
- workflow text checks for absent historical release mechanisms.

Historical deletion tests accumulate indefinitely and make old architecture a
permanent concern of a greenfield schema-2 system.

### Documentation and dashboard-shape checks

Remove documentation from
`test_operator_tailscale_assets_have_no_acceptance_service_or_policy`; deployment
assets may be validated by behavior, but README/runbook text should not gate CI.

In `deploy/compose/tests/test_observability.py`, remove assertions that every
alert merely has a non-empty summary or an HTTPS runbook URL, and remove exact
dashboard title/UID/panel-shape checks. Retain or add functional validation such
as `promtool check rules`, PromQL behavior against fixtures, Grafana provisioning
startup, and actual Caddy access control.

### Static supply-chain/checksum checks

`tests/scripts/test_verify_supply_chain.py` mixes useful policy behavior with a
large checked-in hash manifest. Remove tests whose only purpose is to prove that
repository bytes, SBOM entries, lock entries, or wheel checksums match another
checked-in file. These are high-maintenance self-consistency checks, not
installability.

Likewise remove:

- checksum-generation tests in the container release workflow;
- tests that packaged schema bytes exactly match repository schema bytes;
- tests that only assert a checksum sidecar exists.

Retain tests that demonstrate an actual consumer rejects a tampered package,
model shard, downloaded installer, source bundle, or content-addressed object.

### Generated-client structural tests

`tests/control/test_openapi_clients.py` is over-specified. It repeatedly checks
exact schema properties and exact generated Python/TypeScript source strings.
Replace this with a small functional set:

1. generate once;
2. require no tracked diff;
3. compile/import the Python client;
4. typecheck/build the web client;
5. make representative typed requests for ordinary JSON and streaming paths;
6. assert the generated public contract contains no secret-bearing fields.

Do not run the generator separately in multiple tests, and do not assert exact
generated source lines.

## CI/CD speed recommendations

### Priority 0: remove redundant work

#### 1. Delete `PR contract smoke`

Every test selected by the `test` job is already included in either the complete
repository suite or complete control suite. The job adds about one runner-minute
per PR and creates a second place where focused test lists drift.

If an early signal is desired, use changed-file-targeted functional smoke tests
and do not also run them unconditionally in the complete suites. Branch
protection should wait on one stable aggregate, not both a duplicate smoke and
the full suites.

#### 2. Remove source-level tests from development-image acceptance

The read-only `build-and-accept` job in `dev-images.yml` reinstalls Python and
Node dependencies and reruns Compose, web, and control source tests after PR CI.
Those tests are not bound to the image bytes. Keep:

- exact image receipts;
- real non-root container startup/preflight;
- OCI manifest/attestation validation;
- rendered Compose validation using the exact image digests.

Remove the local web build, focused control tests, and other source tests from
this post-merge image-publication workflow.

### Priority 1: fix dependency installation

#### 3. Stop installing the same web tree twice

`web-suite` and `web-browser-acceptance` each run `npm ci` for the same lockfile.
Options, in recommended order:

1. cache exact `control/web/node_modules` by OS, Node version, and lockfile hash;
   skip `npm ci` only on an exact cache hit;
2. if cache reliability remains poor, combine unit/build and browser acceptance
   in one job so one install feeds both suites;
3. alternatively create one dependency-preparation job and distribute a
   lockfile-bound artifact, but measure artifact compression/transfer first.

The current `setup-node` cache stores npm's download cache, not the installed
tree. Recent logs show cache misses adding two to five minutes twice in the same
run.

#### 4. Fast-path generated-client verification

Do not install the TypeScript generator on every PR. First generate/compare the
OpenAPI schema using Python only. Run the expensive client generators only when:

- the generated schema changed;
- generator code/configuration changed;
- the generator lockfile changed;
- tracked generated outputs changed.

Unify the tiny `tools/openapi-client` dependency tree with an already cached
Node toolchain if practical, or cache its exact `node_modules` directory. Run
the generator once, then perform compile/import/typecheck checks against that
one output.

This can remove three to five minutes from most PR critical paths.

### Priority 2: make test selection and sharding intentional

#### 5. Add one changed-area classifier

The full Python, Rust, web, browser, generated-client, and platform-integration
families currently run on every PR. Add a small, reviewed classifier job with
coarse ownership areas, for example:

- `rust-agent-package`;
- `nas-spark-installer`;
- `control-api`;
- `web`;
- `compose-runtime`;
- `generated-contract`;
- `recipe-catalog`;
- `docs-only`.

Use the outputs to skip unrelated families while preserving stable aggregate
check names. Shared lockfiles, schemas, workflow definitions, and packaging
roots should conservatively select all affected owners. A docs-only change need
not run product test suites if documentation checks are intentionally removed.

Do not write tests that assert the classifier's exact path list. Test the
classifier as a function with representative changed-file sets.

#### 6. Shard by file and duration, not test item modulo

Both Python suite matrices collect every test, then distribute individual node
IDs by position modulo six. This causes tests from one file to appear in several
shards, repeats module/session setup, weakens `--dist loadfile`, and creates
unstable shard durations.

Use committed historical durations and greedy file-level bin packing. A good
starting point is:

- repository suite: reduce from six shards to two or three;
- control suite: reduce from six shards to three duration-balanced shards;
- use a bounded xdist worker count per shard rather than `-n auto` across six
  runners.

The repository shards currently finish in under a minute, so six runner setups
provide little wall-time benefit. Measure p50 and p95 after each reduction.

### Priority 3: make image publication incremental

#### 7. Build only affected image roles

Development images currently rebuild API, worker, Hermes, and LiteLLM together.
Split role authority so a change can rebuild only affected roles and reuse the
latest accepted ancestral digest for unchanged roles. Preserve the final
all-role receipt/Compose gate before advancing aliases.

Typical ownership:

- `control/**`, shared Python inputs: API and worker;
- `deploy/compose/hermes-agent/**`: Hermes;
- `deploy/compose/litellm/**`: LiteLLM;
- shared Compose/render/publication tooling: all roles.

This would have made the LiteLLM ownership fix rebuild/test LiteLLM without
spending time rebuilding the other three images.

#### 8. Add cache scopes for Hermes and LiteLLM

API and worker already use `cache-from`/`cache-to`; Hermes and LiteLLM do not.
Add role-specific registry/GHA cache scopes, with cache writes only from trusted
main builds. LiteLLM is the current four-minute build critical path.

#### 9. Replace pull-only checks with runnable checks

`verify-runtime-images` pulls every pinned runtime image. Pullability alone is
weak evidence and transfers layers on every matching push. Remove it or fold
the necessary images into the functional acceptance that actually starts or
imports them. Manifest availability can be checked without pulling all layers
when runtime execution is not required.

### Priority 4: reduce duplicated post-merge deployment work

#### 10. Coalesce installer publication fan-in

`installer-publication.yml` is triggered by completion of several producer
workflows. Recent history contains many skipped and cancelled publication runs
for one source generation. Prefer one orchestrator per source commit that waits
for/reuses the required accepted component receipts, then starts exactly one
candidate acceptance and promotion.

Do not remove the native NAS, Docker-compatibility NAS, ARM64 Spark, or AMD64
Spark lanes. They are the most valuable tests in this area.

#### 11. Avoid rerunning recovery acceptance after merge

The ARM64 repair and upgrade recovery workflows run on PRs and again on the
resulting main push. If branch protection requires the PR checks and direct
pushes to main are disabled, the second run is usually duplicate evidence for
the same tree. Preserve a main-push path only for commits that did not arrive
through a verified PR, or reuse an exact tree/SHA-bound acceptance receipt.

This can save roughly nine runner-minutes on merges that touch recovery inputs,
without weakening the required PR gate.

## Recommended implementation order

1. Delete the four whole-file workflow test candidates and the pure
   workflow/text blocks in the larger files.
2. Delete `PR contract smoke` and keep one stable aggregate check.
3. Rewrite generated-client validation to generate once and fast-path unchanged
   schemas.
4. Eliminate duplicate web dependency installs.
5. Change Python sharding to file-level duration balancing and reduce shard
   count.
6. Remove duplicate source tests from development-image publication.
7. Add Hermes/LiteLLM build caches and role-selective image builds.
8. Add coarse changed-area CI selection.
9. Coalesce installer publication and deduplicate post-merge recovery runs.

## Success criteria

The cleanup is successful when:

- a test failure describes a broken supported behavior, not a renamed step or
  moved line of YAML;
- changing workflow organization without changing behavior does not require
  test edits;
- image/container changes are gated by running the exact artifact under its
  production constraints;
- installer changes are gated by actual clean NAS/Spark acceptance;
- ordinary PR CI p50 is below three minutes for a single-area change and p95 is
  below five minutes excluding intentionally selected hardware/native lanes;
- no PR installs the same Node dependency tree twice;
- generated clients are regenerated once and only when relevant;
- repository/control shards have similar durations and no test file is split
  across shards;
- post-merge image publication rebuilds only affected roles;
- one source commit creates at most one full installer candidate acceptance run.

## Bottom line

The suite should become smaller but more expensive per assertion: fewer tests
that read files, more tests that run artifacts. The largest immediate deletion
target is workflow-shape testing. The largest immediate speed target is repeated
Node installation and unconditional generated-client work. The real deployment
lanes are not the waste; they are the evidence the rest of the suite was meant
to protect.
