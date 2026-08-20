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
