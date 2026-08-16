# Vonk Forge operator documentation

Vonk Forge runs a Docker-capable control host (normally a NAS) and one or more
Vonk Forge GPU node agents. Caddy is the only published ingress; Tailscale is the
default remote-access boundary; GPU node agents make outbound mTLS connections;
LiteLLM publishes only routes acknowledged by the control plane.
Normal administration uses the stable private Tailscale HTTPS
`svc:vonk-forge` Service in a browser without an SSH or PowerShell tunnel. See
[Open the stable browser URL](runbooks/development-nas-installation.md#open-the-stable-browser-url).

## Authority boundary

- Local PostgreSQL owns recipe families, authored and imported revisions,
  WorkloadRun import reports, installations, placements, and runs. It remains
  usable without the global catalog or a Git remote.
- The optional global Vonk Forge Web service publishes immutable recipe
  revisions and metadata. It never stores image layers, model weights, or
  cluster state.
- Git/TUF remains the authority for platform source, fleet/topology policy,
  and the existing platform/workload release projection until that projection
  is migrated to catalog revisions.

## Deployment boundary

- The NAS/Docker service host runs the local control API, repository-less worker,
  PostgreSQL, Caddy, LiteLLM, and observability services. Optional Hermes is a
  default-disabled Compose profile. Caddy is the
  local ingress boundary; it is not the global website host.
- One stable Git tag drives the GitHub Actions platform release: it builds,
  tests, signs, and publishes the API/worker/Hermes images and the matching
  `vonk-forge-agent` ARM64 Debian package. The verified package can then be
  published to the public Cloudflare R2 APT repository at
  `packages.vonkforge.ai`.
- Accepted `main` commits publish authenticated development packages to apt
  `dev`; trusted stable tags attach the exact accepted package evidence to the
  GitHub Release before apt `stable` advances. Package and apt signing
  authority remain separate from NAS runtime secrets.
- The initial local product does not require Railway or a global catalog. The
  future `vonk-forge-web` frontend belongs on Cloudflare Pages; its global
  API/validation worker/PostgreSQL backend may later run on Railway.
- Recipe containers and model weights are installed and run on the NAS/GPU nodes,
  never on Railway or Cloudflare Pages.

## Start here

- [Fresh development installation](runbooks/fresh-development-install.md)
- [Architecture overview](architecture-overview.md)
- [Source-first local Compose deployment](../deploy/compose/README.md)
- [Development NAS installation and runtime secrets](runbooks/development-nas-installation.md)
- [Agent Debian package `dev`/`stable` release, secrets, and APT installation](operations/agent-package-release.md)
- [Testing and CI](testing-and-ci.md)
- [Identity verification policy](identity-verifier.md)
- [Control-plane bootstrap](runbooks/control-plane-bootstrap.md)
- [Control-plane operations](runbooks/control-plane-operations.md) — Fleet,
  Library, recipe placement, resource previews, and safe action semantics
- [Model catalog](operators/model-catalog.md) — model identity, recipes,
  topology, install/update, and exact-revision rollback
- [Model target ledger](operators/model-targets.md) — current defaults,
  candidates, blocked upstreams, and the path from research to an accepted
  recipe
- [Standard recipe library](operators/recipe-library.md) — the public recipe
  repository, authority split, immutable imports, and validation commands
- [Execution harness operations](operators/execution-harnesses.md) — built-in
  harnesses, interface publication, clean reset, and canonical acceptance
- [Control-plane telemetry](runbooks/control-plane-telemetry.md) — metrics,
  freshness, resolutions, retention, and troubleshooting
- [Node onboarding and health](runbooks/node-onboarding.md)
- [Recipe and workload operations](runbooks/workload-packages.md)
- [Global catalog import and publication](runbooks/global-catalog.md)
- [Vonk Forge agent installation](operations/install-vonk-agent.md)
- [Accepted development system evidence](audits/2026-08-15-development-system-acceptance.md)
- [DGX Spark platform-alignment audit](audits/2026-08-12-dgx-spark-platform-alignment.md)
- [Model switching](runbooks/model-switching.md)
- [Platform updates](runbooks/platform-update.md)
- [Security threat model](security/threat-model.md)

Commands in these pages are plan-first by default. They show the exact
revision, placement, resource checks, and affected nodes before mutation;
`--apply` is required for a state-changing operation. Secrets and private
keys never belong in recipes, Git, command arguments, or captured diagnostics.
