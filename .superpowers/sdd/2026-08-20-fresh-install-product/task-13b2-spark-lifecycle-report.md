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
