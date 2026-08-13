# Vonk Forge

Vonk Forge is a local-first control plane for one or more NVIDIA GB10 GPU systems.
Each GPU node is onboarded independently; the Docker-capable service host runs
separate Caddy, API/worker, PostgreSQL, LiteLLM, Hermes Agent, Prometheus, and
Grafana services.
Administration is available through both `vonkctl admin` and the
web UX. The target recipe workflow is local-first: PostgreSQL is authoritative
for recipe families, authored/imported revisions, installations, placements,
and runs. Git/TUF remains the authority for platform source and the existing
workload-release path while the catalog migration is completed; a recipe never
needs a Git commit or pull request in order to be imported or run.

The initial product has no Railway or global-catalog dependency. This repository
owns the GPU node/NAS runtime and its GitHub Actions platform release: one
stable `vX.Y.Z` tag builds the signed ARM64 `vonk-forge-agent` Debian package,
the API/worker/Hermes control images, and their signed platform manifest. The
same verified package can then be published to the Cloudflare R2 APT repository
at `packages.vonkforge.ai`. Accepted `main` commits separately advance the
signed apt `dev` package channel; production tags attach immutable package
evidence to their GitHub Release before advancing apt `stable`. The
[agent package channel guide](docs/operations/agent-package-release.md) lists
all four protected environments, exact keyring bootstrap commands, and channel
switch/recovery rules. The separate `vonk-forge-web` repository
will later publish a global catalog frontend through Cloudflare Pages and may
add a Railway API/worker/PostgreSQL service; that future service is optional and
never replaces the local catalog authority.

Before a real release, run `scripts/verify-platform-release --candidate X.Y.Z
--json`. A blocked result is expected until external hardware, recovery, and
protected-code-host evidence exists. PR-only repository mutation is a one-way
transition and must not be enabled from simulator evidence.

Vonk Forge is a collection of contracts, controllers, runtime adapters, and
operational tooling for defining, validating, deploying, and operating
model-serving profiles across NVIDIA GB10 systems. The repository keeps
cluster admission and model maturity fail-closed: a checked-in definition is
not treated as production-ready until its evidence gates are accepted.

## Capabilities

- Validate and reconcile the existing content-addressed platform and cluster
  definitions from Git/TUF.
- Author recipes locally, import WorkloadRun profiles with a field-by-field report,
  or download immutable revisions from the optional global catalog. Local
  PostgreSQL remains usable when the global service or Git remote is unavailable.
- Execute routine lifecycle and probe operations through outbound, fenced,
  mutually authenticated GPU node agents; the control worker never SSHes to a
  GPU node.
- Collect durable node, NVIDIA, Docker, thermal, and storage state reported by
  authenticated agents.
- Configure and validate the direct RoCE/NCCL fabric between GPU nodes.
- Build recipe workload containers from digest-bound Dockerfiles on a compatible
  GPU node, transfer the exact OCI result to mapped nodes, and run them without
  requiring a community container registry.
- Build and operate model-specific runtime adapters, including the checked-in
  DeepSeek Mia and DS4 definitions.
- Publish and operate generic, signed workload packages independently from
  Vonk Forge platform releases; ordinary model/runtime releases do not require
  an agent update.
- Review and apply NAS-to-GPU node platform skew updates through the Admin web UX
  or `vonkctl`, with explicit signed fan-out over the outbound agent channel.

## Prerequisites

- Python 3.12 or newer
- [`uv`](https://docs.astral.sh/uv/)
- SSH access for one-time onboarding and explicit operator recovery only
- NVIDIA's DGX OS Docker Engine, NVIDIA driver, and NVIDIA Container Toolkit on
  each DGX Spark GPU node. These platform-owned components are preconfigured by
  NVIDIA and are validated, not installed or reconfigured, by Vonk Forge.
- Rootless Podman, `fuse-overlayfs`, and `slirp4netns` on each GPU node
  (installed by `vonk-forge-agent`) for isolated recipe builds only. Accepted
  GPU workloads run through the Spark-managed Docker/NVIDIA runtime behind the
  signed host-helper boundary.

## Quick start

Install the locked development environment and run the local test suite:

```bash
uv sync --dev
uv run pytest
```

For a first installation, follow the concise
[fresh development installation](docs/runbooks/fresh-development-install.md).
It covers the NAS, private browser login, signed agent package, pairing, and
first synthetic workload in one order. Normal administration opens the stable
private Tailscale HTTPS `svc:vonk-forge` Service in a browser without an SSH or
PowerShell tunnel. The exact URL and login steps are in
[Open the stable browser URL](docs/runbooks/development-nas-installation.md#open-the-stable-browser-url).
For a NAS, create one NAS-local project containing only
`docker-compose.yml` and `secrets/`. Download and retain
`docker-compose.dev.yml` locally; the project publisher installs it as
`docker-compose.yml`, which remains unchanged during normal development.
Accepted `main` advances the mutable
`:dev` channel; operators update development by pulling/redeploying the
unchanged project file. Keep `docker-compose.pinned.yml` only for explicit
reproduction or guarded recovery. Signed releases separately publish the full
`docker-compose.production.yml`. The NAS pulls public images and never
receives source, Dockerfiles, build contexts, or image archives. Follow the
[development NAS installation guide](docs/runbooks/development-nas-installation.md)
for the complete 17-file deployed runtime bundle and generic Compose-project
import.

Repository contributors with the two `dev-local` images already built can run
the same image-only stack locally:

```bash
scripts/dev-compose
curl --fail http://127.0.0.1:8080/api/v1/readyz
scripts/dev-compose down
```

This local command never publishes images or deploys to production. Accepted
`main` commits publish immutable `dev-sha-*` tags and the mutable `:dev`
convenience alias used only for operator-pulled/redeployed development
projects. Signed release tags promote the exact accepted digests to immutable
`vX.Y.Z` tags. `latest` is informational only; production selection remains
authoritative only through the trusted host-updater and TUF-reviewed platform
target. Generated Compose and release artifacts always use immutable
tag-plus-digest references, never a moving alias. Production uses the reviewed
digest-pinned platform Compose path in
[`deploy/compose/README.md`](deploy/compose/README.md).

The repository deliberately keeps expensive acceptance work local. Pull
requests run only the focused contract smoke checks and generated-client drift
check in GitHub Actions. Before requesting review, run the full local tiers
that match the change:

```bash
uv run --frozen pytest -q
uv run --project control --frozen --with-editable . pytest -q control/tests
npm ci --prefix control/web && npm test --prefix control/web -- --run
uv run --frozen pytest -q deploy/compose/tests
scripts/verify-supply-chain --json
```

The simulated workload acceptance and failure matrix run on manual/tagged
workflow executions so release evidence is still produced without charging
every PR for the longest jobs.

The protected `Main` ruleset requires the three PR checks (`Ruff`, `Generated
control clients`, and `PR contract smoke`). A successful merged PR lifecycle is
recorded in `inventory/reports/code-host-protection.json`; heavyweight
acceptance remains outside the PR path by design.

See [Testing and CI policy](docs/testing-and-ci.md) for the exact local tiers,
the hosted smoke subset, and the release-only acceptance gates.

Configure the authenticated control origin and restrictive token file, then
inspect current node state and preview the exact server reconciliation plan:

```bash
export VONK_CONTROL_URL=https://control.example.invalid
export VONK_CONTROL_TOKEN_FILE=/run/secrets/vonk-control-token
uv run --project /path/to/vonk-forge vonkctl nodes status --json
uv run --project /path/to/vonk-forge vonkctl validate PROFILE --json
uv run --project /path/to/vonk-forge vonkctl switch PROFILE --json
```

`prepare`, `switch`, and `restore-default` are plan-only unless `--apply` is
present. Applied commands wait for the accepted job by default; use `--no-wait`
to return its job ID for later API polling. Routine commands fail with
`error_type=control_api` when the control plane is unavailable and never fall
back to SSH.

The old local controller remains available only as an explicitly named
migration/recovery compatibility tool:

```bash
uv run --no-project --with jsonschema -- bin/vonkctl-legacy status --json
```

Never use or configure `vonkctl-legacy` as a production command. It is never
selected implicitly. Routine production work is repository-planned by the API,
persisted in PostgreSQL, claimed outbound by each GPU node agent over mTLS, and
reconciled by the repository-less worker.

## Repository layout

- `bin/` — repository-local command launchers
- `src/cluster_profiles/` — profile catalog, admission, state, health, and CLI
- `adapters/` — model-specific runtime definitions and lifecycle tooling
- `config/` — controller, workload, and cluster-profile configuration
- `nodes/` — node bootstrap, health, fabric, and recovery utilities
- `schemas/` — JSON contracts for profiles, workloads, and health evidence
- `tests/` — Python and shell test suites
- `docs/` — architecture, security, testing, and operator runbooks

## Documentation

- [Documentation index](docs/README.md)
- [Fresh development installation](docs/runbooks/fresh-development-install.md)
- [Architecture overview](docs/architecture-overview.md)
- [Recipe catalog and WorkloadRun operations](docs/runbooks/workload-packages.md)
- [NAS pull-only Compose deployment](deploy/compose/README.md)
- [Development NAS installation and runtime secrets](docs/runbooks/development-nas-installation.md)
- [Source-first recipe containers and local builds](deploy/compose/README.md#recipe-containers-are-source-first)
- [Agent development/stable package release and APT installation](docs/operations/agent-package-release.md)
- [Control-plane bootstrap](docs/runbooks/control-plane-bootstrap.md)
- [`vonkctl` runbook](docs/runbooks/vonkctl.md)
- [Inventory runbook](docs/runbooks/inventory.md)
- [Generic fleet migration](docs/runbooks/fleet-migration.md) — generated node
  identities and compatibility with the current inventory, with no fixed node
  count
- [Direct-fabric runbook](docs/runbooks/fabric.md)
- [Runtime release runbook](docs/runbooks/runtime-release.md)
- [GPU node agent PKI and recovery runbook](docs/runbooks/agent-pki.md)
- [Tailnet-only NAS ingress runbook](docs/runbooks/tailscale.md)
- [Hermes Agent runbook](docs/runbooks/hermes-agent.md)
- [Workload package operations](docs/runbooks/workload-packages.md) — generic
  family/release publication, rollout, rollback, repair, GC, and first-release
  evidence
- [Platform update runbook](docs/runbooks/platform-update.md) — NAS/GPU node
  platform skew and recovery boundaries

## Security

Do not commit credentials. Keep private keys, tokens, and passwords out of
profile files, command arguments, and captured diagnostics. Membership in the
Docker group is root-equivalent and should be limited to trusted operators.

## License

Vonk Forge is available under the [MIT License](LICENSE).
