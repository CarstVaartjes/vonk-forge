# Task 13B2 Spark lifecycle implementation report

## Status

BLOCKED. This commit implements the smallest executable, fail-closed 13B2 seam
requested at the checkpoint. It does not complete the real-controller Spark
lifecycle and cannot produce a Spark gate report in CI because the `run`
orchestration command is not implemented. Promotion therefore remains closed.

Base: `56e4b8e2`

Branch: `feat/fresh-install-foundation`

## Implemented executable seam

- Added `tests/acceptance/test_spark_lifecycle.py` with a real
  `check-publication-graph` command. It binds candidate and acceptance-only
  baseline documents to the exact channel, version, source SHA, generation,
  platform, immutable image graph, and strictly ordered versions.
- The graph command requires and hashes all four package objects: candidate and
  baseline packages for both `linux-amd64` and `linux-arm64`. Paths must remain
  inside the immutable generation prefixes, records must match exact size and
  digest, images must be digest pinned, and selected candidate/baseline package
  digests must differ.
- Added an `emit-report` command that accepts only canonical, exact-run evidence
  with every lifecycle phase and proof. It requires one-use pairing, the full
  development-slice state sequence, renewal/rejection/preservation proofs,
  changed version/package/build/binary identities, direct Rust-agent health,
  both native graph platforms, and explicit synthetic CDI provenance. It emits
  schema-2 reports atomically and never reports physical GPU evidence.
- Hardened `install-release-publication accept` so Spark gates are rejected in
  schema-1 reports. Schema-2 Spark reports must carry the exact native platform
  gate set and exact CI-only synthetic CDI provenance; both native platforms
  must be present before acceptance can be signed.
- The acceptance authority now makes the existing workflow fail closed: its
  schema-1 shell Spark reports are rejected and cannot be signed. Workflow
  orchestration was deliberately not wired to a nonexistent `run` command.

No pairing token handling, runtime bypass, Python agent, supervisor/A-B slot,
migration, `node.probe`, SSH, mutable baseline pointer, or external controller
state was introduced. The four pre-existing unrelated worktree edits were not
modified or staged.

## TDD evidence

### Red

- Dual-architecture graph test failed with exit 2 because
  `tests/acceptance/test_spark_lifecycle.py` did not exist.
- Report-ownership test failed because `emit-report` was not a recognized
  command.
- Acceptance-authority test failed with `behavioral gate report is invalid`
  when supplied the required platform/provenance-bearing schema-2 reports.

### Green

- Focused graph and report contract: 2 passed.
- Focused schema-2 acceptance authority: 2 passed.
- Combined publication graph, workflow, and acceptance-authority suite:
  37 passed in the final focused run.

## Remaining exact blocker

The specific repository interface blocking the Python `run` command is the
native workload architecture contract. `rust/crates/vonk-agent/src/workloads.rs`
rejects every workload architecture except `linux/arm64`, and
`schemas/global/container-runtime-policy-v1.json` compiles that same fixed
architecture into the direct Rust agent. The publication matrix, however,
assigns `spark_job` to native `linux-amd64`. A real Docker/Podman canary cannot
cross that boundary on the amd64 runner, and an emulation/wrapper bypass would
violate the brief.

The next executable sub-slice must first decide and implement one architecture
contract without weakening production runtime policy: either move the real
canary gate to the arm64 matrix member while retaining native package graph
checks on both runners, or add an explicitly approved amd64 workload policy.
After that decision, the Python `run` command and these real boundaries remain
mandatory:

1. Generate and start an isolated same-generation candidate NAS Compose bundle,
   then prove PostgreSQL, Caddy enrollment/controller ingress, Step CA,
   generation, and exact image graph.
2. Reach the canonical browser/admin ingress without bypassing the private
   tailnet boundary, create one enrollment grant via the public API, and feed
   its token only through `runtime.run_interactive(..., forbidden_values=...)`.
3. Install the immutable lower package with the literal baseline Spark curl and
   prove the direct Rust agent reaches the controller.
4. Materialize a native executable CDI/device fixture that lets real Docker's
   `--gpus all` path run the CPU-safe canary without a Docker wrapper or a
   physical-GPU claim.
5. Run the existing `development_slice_client` lifecycle through build/import,
   distribute, install, run/response, stop, and uninstall.
6. Force a genuinely due renewal with acceptance-only service configuration,
   then prove unchanged node identity, changed serial, durable old-serial
   recording/rejection, and direct-agent health.
7. Invoke the strictly newer candidate Spark curl and prove preserved config
   and private identity plus changed semantic/package/build/binary digests and
   native self-test/controller observation.

Until those boundaries succeed on both native CI runners, no Spark report exists
and Task 13B2 remains BLOCKED. Physical DGX Spark evidence remains a separate
stable-rollout requirement and is not represented by this synthetic CI seam.

## Contract-seam review remediation (base `6e5eae87`)

Status: DONE for the requested interface-safety review. The parent 13B2 rollout
remains BLOCKED only on the deliberately deferred real-controller `run`
orchestration. No lifecycle report can be signed from the current workflow's
schema-1 shell output.

The production workload-policy ruling in the updated brief supersedes the
architecture blocker recorded above. `spark_job` is now owned by native
`linux-arm64` everywhere: AMD64 owns `spark_amd64` and `spark_pairing`; ARM64
owns `spark_arm64`, `spark_job`, `spark_renewal`, and `spark_upgrade`. The
workflow contract, report emitter, and acceptance authority have exact tests
for this assignment, including rejection of the previous assignment.

### Bound lifecycle authority

- Added one shared strict lifecycle contract used by both `emit-report` and
  `install-release-publication accept`, eliminating producer/signer schema
  drift.
- Schema-2 reports now carry the complete canonical lifecycle object, including
  exact platform phases and the complete proof object. The acceptance authority
  validates that object before signing instead of trusting gate or synthetic-CDI
  labels.
- The bound publication graph includes channel, source SHA, generation,
  candidate and acceptance-only baseline versions, selected native package
  identities, all candidate/baseline package identities for both native
  architectures, and the immutable image-graph digest.
- AMD64 proof requires exact baseline installation architecture/package/version,
  one-use pairing, node identity, controller generation, and direct Rust-agent
  health.
- ARM64 proof additionally requires the complete canary state sequence and
  deterministic response identity, exact synthetic CDI provenance, changed
  semantic/package/build/binary identities, due renewal with a changed serial,
  durable rejection of the exact old serial, preserved node/config/private
  identity, and direct Rust-agent health.
- Exact-object validation rejects missing or extra proof fields. Negative
  authority tests reject reports with no lifecycle, missing phases/proofs,
  pairing reuse, unchanged renewal serial, accepted old serial, changed node or
  private state, unchanged build identity, indirect agent health, incomplete
  dual-native graph evidence, mismatched package identity, or false CDI
  provenance. No rejected case creates a signed acceptance receipt.

### Descriptor-safe immutable graph reads

`_verified_artifact` no longer resolves and reopens pathnames. It opens every
absolute-root and relative-artifact component with descriptor-relative
`O_DIRECTORY|O_NOFOLLOW`, opens the final file with `O_NOFOLLOW`, requires a
single-link regular file of the recorded size, hashes only from that descriptor,
and compares device, inode, link count, size, mtime, and ctime before/after the
read. Tests prove rejection of a symlinked parent, symlinked final file,
hardlink ambiguity, and a pathname substitution during descriptor reading.

### TDD and verification evidence

Red evidence was recorded before implementation:

- Complete structured evidence was rejected as `lifecycle proof is incomplete`.
- The acceptance authority rejected the new schema-2 lifecycle shape as a
  generic invalid report and could not validate its proof.
- The former AMD64 `spark_job` assignment remained accepted by the old contract.
- Symlinked parent and final-file artifacts both passed graph validation.
- The old path reader never reached the descriptor-race hook.
- The workflow architecture-assignment test observed `spark_job` on AMD64.

Final green evidence:

- `.venv/bin/pytest -q tests/test_spark_lifecycle_contract.py tests/scripts/test_install_release_publication.py tests/test_installer_publication_workflow.py`
  — 48 passed in 16.72s.
- `uvx --from ruff==0.16.1 ruff check scripts/install-release-publication scripts/spark_lifecycle_contract.py tests/acceptance/test_spark_lifecycle.py tests/test_spark_lifecycle_contract.py tests/scripts/test_install_release_publication.py tests/test_installer_publication_workflow.py`
  — all checks passed.
- `git diff --check` — passed.

No full lifecycle orchestration, placeholder proof input, pre-generated gate
evidence, skip, security bypass, or fabricated passing report was added. The
workflow remains fail closed until the real controller-backed lifecycle creates
evidence that satisfies this contract.

## Contract-seam review remediation round 2 (base `2debb4df`)

Status: DONE for the immutable-publication authority finding. The parent 13B2
rollout remains BLOCKED only on the separately deferred real-controller `run`
orchestration.

### Authority-owned publication graph

`install-release-publication accept` now requires three explicit local inputs:
the candidate release object, the acceptance-baseline release object, and their
immutable object root. Before reading Spark reports or signing a receipt, the
authority independently:

- requires both release paths to be the exact channel/generation paths beneath
  that object root;
- descriptor-traverses and reads both canonical release documents without
  following any path component or final-file symlink;
- binds both release identities to the requested channel, generation, source
  SHA, and candidate version, and requires the baseline's acceptance-only
  identity and strict Debian version ordering;
- requires the candidate and baseline immutable image graphs to match, contain
  only digest-pinned non-mutable references, and records their canonical graph
  digest;
- verifies the exact package records, immutable paths, sizes, and descriptor-
  streamed SHA-256 identities for candidate and baseline packages on both
  `linux-amd64` and `linux-arm64`;
- rejects hardlink ambiguity, symlinked components/files, concurrent
  substitution, missing objects, matching baseline/candidate package identity,
  and any release-record/object mismatch; and
- recomputes the exact per-platform graph object once from those four package
  objects, then requires each submitted Spark report's embedded graph to equal
  the corresponding authority graph before signing.

The descriptor and graph implementation now lives only in the shared
`scripts/spark_lifecycle_contract.py` contract. Both
`check-publication-graph` and the acceptance authority consume it, so report
production and signing no longer maintain separate path/digest protocols.
Package contents are hashed as bounded streams from verified descriptors and
are not retained in authority memory.

### Immutable CI handoff

The aggregate acceptance job now downloads the exact immutable candidate
artifact named by the authority channel and candidate generation. It passes
explicit local candidate release, baseline release, and object-root paths to
`accept`. This uses only the immutable same-run GitHub artifact; no mutable
channel pointer or network publication lookup is involved. Native ownership is
unchanged: `spark_job` remains ARM64-only.

### TDD evidence

Red evidence was recorded before production changes:

- A fully canonical and internally consistent pair of Spark reports with an
  invented ARM64 candidate package digest was signed successfully (`returncode
  0`), proving that JSON-only validation remained forgeable.
- After adding the required authority inputs to the positive fixture, the CLI
  failed with `unrecognized arguments: --candidate-release ...
  --baseline-release ... --object-root ...`.
- The workflow contract failed because `Download exact candidate publication
  graph` did not exist.

The positive authority tests now derive graph evidence from actual assembled
release records and actual package bytes. Added negative tests reject an
internally consistent invented digest, wrong object root, wrong candidate
release, cross-generation release path, and a missing native package object.
The pre-existing symlinked-parent/file, hardlink, dual-native graph, and
substitution-race tests now exercise the same shared verifier used by the
signer.

### Verification evidence

- `.venv/bin/pytest -q tests/test_spark_lifecycle_contract.py tests/scripts/test_install_release_publication.py tests/test_installer_publication_workflow.py`
  — 54 passed in 19.78s.
- `uvx --from ruff==0.16.1 ruff check scripts/install-release-publication scripts/spark_lifecycle_contract.py tests/acceptance/test_spark_lifecycle.py tests/test_spark_lifecycle_contract.py tests/scripts/test_install_release_publication.py tests/test_installer_publication_workflow.py`
  — all checks passed.
- Parsed `.github/workflows/installer-publication.yml` with `yaml.BaseLoader` and
  ran `bash -n` over all 16 Bash steps — passed.
- `git diff --check` — passed.

No full lifecycle orchestration, pre-generated proof, mutable publication
lookup, architecture-policy change, or CI security weakening was introduced.
