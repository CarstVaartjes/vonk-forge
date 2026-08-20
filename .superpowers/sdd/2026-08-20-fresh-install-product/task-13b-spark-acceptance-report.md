# Task 13B Spark acceptance implementation report

## Status

BLOCKED. This commit is a coherent prerequisite slice, not a completed Task 13B
publication gate. The executable real-controller lifecycle entry point
`tests/acceptance/test_spark_lifecycle.py` is not present yet, so the existing
`spark-acceptance` workflow matrix must not be treated as acceptance evidence.

Base: `f5515560`

Branch: `feat/fresh-install-foundation`

## Implemented prerequisite slice

- Extracted the development-slice HTTP protocol boundary into
  `scripts/development_slice_client.py` and retained the existing
  `scripts/run-development-slices` behavior through imports. This is the client
  that the Spark lifecycle must reuse rather than introducing another protocol
  implementation.
- Extended the PTY acceptance runtime with process-tree argv/environment scans.
  A caller can mark pairing-token values forbidden; discovery in the interactive
  process or a descendant terminates the process group and fails acceptance.
- Added a narrowly versioned `--acceptance-baseline` package build mode. It only
  accepts an exact lower `MAJOR.MINOR.PATCH~acceptance.1+g<12 hex>` version and
  compiles that semantic identity into the direct Rust binary. Ordinary package
  builds still require exact Cargo semantic-version equality.
- Added deterministic lower-baseline metadata and dual-architecture baseline
  package outputs/artifacts to the reusable package action and its CI/release
  callers. The baseline artifact has seven-day retention and is separate from
  the promotion candidate.
- Bound both lower architecture packages, their content digests, and their
  version into publication generation identity. The assembly emits a signed,
  immutable `acceptance-baseline/release.json` graph with
  `acceptance_only: true`; no endpoint or mutable channel pointer references the
  baseline.
- Updated Spark setup release resolution so the signed `acceptance_only` field
  selects only the immutable baseline prefix. This is release-document binding,
  not a runtime bypass.
- Wired the authority and candidate publication jobs to discover, download, and
  assemble the lower baseline artifact for both native architectures.

No Python agent, supervisor, A/B slots, migration path, `node.probe`, generic
production job, SSH controller dependency, 1Password dependency, or external
secret/key operation was added.

## TDD evidence

### Red

- Reusable protocol client: importing `development_slice_client.Client` from an
  external process failed with `ModuleNotFoundError` before extraction.
- PTY secrecy: the behavioral process-tree test failed because
  `run_interactive()` did not accept `forbidden_values`.
- Lower package: `scripts/build-agent-deb --acceptance-baseline` failed as an
  unknown argument.
- Baseline metadata: exact development/production metadata assertions failed
  because baseline version, package names, and artifact name were absent.
- Package action: the dual-architecture lower-baseline behavior test failed
  because the action had no immutable baseline upload.
- Publication graph: assembly rejected `--agent-baseline-package`; after adding
  the input, a duplicate Spark bootstrap output name exposed a collision and
  failed closed.
- Spark setup: the unit test for baseline/current artifact-prefix selection
  failed because the release-prefix function did not exist.

### Green during development

- `scripts/tests/test_run_development_slices.py`: 62 passed.
- `tests/test_acceptance_runtime.py`: 10 passed.
- Focused package-baseline tests: 2 passed.
- `tests/scripts/test_agent_package_metadata.py`: 19 passed.
- Focused package-action, publication-graph, and Spark setup baseline tests each
  passed after their implementation changes.

## Final verification evidence

- Focused Python regression command covering publication workflow, package
  action, package build/metadata, publication assembly, PTY runtime, and the
  development-slice client: `198 passed in 52.55s`.
- `cargo test --locked -p vonk-agent`: all agent unit/integration/doc tests
  passed, including pairing-token argv rejection, runtime identity, renewal,
  inventory, recipe build, and workload lifecycle boundaries.
- `cargo test --locked -p vonk-spark-setup`: 4 unit tests, 23 privilege tests,
  and 7 process-boundary tests passed; no failures.
- Pinned `ruff==0.16.1` on every changed Python file: `All checks passed!`.
- YAML parsing for the changed action/workflows: `parsed 4 YAML files`.
- ShellCheck 0.11.0 on the four changed Bash run blocks, with GitHub-provided
  environment-name diagnostics and literal `^{commit}` diagnostics excluded:
  exit 0 with no findings.
- `git diff --check`: exit 0.

## Missing exact slice / CI-only gap

The next implementation slice is exact and remains mandatory:

1. Add focused failing tests and `tests/acceptance/test_spark_lifecycle.py`.
2. Start a same-generation isolated Compose controller with real PostgreSQL,
   Caddy enrollment ingress, and Step CA; prove its generation and image graph.
3. Install the immutable lower package through the real Spark curl, create one
   administrator enrollment grant, and pair with the token only through the PTY
   forbidden-value boundary.
4. Materialize an architecture-correct synthetic CDI fixture, label it
   synthetic, and invoke the existing development-slice lifecycle against the
   real API so a real CPU-safe Docker/Podman canary reaches build/import,
   distribute, install, run/response, stop, and uninstall.
5. Force real due renewal with ephemeral acceptance-only service configuration;
   query PostgreSQL for the same node, changed serial, recorded old serial, and
   verify rejection of the old credential.
6. Invoke the strictly newer immutable candidate Spark curl and prove preserved
   config/node/private state plus changed semantic/package/build/binary digests,
   native architecture, and successful self-test at the controller.
7. Make the Python entry point, not an unconditional workflow `jq`, emit the
   generation-bound report only after every assertion succeeds. Replace the
   current matrix skeleton and add fail-closed workflow tests.

Until that slice exists and runs on both `ubuntu-24.04` and
`ubuntu-24.04-arm`, there is no Spark acceptance gate. Physical NVIDIA Spark
validation also remains required before stable rollout; CI synthetic CDI
evidence cannot replace it.

## Worktree preservation

The following pre-existing user modifications were not edited or staged by this
slice:

- `control/web/src/components/library-actions.test.tsx`
- `deploy/compose/Caddyfile`
- `deploy/compose/tests/test_agent_ingress.py`
- `docs/runbooks/platform-operations.md`

## Task 13B1 prerequisite review fix round

Base: `0d147c42`

Scope remained limited to the reviewed prerequisite behavior. Task 13B2 and
`tests/acceptance/test_spark_lifecycle.py` were not started.

### Fixes

- Extended `scripts/verify-agent-deb` by exactly one canonical alternative:
  `MAJOR.MINOR.PATCH~acceptance.1+g<12 lowercase hex>`. The real builder output
  now crosses the verifier boundary successfully; ordinary, lifecycle, and
  development grammar remains unchanged.
- Replaced native lifecycle semantic equality with two strict checks:
  `dpkg --compare-versions` must establish both lower Debian package ordering
  and lower canonical semantic ordering. The helper now executes baseline
  `--version` and self-test before upgrade, verifies native architecture and
  build/binary digest shapes, and requires the candidate semantic, build, and
  binary identities to be strictly newer/different after upgrade.
- Bound Clap's `--version` output to the build-script-provided
  `VONK_AGENT_SEMANTIC_VERSION`, matching runtime identity and self-test. A real
  compiled Rust integration test covers CLI output, self-test, architecture,
  build digest, binary digest, and pass state under both the ordinary candidate
  build and an explicit `0.0.0` baseline build.
- Added synchronous forbidden-value inspection of every argv element and every
  environment key/value before `pty.fork`. Runtime monitoring now follows the
  PTY process group independently of parent relationships, so reparenting or
  reaping the root process does not terminate descendant inspection. Error
  cleanup terminates the process group even if the root was already reaped.

### TDD red evidence

- Real acceptance-baseline builder/verifier regression failed with verifier
  JSON `{"error": "package version is not canonical", "ok": false}`.
- Lower-baseline native lifecycle regression exited 2 with
  `native lifecycle package semantic versions disagree`.
- The compiled `0.0.0` Rust identity regression reported
  `left: "vonk-agent 0.1.0\\n"`,
  `right: "vonk-agent 0.0.0\\n"`.
- Fast-exit argv, environment-key, environment-value, and delayed reparented
  descendant PTY regressions all failed with `DID NOT RAISE AcceptanceError`.

### TDD green evidence

- Real baseline builder/verifier regression: 1 passed.
- Native lower-baseline-to-candidate lifecycle executable:
  `native linux-amd64 package lifecycle: PASS` and
  `native direct-package lifecycle helper: PASS`.
- Compiled baseline semantic identity integration: 1 passed with
  `VONK_AGENT_SEMANTIC_VERSION=0.0.0`.
- PTY fast-exit and reparented-descendant regressions: 4 passed; complete PTY
  runtime file: 14 passed.

### Final evidence

- Full prerequisite pytest command plus new cases: `203 passed in 54.01s`.
- `cargo test --locked -p vonk-agent`: 146 tests passed, 0 failed; doc tests
  also passed.
- Explicit compiled baseline command with
  `VONK_AGENT_SEMANTIC_VERSION=0.0.0`: 1 passed, 0 failed.
- `cargo test --locked -p vonk-spark-setup`: 34 tests passed, 0 failed; doc
  tests also passed.
- Native lifecycle executable and ShellCheck 0.11.0 on both changed shell
  files: exit 0.
- Pinned `ruff==0.16.1` on all changed Python files: `All checks passed!`.
- Existing four action/workflow files parsed as YAML mappings:
  `parsed 4 YAML files`.

The prior CI-only and physical-Spark gaps remain unchanged; this fix round does
not claim the Task 13B publication gate is complete.
