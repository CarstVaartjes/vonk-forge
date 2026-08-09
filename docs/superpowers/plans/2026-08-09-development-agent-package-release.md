# Development and Production Agent Package Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish authenticated ARM64 agent Debian packages from accepted `main` commits to apt `dev`, and from trusted stable tags to GitHub Releases and apt `stable`.

**Architecture:** A strict metadata helper derives canonical development and production Debian versions from synchronized workspace metadata. One reusable package-build workflow owns native ARM64 reproducibility, lifecycle, Sigstore, and attestation gates; one reusable apt workflow owns channel-isolated signing and R2 publication. Thin development and production orchestrators enforce their source authority before calling those shared boundaries.

**Tech Stack:** GitHub Actions reusable workflows, Ubuntu 24.04 ARM64, Rust 1.97.1, Python 3.12, dpkg/aptly, GnuPG, R2/rclone, Cosign/Sigstore, GitHub artifact attestations.

## Global Constraints

- Development source is only the exact current `origin/main` tip.
- Production source is only an annotated trusted SSH-signed `vX.Y.Z` tag reachable from `origin/main`.
- Canonical workspace versions in `Cargo.toml`, `pyproject.toml`, `control/pyproject.toml`, and `agent/pyproject.toml` must match.
- Development versions are exactly `X.Y.Z~dev.<commit-epoch>+g<sha12>`; production versions are exactly `X.Y.Z`.
- Package builds are native `linux/arm64`, deterministic, run twice, and require byte equality.
- The package build receives only the persistent agent signing key and its expected fingerprint; apt publishers never receive that key.
- apt `dev` and `stable` use different protected environments, GPG keys, keyring filenames, private state buckets, and concurrency groups.
- No private key, passphrase, cloud credential, runtime secret, or NAS secret may enter Git, images, packages, uploaded artifacts, logs, or attestations.
- All third-party Actions remain pinned to full commit SHAs.

---

### Task 1: Canonical package-channel metadata

**Files:**
- Create: `scripts/agent-package-metadata`
- Create: `tests/scripts/test_agent_package_metadata.py`
- Modify: `scripts/container-release-metadata`
- Modify: `tests/scripts/test_container_release_metadata.py`
- Modify: `scripts/build-agent-deb`
- Modify: `packaging/debian/preinst`
- Modify: `tests/scripts/test_agent_deb.py`

**Interfaces:**
- Consumes: channel, Git ref type/name, full source SHA, commit epoch, and the four committed workspace version files.
- Produces: newline-delimited GitHub outputs `version`, `next_version`, `package`, `artifact_name`, `channel`, and `snapshot`.

- [ ] **Step 1: Write failing metadata and Debian-version tests**

Add subprocess tests that require:

```python
result = run_metadata("development", "branch", "main", SHA, "1786300000")
assert result.stdout.splitlines() == [
    "version=0.1.0~dev.1786300000+g0123456789ab",
    "next_version=0.1.0~dev.1786300001+g0123456789ab",
    "package=vonk-forge-agent_0.1.0~dev.1786300000+g0123456789ab_arm64.deb",
    f"artifact_name=vonk-agent-development-{SHA}",
    "channel=dev",
    "snapshot=dev-0.1.0~dev.1786300000+g0123456789ab",
]
```

Require production metadata to accept only `v0.1.0`, reject mismatched project
versions, malformed SHAs/epochs/refs, and prove with `dpkg --compare-versions`
that development `<` final and the next development build `>` current.

- [ ] **Step 2: Run the tests and verify RED**

Run:

```bash
uv run --frozen pytest -q \
  tests/scripts/test_agent_package_metadata.py \
  tests/scripts/test_container_release_metadata.py \
  tests/scripts/test_agent_deb.py
```

Expected: failures because `agent-package-metadata` and `~dev` support do not exist and stable metadata does not bind the workspace version.

- [ ] **Step 3: Implement strict metadata and version validation**

Implement a Python helper using `tomllib`, exact regexes, bounded epochs, and
fixed repository-root paths. Update `container-release-metadata` to reject a tag
whose version differs from the synchronized workspace version. Permit only
canonical stable SemVer or the exact derived `~dev` form in `build-agent-deb`.
Quote the substituted version in `preinst` so shell metacharacters are never
interpreted.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command. Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/agent-package-metadata scripts/container-release-metadata \
  scripts/build-agent-deb packaging/debian/preinst \
  tests/scripts/test_agent_package_metadata.py \
  tests/scripts/test_container_release_metadata.py tests/scripts/test_agent_deb.py
git commit -S -m "feat(agent): define development package versions"
```

### Task 2: Reusable native package acceptance workflow

**Files:**
- Create: `.github/workflows/agent-package-build.yml`
- Modify: `.github/workflows/agent-release.yml`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_agent_release_workflow.py`
- Modify: `tests/test_container_release_workflow.py`

**Interfaces:**
- Consumes: reusable-workflow inputs `channel`, `version`, `next_version`, `package`, `artifact_name`, and `environment`.
- Produces: workflow outputs `version`, `package`, and `artifact_name`, plus one immutable Actions artifact containing `dist/*`.

- [ ] **Step 1: Write failing reusable-workflow boundary tests**

Require one `workflow_call` package builder on `ubuntu-24.04-arm`, exact input
validation, channel-specific Sigstore identities, an environment-selected
agent key, expected public-key fingerprint verification, two byte-identical
builds, all existing lifecycle gates, attestations, and immutable artifact
upload. Require `ci.yml` and `agent-release.yml` not to duplicate package build,
key-materialization, lifecycle, or Cosign steps.

- [ ] **Step 2: Run workflow tests and verify RED**

```bash
uv run --frozen pytest -q \
  tests/test_agent_release_workflow.py \
  tests/test_container_release_workflow.py
```

Expected: failure because the reusable workflow does not exist and both callers contain duplicated steps.

- [ ] **Step 3: Extract the accepted build without weakening gates**

Move the exact native build, Rust/package checks, offline lifecycle, Sigstore,
GitHub attestation, cleanup, and upload steps into
`agent-package-build.yml`. Verify the Ed25519 public key fingerprint before the
first build. Use `next_version` from trusted metadata instead of calculating a
SemVer patch inside shell. Make callers local reusable-workflow jobs with
least-privilege permissions and no direct secret interpolation.

- [ ] **Step 4: Run workflow/YAML tests and verify GREEN**

```bash
uv run --frozen pytest -q \
  tests/test_agent_release_workflow.py \
  tests/test_container_release_workflow.py
python3 - <<'PY'
from pathlib import Path
import yaml
for path in Path('.github/workflows').glob('*.yml'):
    yaml.safe_load(path.read_text())
PY
```

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/agent-package-build.yml \
  .github/workflows/agent-release.yml .github/workflows/ci.yml \
  tests/test_agent_release_workflow.py tests/test_container_release_workflow.py
git commit -S -m "ci(agent): share native package acceptance"
```

### Task 3: Reusable isolated apt publisher

**Files:**
- Create: `.github/workflows/agent-apt-publish.yml`
- Create: `scripts/agent-apt-metadata`
- Create: `tests/scripts/test_agent_apt_metadata.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_agent_release_workflow.py`
- Modify: `tests/test_container_release_workflow.py`

**Interfaces:**
- Consumes: `channel`, `version`, `package`, `artifact_name`, `environment`, `source_sha`, and channel environment secrets/variables.
- Produces: signed apt distribution `dev` or `stable`, immutable checksum-verified private state, and a job summary containing no credentials.

- [ ] **Step 1: Write failing apt-channel tests**

Test a strict helper that maps only:

```text
dev    -> vonk-forge-dev, dists/dev, vonk-forge-dev-archive-keyring.gpg
stable -> vonk-forge,     dists/stable, vonk-forge-archive-keyring.gpg
```

Require the reusable workflow to verify the package before key materialization,
validate restored state checksums/archive members, reuse an exact existing
snapshot, reject conflicting immutable state, locally verify `InRelease`, copy
public files before state `latest`, and use channel-specific concurrency.

- [ ] **Step 2: Run apt workflow tests and verify RED**

```bash
uv run --frozen pytest -q \
  tests/scripts/test_agent_apt_metadata.py \
  tests/test_agent_release_workflow.py \
  tests/test_container_release_workflow.py
```

- [ ] **Step 3: Implement the reusable apt publisher**

Use fixed metadata from `agent-apt-metadata`; never accept repository names,
distribution names, keyring paths, or state prefixes from a dispatch input.
Materialize GPG files at mode `0600`, configure aptly under `RUNNER_TEMP`, and
restore only the environment's private state bucket. Keep development and
production environment names fixed in their callers. Upload immutable state
first, then the channel's `latest` state after public publication succeeds.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command and parse every workflow as YAML.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/agent-apt-publish.yml .github/workflows/ci.yml \
  scripts/agent-apt-metadata tests/scripts/test_agent_apt_metadata.py \
  tests/test_agent_release_workflow.py tests/test_container_release_workflow.py
git commit -S -m "ci(agent): isolate signed apt channels"
```

### Task 4: Exact-main development package orchestration

**Files:**
- Modify: `.github/workflows/agent-release.yml`
- Modify: `tests/test_agent_release_workflow.py`
- Modify: `scripts/verify-supply-chain`
- Modify: `tests/scripts/test_verify_supply_chain.py`

**Interfaces:**
- Consumes: push or manual dispatch at the exact current `main` tip.
- Produces: accepted development package artifact and apt `dev` publication.

- [ ] **Step 1: Write failing exact-main and publication tests**

Require push `branches: [main]`, manual dispatch without a version input, full
history checkout, exact `origin/main` equality before build and again before apt
publication, reusable build with `agent-development`, reusable apt publication
with `apt-development`, immutable full-SHA artifact naming, and no production
environment, stable suite, release, or `contents: write` authority.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q \
  tests/test_agent_release_workflow.py \
  tests/scripts/test_verify_supply_chain.py
```

- [ ] **Step 3: Implement the development orchestrator**

Derive metadata only after exact-main verification, call the package workflow,
then call the apt workflow. Give the metadata job `contents: read`; give called
jobs only the permissions their reusable workflows require. Bind the new
workflow/helper files into the supply-chain manifest.

- [ ] **Step 4: Run and verify GREEN**

Run the Step 2 command and `scripts/verify-supply-chain --generate --json && scripts/verify-supply-chain --json`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/agent-release.yml scripts/verify-supply-chain \
  tests/test_agent_release_workflow.py tests/scripts/test_verify_supply_chain.py \
  inventory/sbom/manifest.json
git commit -S -m "ci(agent): publish accepted main packages"
```

### Task 5: Production integration and operator guide

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `docs/operations/agent-package-release.md`
- Modify: `docs/runbooks/platform-release-publication.md`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `tests/test_agent_release_workflow.py`
- Modify: `tests/test_container_release_workflow.py`

**Interfaces:**
- Consumes: successful trusted tag metadata, accepted package artifact, and completed GitHub Release.
- Produces: production package Release assets and apt `stable`, with documented dev/stable setup.

- [ ] **Step 1: Write failing production/docs tests**

Require the production caller to pass only canonical stable metadata to the
shared build, attach the exact resulting package set to the GitHub Release, and
call the stable apt publisher only after release creation. Require docs to list
all environment secrets/variables, both keyrings/source lines, channel switch,
attestation verification, key rotation, state recovery, and version ordering.

- [ ] **Step 2: Run and verify RED**

```bash
uv run --frozen pytest -q \
  tests/test_agent_release_workflow.py \
  tests/test_container_release_workflow.py
```

- [ ] **Step 3: Complete production wiring and documentation**

Replace the inline stable apt job with the reusable publisher while preserving
release-manifest ordering. Update operator commands with exact URLs,
fingerprint checks, `signed-by` paths, `dev`/`stable` distributions, and explicit
warnings that changing channels never bypasses Debian downgrade protection.

- [ ] **Step 4: Run focused verification and verify GREEN**

Run the Step 2 command plus documentation tests and `git diff --check`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml docs/operations/agent-package-release.md \
  docs/runbooks/platform-release-publication.md README.md docs/README.md \
  tests/test_agent_release_workflow.py tests/test_container_release_workflow.py
git commit -S -m "docs(agent): document package release channels"
```

### Task 6: Full release completion audit

**Files:**
- Modify only files required by failures found in this audit.

**Interfaces:**
- Consumes: complete branch state.
- Produces: evidence that all container and package development/production requirements are implemented.

- [ ] **Step 1: Run all architecture-independent tests**

```bash
uv run --frozen pytest -q
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test --workspace --locked
```

- [ ] **Step 2: Run release/security validation**

```bash
scripts/verify-supply-chain --json
bash -n scripts/promote-image-aliases scripts/dev-image-acceptance
git diff --check
```

Parse all workflows as YAML and inspect every changed `uses:` line for a full
commit SHA. Verify no private-key marker or secret value exists in tracked or
generated package/Compose/image artifacts.

- [ ] **Step 3: Build the real package where architecture permits**

On AArch64, create an ephemeral Ed25519 fixture key under a `0700` temporary
directory, build twice from the exact commit epoch, compare bytes, run
`scripts/verify-agent-deb`, install with `SYSTEMD_OFFLINE=1`, exercise upgrade,
downgrade rejection, removal, and reinstall, then securely remove the fixture.
On non-AArch64, require the reusable ARM64 Actions workflow and package tests as
the authoritative architecture gate and report that limitation explicitly.

- [ ] **Step 4: Audit every requested deliverable**

Record direct evidence for public dev containers, production containers,
development Compose, pinned Compose, production Compose, development `.deb`,
production `.deb`, apt `dev`, apt `stable`, secrets isolation, attestations,
installation documentation, and rollback/recovery behavior.

- [ ] **Step 5: Commit any audit fixes and present integration choices**

```bash
git status --short
git log --show-signature -1
```

Do not merge, push, or create a release without explicit user authorization.
