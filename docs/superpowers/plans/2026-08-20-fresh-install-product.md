# Fresh-Install Vonk Forge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` or `superpowers:executing-plans` to
> implement this plan task by task. Every implementation task starts with a
> failing behavioral test and ends with focused verification.

**Goal:** Deliver one canonical fresh-only Vonk Forge runtime with one-curl NAS
preparation and one-curl direct Rust Spark installation/upgrading.

**Architecture:** PostgreSQL owns runtime authority, Step CA owns PKI, one
Compose graph serves both channels, and one signed release manifest drives both
installers. All legacy and alternate runtime paths are deleted.

**Spec:** `docs/superpowers/specs/2026-08-20-fresh-install-product-design.md`

## Product definition of done

The entire program is complete only when a new operator can deploy Vonk Forge
without a checkout, archive, helper command, or hand-edited configuration:

| Target | Command location | Only installation command | Successful result |
|---|---|---|---|
| NAS bundle | Ordinary Linux/macOS workstation | `curl -fsSL https://install.vonkforge.ai/nas | sh` | `./vonk-forge` contains only `docker-compose.yaml`, `.env`, and `secrets/`, ready to drag onto the NAS and start in its Docker UI. |
| Spark | The Spark itself | `curl -fsSL https://install.vonkforge.ai/spark | sh` | The direct Rust agent is installed or upgraded, paired when necessary, running, and verified. |

The development channel has the exact same contract at `/dev/nas` and
`/dev/spark`. The channel may select different immutable versions, but it may
not change prompts, topology, defaults, paths, security, or lifecycle behavior.

The curl bootstraps accept no required arguments or environment variables. They
may prompt through `/dev/tty`; they must download and verify their complete
payload, perform all work for that target, and either produce the successful
result above or exit with a precise, actionable error. Documentation may
explain how to use the resulting NAS directory or administer a running system,
but it must never introduce another installation path.

## End-to-end execution order

1. Establish one fresh-only runtime model: PostgreSQL authority, Step CA PKI,
   one production-shaped Compose topology, and Hermes as the sole option.
2. Remove every alternate or legacy path: runtime Git, Python Spark, built-in
   CA, A/B supervisor, controller-driven updater, migrations, helper services,
   overlays, and obsolete installer documentation.
3. Make the NAS setup executable generate the exact upload directory safely on
   Linux and macOS, including every required secret and site-local choice.
4. Make the Spark setup executable install or upgrade the verified direct Rust
   package, pair securely, and prove the running identity to the controller.
5. Build all images, packages, and setup executables once in required CI; test
   those exact artifacts with real PostgreSQL and clean Docker Compose.
6. Publish NAS and Spark together through one signed, expiring, atomic channel
   manifest; stable promotion reuses accepted artifacts and forbids rollback.
7. Exercise the literal development curl commands from clean hosts, deploy the
   generated NAS directory, run a real Spark lifecycle, and repeat the curls to
   prove upgrades and interruption recovery.
8. Review the whole repository, merge only with all required checks green,
   publish stable, exercise both literal stable curls, and generate `/mnt/z`
   exclusively from the published NAS command.

No phase is accepted solely because unit tests pass. The final gate is the
published-command experience on clean, production-shaped systems.

## Program invariants

- Development and production differ only in immutable artifact identities.
- Each side has exactly one stable, no-argument entry point:
  `curl -fsSL https://install.vonkforge.ai/nas | sh` and
  `curl -fsSL https://install.vonkforge.ai/spark | sh`.
- Development exercises the identical no-argument entry points at
  `curl -fsSL https://install.vonkforge.ai/dev/nas | sh` and
  `curl -fsSL https://install.vonkforge.ai/dev/spark | sh`; only resolved
  immutable artifact identities differ.
- Each curl command resolves and verifies every artifact it needs, performs the
  complete workflow, and exits with either a usable result or a specific error;
  it never prints a required follow-up setup command.
- Re-running the same curl command is the supported upgrade path. There is no
  separate upgrade command or installer download step.
- The NAS output contains `docker-compose.yaml`, `.env`, and `secrets/` only.
- NAS preparation requires no Docker, root, Git, SSH, or NAS access.
- Spark installation invokes privilege only after artifact verification.
- No runtime Git, Python Spark agent, built-in CA, A/B supervisor, one-shot
  service, sleeping bootstrap service, mutable image, or migration compatibility
  remains.
- Required CI acceptance uses real PostgreSQL and a real clean Compose rollout.
- `/mnt/z/vonk-forge` is generated from a published artifact, never hand-edited.

---

### Task 1: Lock the canonical fresh-runtime contract

**Files:**
- Create: `deploy/compose/tests/test_fresh_runtime_contract.py`
- Modify: `deploy/compose/tests/test_production_shaped_development.py`
- Modify: `tests/scripts/test_render_production_compose.py`

- [ ] Add model-level tests for the exact default and Hermes service sets.
- [ ] Assert one canonical source graph, Step CA always present, and Hermes the
      only profile.
- [ ] Assert every long-running service has a healthcheck and no service has
      `restart: "no"`, a sleep-only command, or a completed-success dependency.
- [ ] Assert all paths are relative and all images are digest pinned.
- [ ] Assert dev and production renders differ only in image identities.
- [ ] Run the focused tests and retain the expected failures.

### Task 2: Collapse Compose to one topology

**Files:**
- Modify: `deploy/compose/compose.yaml`
- Delete: `deploy/compose/compose.step-ca.yaml`
- Modify: `deploy/compose/compose.dev.images.yaml`
- Modify: `scripts/dev-compose`
- Modify: `scripts/render-dev-compose`
- Modify: `scripts/render-production-compose`
- Modify: `deploy/compose/.env.example`

- [ ] Fold Step CA into the canonical graph and remove CA overlay selection.
- [ ] Keep Hermes as the only optional profile.
- [ ] Make every renderer and launcher consume the same canonical model.
- [ ] Remove absolute NAS paths and any environment-specific topology inputs.
- [ ] Make image inputs exact immutable manifest values.
- [ ] Run canonical model tests and Compose configuration validation.

### Task 3: Remove bootstrap containers and initialize owned state correctly

**Files:**
- Create: `deploy/compose/postgres/init-databases.sh`
- Modify: `control/src/vonk_control/api.py`
- Modify: `control/src/vonk_control/database.py`
- Modify: `control/src/vonk_control/database_authority.py`
- Delete: `control/src/vonk_control/compose_bootstrap.py`
- Modify: API and PostgreSQL image entrypoints
- Modify: `deploy/compose/compose.yaml`
- Test: real PostgreSQL startup and concurrent initialization tests

- [ ] Prove a fresh cluster creates distinct control and LiteLLM roles/databases.
- [ ] Move schema and authority initialization into API startup under a
      PostgreSQL advisory lock.
- [ ] Prove concurrent API starts cannot create partial authority state.
- [ ] Remove the bootstrap service and all completed-success dependencies.
- [ ] Ensure each service prepares only its own state and then execs the
      long-running process.

### Task 4: Complete Step CA, controller TLS, and tailnet access

**Files:**
- Modify: `deploy/compose/Caddyfile`
- Modify: `deploy/compose/compose.yaml`
- Modify: Step CA and Tailscale configuration assets/images
- Test: `deploy/compose/tests/test_agent_ingress.py`
- Test: new clean PKI/TLS integration suite

- [ ] Remove the built-in CA and all flat credential fallbacks.
- [ ] Bind Caddy explicitly to the generated controller certificate/key.
- [ ] Give Caddy an unconditional loopback readiness endpoint.
- [ ] Terminate `.ts.net` browser TLS through Tailscale and proxy internally.
- [ ] Verify browser, enrollment, agent mTLS, and registry SNI independently.
- [ ] Verify Step CA restart persistence and certificate renewal.

### Task 5: Add complete service readiness

**Files:**
- Modify: control worker health surface and Compose healthcheck
- Modify: Prometheus, Grafana, registry, LiteLLM, Tailscale reconciler, Caddy,
  Step CA, API, worker, and PostgreSQL healthchecks
- Create: `deploy/compose/tests/test_runtime_health_contract.py`

- [ ] Define meaningful readiness for every default long-running service.
- [ ] Change dependencies to `service_healthy` where startup order matters.
- [ ] Prove `docker compose up -d --wait` reaches all healthy with no exited
      service or warning on an empty project.
- [ ] Prove a full Compose restart returns to healthy.

### Task 6: Remove all legacy runtime code

**Files:**
- Delete: obsolete Python enrollment/registration implementations
- Delete: built-in CA settings and code
- Delete: residual Git authority/runtime code and tests
- Delete: migration-only scripts and documentation
- Modify: API, settings, models, schemas, generated clients, and tests

- [ ] Inventory legacy terms and classify each occurrence as active, historical
      design documentation, vendored dependency, or removable residue.
- [ ] Add negative source/package/runtime tests for removed components.
- [ ] Delete active compatibility paths and regenerate clients.
- [ ] Keep Git history but remove obsolete tracked source and instructions.

### Task 7: Replace the A/B agent with direct Rust packaging

**Files:**
- Delete: `rust/crates/vonk-agent-supervisor/`
- Modify: Rust workspace and agent protocol models
- Delete: supervisor systemd unit and slot-management scripts
- Modify: `packaging/debian/preinst`
- Modify: `packaging/debian/postinst`
- Modify: `packaging/systemd/vonk-forge-agent.service`
- Modify: controller runtime-identity claim logic and tests

- [ ] Define direct identity fields: semantic version, build digest, binary
      digest, architecture, and self-test result.
- [ ] Remove slots, generations, activation, rollback, supervisor commands, and
      protocol fields end to end.
- [ ] Remove controller-driven agent update/rollback operations, runtime signer,
      and agent-update TUF publication; package releases remain GitHub-owned.
- [ ] Package one binary at `/usr/lib/vonk-forge/vonk-agent` with a direct
      systemd `ExecStart`.
- [ ] Make apt/dpkg replacement restart and verify the direct service.
- [ ] Prove an interrupted install is recoverable using package-manager state,
      not an application-level fallback binary.

### Task 8: Make pairing a complete secure workflow

**Files:**
- Modify: Rust agent CLI and configuration writer
- Modify: controller enrollment API
- Modify: `vonkctl` agent administration commands
- Test: Rust CLI, API, and deployed mTLS integration suites

- [ ] Make `pair` the sole fresh enrollment command.
- [ ] Read tokens from `/dev/tty` or stdin; never accept secrets on argv.
- [ ] Make grant creation the explicit administrator authorization; remove the
      redundant approve/reject ceremony.
- [ ] Publish bounded controller trust bootstrap metadata and make pairing
      atomically install the immediately issued identity.
- [ ] Remove `bootstrap`, flat credentials, and duplicate enrollment services.

### Task 9: Build the versioned NAS preparation artifact

**Files:**
- Create: setup executable/package for supported workstation OS/architectures
- Create: `scripts/build-nas-compose-bundle`
- Create: `tests/scripts/test_build_nas_compose_bundle.py`
- Modify: release workflows and artifact metadata

- [ ] Build a secret-free canonical template containing no runtime subfolders.
- [ ] Make the public curl command require no arguments: the bootstrap supplies
      only its verified temporary payload path to the executable, uses the
      current directory as the output parent, and infers fresh install versus
      upgrade from `./vonk-forge`.
- [ ] Generate/import every required secret and coherent Step CA/controller PKI.
- [ ] Prompt through `/dev/tty`, hide secret input, and reject symlinks or unsafe
      existing files.
- [ ] Produce exactly `docker-compose.yaml`, `.env`, and `secrets/`.
- [ ] Preserve site-local values when the same command detects and upgrades an
      existing local bundle.
- [ ] Reconcile newly required release inputs during upgrade without replacing
      existing site identity or secret values.
- [ ] Prove operation without Docker, sudo, Git, SSH, or network access after
      artifacts have been downloaded.

### Task 10: Publish the NAS curl endpoint

**Files:**
- Create: tiny `install/nas` bootstrap
- Modify: GitHub Pages/release publication workflow
- Test: bootstrap shell tests and end-to-end artifact verification

- [ ] With no arguments or environment setup, resolve the current accepted
      stable release and supported workstation platform.
- [ ] Download and verify the signed manifest and setup executable.
- [ ] Execute as the caller and preserve interactive `/dev/tty` input.
- [ ] Complete by creating or upgrading `./vonk-forge`; do not require a second
      command, manually supplied payload, checksum file, or extracted archive.
- [ ] Fail closed on unsupported systems, stale manifests, digest mismatches, or
      missing signatures.
- [ ] Publish at `https://install.vonkforge.ai/nas`.
- [ ] Publish the identical development flow at
      `https://install.vonkforge.ai/dev/nas`.

### Task 11: Publish the Spark curl endpoint

**Files:**
- Create: tiny `install/spark` bootstrap
- Modify: Debian repository/release workflows
- Test: amd64 and arm64 package installation/upgrade tests

- [ ] With no arguments or environment setup, resolve the current accepted
      stable release and verify its immutable package before privilege
      escalation.
- [ ] Invoke sudo only for package/repository installation and service control.
- [ ] Pair on first install and verify controller-observed identity.
- [ ] Upgrade directly on subsequent runs while preserving identity.
- [ ] Complete first install or upgrade, pairing when needed, service start, and
      local/controller verification in this one invocation; print no required
      follow-up command.
- [ ] Publish at `https://install.vonkforge.ai/spark`.
- [ ] Publish the identical development flow at
      `https://install.vonkforge.ai/dev/spark`.

### Task 12: Make release publication atomic and required

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: development image, agent package, platform release, and Pages workflows
- Modify: branch ruleset configuration
- Create: release-manifest schema, assembler, and verifier

- [ ] Replace source-text/documentation assertions with behavioral checks.
- [ ] Eliminate placeholder artifact identities and mutable Compose outputs.
- [ ] Assemble manifests only from accepted publication receipts.
- [ ] Promote stable artifacts without rebuilding.
- [ ] Advance channel pointers atomically after full verification.
- [ ] Require one aggregate job that fails if any required suite fails or skips.

### Task 13: Run full clean-install acceptance

**Files:**
- Create: `tests/acceptance/test_fresh_nas_install.py`
- Create: `tests/acceptance/test_spark_lifecycle.py`
- Create: CI and canary environment definitions

- [ ] Invoke each literal no-argument curl command in a clean shell and reject
      any flow that needs a second setup command or unpublished input.
- [ ] Generate the NAS directory from the published curl path in an ordinary
      non-root workstation environment without Docker, Git, SSH, or NAS access.
- [ ] Start an empty Docker 29.4.3 / Compose 5.1.3 project and require every
      service healthy with no warnings or exited containers.
- [ ] Verify PostgreSQL, LiteLLM, controller TLS, Tailscale browser URL,
      observability, registry, and optional Hermes.
- [ ] Install and pair real amd64 and arm64 Rust packages.
- [ ] Execute a job, renew identity, publish a newer package, and upgrade.
- [ ] Repeat from a second clean directory and compare all non-secret outputs.

### Task 14: Publish and replace the test deployment

**Files:**
- Generated: `/mnt/z/vonk-forge/docker-compose.yaml`
- Generated: `/mnt/z/vonk-forge/.env`
- Generated: `/mnt/z/vonk-forge/secrets/`

- [ ] Merge only after complete behavioral review and required green checks.
- [ ] Publish immutable development artifacts and the signed manifest.
- [ ] Run the NAS curl preparer against `/mnt/z`, preserving only deliberate
      site-local credentials.
- [ ] Delete every old hand-maintained file outside the three-item contract.
- [ ] Perform the destructive clean NAS rollout authorized for this unshipped
      deployment and validate the full Spark lifecycle.
- [ ] Publish the final two commands and concise recovery/upgrade guidance.
