# Generic Runtime and Spark Wrapper Build Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the DS4 Spark runtime through the generic attested workload-image path and prove its single-stage wrapper on the native two-Spark platform.

**Architecture:** GitHub Actions composes the accepted DS4 runtime digest with a pinned BusyBox stage, copying only its static fabric transport and leaving the DS4 binary byte-identical. The GPU node performs only a small, networkless, single-stage wrapper build from the accepted derived runtime digest; model weights remain separate content-addressed artifacts on local NVMe.

**Tech Stack:** GitHub Actions, Docker Buildx, GHCR, Syft, GitHub attestations, Sigstore, Ubuntu 24.04 ARM64, native Podman 4.9/FUSE-overlayfs, Rust agent, Python control plane and acceptance runner.

## Global Constraints

- Keep `overlay.force_mask=shared`, `fuse-overlayfs`, private operation graphroots, bounded output, and byte accounting unchanged.
- Reusable runtime composition runs only in `.github/workflows/workload-artifacts.yml`; no local release publication is allowed.
- Runtime and base-image references are canonical lowercase digest references; floating tags are forbidden.
- `ghcr.io/carstvaartjes/vonk-forge-workloads` must be anonymously readable before node installation.
- Images contain no NAS credentials, runtime tokens, signing keys, model-provider credentials, or model weights.
- The runtime and workload process remain numeric non-root `10001:10001` with `ai.vonkforge.runtime-interface=v1`.
- A new privileged capability is out of scope; Docker/NVIDIA helper policy and the native NVIDIA stack remain unchanged.

---

### Task 1: Compose a self-contained DS4 runtime

**Files:**
- Modify: `tests/adapters/test_ds4_runtime.py`
- Create: `adapters/deepseek/ds4/Dockerfile.workload`

**Interfaces:**
- Consumes: accepted `ghcr.io/carstvaartjes/spark-ds4` and public BusyBox digest-pinned images.
- Produces: target `runtime`, containing `/opt/vonk/busybox`, label `ai.vonkforge.runtime-interface=v1`, and user `10001:10001`, without changing the legacy DS4 release.

- [ ] **Step 1: Add the failing runtime-contract assertions**

Add `test_generic_runtime_is_self_contained_without_mutating_the_legacy_release`:

```python
assert hashlib.sha256(legacy).hexdigest() == manifest["files"][legacy_path]
assert f"FROM {BUSYBOX_BASE} AS fabric-tools" in generic
assert f"FROM {IMAGE} AS runtime" in generic
assert "COPY --from=fabric-tools /bin/busybox /opt/vonk/busybox" in generic
assert 'ai.vonkforge.runtime-interface="v1"' in generic
assert "ARG " not in generic and "RUN " not in generic
```

- [ ] **Step 2: Verify the new contract fails**

Run: `uv run --frozen pytest tests/adapters/test_ds4_runtime.py -q`

Expected: FAIL because `Dockerfile.workload` does not exist.

- [ ] **Step 3: Implement the minimal derived runtime**

Create the networkless two-stage composition while leaving the legacy Dockerfile and runtime manifest unchanged:

```dockerfile
FROM docker.io/library/busybox@sha256:fc6dddc4c44b1bfe37f41cae8e67d1693828e8f42a91862816d7953e2c9d3f23 AS fabric-tools
FROM ghcr.io/carstvaartjes/spark-ds4@sha256:084d9a9ffa47431842c5dec84de97b058034dec0535b2a563bc5db78c9e14615 AS runtime
LABEL ai.vonkforge.runtime-interface="v1"
COPY --from=fabric-tools /bin/busybox /opt/vonk/busybox
USER 10001:10001
ENTRYPOINT ["/opt/ds4/ds4-server"]
```

Do not add `RUN`, `ARG`, a mutable image, credential, model file, or runtime network action.

- [ ] **Step 4: Verify the adapter and metadata suites**

Run:

```bash
uv run --frozen pytest \
  tests/adapters/test_ds4_runtime.py \
  tests/scripts/test_workload_artifact_metadata.py \
  tests/test_workload_artifact_workflow.py -q
git diff --check
```

Expected: all selected tests pass and `git diff --check` exits zero.

- [ ] **Step 5: Commit, push, review, and merge the runtime source**

```bash
git add adapters/deepseek/ds4/Dockerfile.workload tests/adapters/test_ds4_runtime.py
git commit -m "fix(ds4): include fabric transport in runtime"
git push -u origin fix/ds4-generic-workload-runtime
```

Open a PR, require the complete CI matrix, review the patch, merge it, and record the resulting `main` SHA as `runtime_source_sha`.

---

### Task 2: Bind a reviewed generic build request

**Files:**
- Create: `release/workloads/ds4-v0.5.3-spark-runtime.json`
- Modify: `tests/scripts/test_workload_artifact_metadata.py`

**Interfaces:**
- Consumes: the exact merged `runtime_source_sha` from Task 1 and its Git archive.
- Produces: one canonical `WorkloadArtifactBuild` request for target `runtime` and repository `ghcr.io/carstvaartjes/vonk-forge-workloads`.

- [ ] **Step 1: Start a request branch from the accepted merged source**

```bash
git fetch origin main
git switch -c release/ds4-generic-runtime-request origin/main
runtime_source_sha=$(git rev-parse HEAD)
context_digest="sha256:$(git archive --format=tar "$runtime_source_sha" -- adapters/deepseek/ds4 | sha256sum | awk '{print $1}')"
```

Assert that `runtime_source_sha` is the Task 1 merge and `context_digest` matches `^sha256:[0-9a-f]{64}$`.

- [ ] **Step 2: Add a failing repository-request test**

Add a test that loads every `release/workloads/*.json`, parses it with `WorkloadArtifactBuild.parse`, and checks that the named DS4 request has:

```python
assert request.context == "adapters/deepseek/ds4"
assert request.dockerfile == "adapters/deepseek/ds4/Dockerfile.workload"
assert request.target == "runtime"
assert request.architecture == "linux/arm64"
assert request.output_repository == "ghcr.io/carstvaartjes/vonk-forge-workloads"
```

- [ ] **Step 3: Verify the missing request fails**

Run: `uv run --frozen pytest tests/scripts/test_workload_artifact_metadata.py -q`

Expected: FAIL because `release/workloads/ds4-v0.5.3-spark-runtime.json` does not exist.

- [ ] **Step 4: Create and validate the exact request**

Write canonical JSON with `source_commit` set to the captured `runtime_source_sha`, `context_digest` set to the captured Git-archive digest, target `runtime`, the exact DS4 and BusyBox base references copied from `Dockerfile.workload`, and required SBOM/provenance booleans. Validate it:

```bash
scripts/workload-artifact-metadata request \
  release/workloads/ds4-v0.5.3-spark-runtime.json >/tmp/ds4-request.validated.json
uv run --frozen pytest tests/scripts/test_workload_artifact_metadata.py -q
git diff --check
```

Expected: parser exits zero, the test passes, and the request contains no fields outside schema version 1.

- [ ] **Step 5: Commit, push, review, and merge the build request**

```bash
git add release/workloads/ds4-v0.5.3-spark-runtime.json \
  tests/scripts/test_workload_artifact_metadata.py
git commit -m "release: request generic DS4 runtime artifact"
git push -u origin release/ds4-generic-runtime-request
```

Require complete CI, review that source and context identities match Task 1, then merge.

---

### Task 3: Publish and independently verify the runtime

**Files:**
- No repository files change.
- Preserve private evidence below `.state/development-acceptance/` with mode `0600`.

**Interfaces:**
- Consumes: merged request path and current accepted `main` workflow definition.
- Produces: a `runtime_reference` formed from the fixed
  `ghcr.io/carstvaartjes/vonk-forge-workloads` repository and the validated
  64-hex-character OCI manifest digest, plus attestation evidence.

- [ ] **Step 1: Dispatch only from current main**

```bash
gh workflow run workload-artifacts.yml --ref main \
  -f request_path=release/workloads/ds4-v0.5.3-spark-runtime.json
```

Record the run ID returned by the subsequent exact workflow lookup and watch it to completion.

- [ ] **Step 2: Require every publication job to pass**

```bash
gh run view "$run_id" --json status,conclusion,headSha,jobs,url
gh run watch "$run_id" --exit-status
```

Expected: authorization, exact-source verification, read-only CI gate, build, SBOM, provenance, signing, and digest-only push all succeed.

- [ ] **Step 3: Download and validate workflow evidence**

Download `workload-artifact-$run_id` into a private temporary directory, run `scripts/workload-artifact-metadata result` against the reviewed request, and extract `oci_manifest_digest` from `validated-result.json`. Construct `runtime_reference` only from the fixed repository name and that validated digest.

- [ ] **Step 4: Verify trust and runtime metadata independently**

Run GitHub attestation verification for the OCI subject and inspect the remote manifest/config. Require Linux ARM64, `10001:10001`, `ai.vonkforge.runtime-interface=v1`, the reviewed source revision, and no embedded secret canary. Make the new GHCR package public if its first publication created it as private, then prove anonymous inspection from a process with no registry credential.

- [ ] **Step 5: Record immutable evidence**

Store only public digests, workflow URLs, source SHA, request digest, SBOM/provenance digests, architecture, user, labels, and anonymous-pull result in `.state/development-acceptance/ds4-generic-runtime.json` with mode `0600`.

---

### Task 4: Pin the accepted runtime in the single-stage wrapper

**Files:**
- Modify: `scripts/tests/test_qualify_development_model.py`
- Modify: `config/recipes/development/model-smoke-context/Dockerfile`
- Modify: `config/recipes/development/model-smoke.json`
- Modify: `config/recipes/development/model-smoke-source.json`
- Modify: `docs/audits/development-model-smoke.md`
- Modify: `docs/runbooks/development-agent-workloads.md`

**Interfaces:**
- Consumes: exact `runtime_reference` and evidence from Task 3.
- Produces: a canonical recipe whose source archive and recipe-content digests bind the single-stage wrapper.

- [ ] **Step 1: Change qualification tests first**

Require exactly one `FROM`, the accepted generic runtime reference, no `AS` stage, no `COPY --from`, local wrapper copy, `/opt/vonk/busybox` as the rendezvous default, and equality among recipe/source/qualification runtime references.

- [ ] **Step 2: Verify the old wrapper fails the new contract**

Run: `uv run --frozen pytest scripts/tests/test_qualify_development_model.py -q`

Expected: FAIL because the checked-in wrapper still declares BusyBox as an external stage and pins `spark-ds4`.

- [ ] **Step 3: Implement the single-stage wrapper and regenerate identities**

Replace the Dockerfile with the accepted generic runtime `FROM` plus the local wrapper `COPY`, preserving label, user, and empty entrypoint. Update `model-smoke-source.json`. Use the canonical source-bundle routine from `scripts/run-development-slices` to calculate the new source SHA-256 and byte count, then update only those exact fields in `model-smoke.json`.

- [ ] **Step 4: Regenerate qualification input and run the model verifier**

Use `scripts/qualify-development-model` with the public runtime reference, existing artifact locks, exact single-/multi-node identities, and private output paths. Require status `qualified`, canonical JSON, and mode `0600`.

- [ ] **Step 5: Run focused and repository verification**

```bash
uv run --frozen pytest scripts/tests/test_qualify_development_model.py -q
uv run --frozen ruff check scripts scripts/tests
cargo test -p vonk-agent --manifest-path rust/Cargo.toml
git diff --check
```

Run the repository's complete required CI-equivalent suites before the PR.

- [ ] **Step 6: Commit, push, review, and merge the runtime pin**

Commit only source, recipe, tests, and public documentation; never commit `.state` evidence or tokens. Open a PR, require complete CI, review all digest transitions, and merge.

---

### Task 5: Complete physical single- and two-Spark acceptance

**Files:**
- Modify: `docs/runbooks/development-agent-workloads.md`
- Modify: `docs/audits/development-agent-workload-acceptance.md`
- Modify: `docs/audits/development-model-smoke.md`
- Modify: `docs/runbooks/fresh-development-install.md` when the operator sequence changes.

**Interfaces:**
- Consumes: merged accepted recipe, public runtime digest, signed development agent package, healthy NAS development Compose cohort, and two paired Sparks.
- Produces: reproducible fresh-install instructions and truthful physical acceptance evidence.

- [ ] **Step 1: Redeploy the accepted development cohort without replacing site specialization**

Pull the workflow-created `:dev` images and use the existing NAS project containing exactly `docker-compose.yml` and `secrets/`. Preserve named volumes, direct-fabric CIDRs, PKI, identities, and runtime authority. Require every one-shot to exit zero, API readiness, Caddy DNS, fresh agent inventory, and no continuing proxy errors.

- [ ] **Step 2: Canary the signed Rust package on Spark 2**

Repeat the documented APT inactive-slot stage and supervisor activation used on Spark 1. Require the exact package version, stable new slot, new artifact digest, no rollback, active firewall/helper/supervisor units, and fresh controller inventory.

- [ ] **Step 3: Run the single-Spark acceptance through inference**

Start a fresh private evidence record with
`scripts/run-development-slices --phase model-single`. Require recipe/source
checks, rootless image build, exact archive digest, Docker import,
checksum-verified model installation, start, route publication, and
authenticated OpenAI inference. Confirm the wrapper build remains single-stage
and model weights are absent from the image archive.

- [ ] **Step 4: Prove single-Spark restart persistence and cleanup**

Restart Spark 1's supervisor and the NAS project while preserving state, resume the same evidence record, require fresh inventory and recovered inference, then stop, verify route withdrawal, uninstall, and prove GPU memory/container/route cleanup.

- [ ] **Step 5: Run the two-Spark failure and recovery matrix**

Use `scripts/run-development-slices --phase model-multinode` with the accepted
direct fabric addresses and persistent Docker-aware firewall. Require both
ranks to rendezvous, route and inference success, terminate rank 1, observe
route withdrawal, recover rank 1, observe route republish and inference
recovery, then restart both supervisors and NAS and repeat persistence checks.

- [ ] **Step 6: Perform final cleanup and documentation audit**

Stop, withdraw, and uninstall the two-node deployment. Remove only exact temporary debug paths and the temporary `/etc/sudoers.d/vonktemp` files on NAS and both Sparks; verify passwordless sudo is no longer available. Update runbooks/audits with public workflow links and immutable digests, then run:

```bash
uv run --frozen ruff check .
uv run --project control --frozen pytest control/tests -q
uv run --project agent --frozen pytest agent/tests -q
uv run --frozen pytest scripts/tests tests -q
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace
git diff --check
```

Commit only public documentation and source changes, open a PR, require the complete GitHub CI matrix, review it, and merge.
