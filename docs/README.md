# Vonk Forge documentation

Vonk Forge is a private control plane for one NVIDIA DGX Spark or a fleet. The
controller runs on any local computer with Docker Compose—your laptop, a NAS, or
a server—and owns the Web UI, API, PostgreSQL state, identity, policy, and
runtime secrets. Native Spark agents connect outbound; normal Spark operation
does not require routine SSH.

## Choose your path

| I want to… | Start here |
| --- | --- |
| Install a controller and first Spark | [Public installation guide](https://vonkforge.ai/install) |
| Understand what is public and what stays local | [Architecture overview](architecture-overview.md) |
| Understand the engineering stance | [Engineering principles](engineering-principles.md) |
| Understand state ownership and deadlock prevention | [Coordination boundaries](architecture-overview.md#coordination-and-deadlock-prevention) |
| Implement resilient artifact storage | [Storage and coordination plan](plans/resilient-artifact-storage.md) |
| Follow the current fault-resilience review | [Resilience handover and acceptance criteria](operations/resilience-handover.md) |
| Use the complete terminal interface | [`vonkctl` guide](runbooks/vonkctl.md) |
| Use unattended credentials and redeploy NAS Compose | [Operator CLI access](runbooks/operator-cli-access.md) |
| Deploy or upgrade the Docker Compose project | [Controller-host deployment](../deploy/compose/README.md) |
| Configure Tailscale before first install | [Tailscale fresh-install preflight](runbooks/tailscale.md#fresh-install-preflight) |
| Understand identities and trust | [Security threat model](security/threat-model.md) |
| Contribute or verify changes | [Testing and CI](testing-and-ci.md) |
| Keep main current, integrate agent work, and land PRs | [Development workflow](runbooks/development-workflow.md) |
| Understand shared Python, Rust, and API contracts | [Contract ownership and handoffs](api-contracts.md) |

## Authority at a glance

```mermaid
flowchart LR
    Public[Public website and recipe library<br/>documentation, signed artifacts, metadata]
    Control[Local controller<br/>PostgreSQL, policy, identity, secrets]
    Spark[DGX Spark agents<br/>derived execution cache, runtime, telemetry]

    Public -->|verify and import| Control
    Control -->|previewed operations| Spark
```

- Local PostgreSQL owns recipes, installations, placements, runs, profiles, and
  audit state. The trusted Controller/NAS cache owns the exact verified model
  artifact sets and recipe images used by profile choices and apply. It remains
  usable without a hosted catalog or Git remote.
- The target [ownership boundary](architecture-overview.md#state-ownership)
  keeps control intent, permissions, coordination, and audit in PostgreSQL while
  moving physical artifact state and local checkpoints to self-descriptive
  managed storage. The implementation plan records that cutover as implemented
  in the repository; deployed recovery and physical acceptance remain separate.
  Derived indexes are disposable; they do not create another authority.
- Coordination has strict transaction, lock, and resource boundaries: no SQL
  transaction waits for an artifact lock or external work, waiting parents
  release execution slots, and children inherit their parent's node ownership.
- The public recipe library contains immutable metadata and deterministic source
  contexts—not images, weights, credentials, or fleet state.
- Caddy is the private local ingress. Tailscale is the default remote-access
  boundary. Spark agents use enrolled identity and outbound connections.
- Recipe containers and model weights run on Spark-local infrastructure, but
  Spark-local copies are derived execution caches: they do not authorize
  profile choices or pin NAS objects. They are not stored on `vonkforge.ai` or
  Cloudflare Pages.
- Profile choices and apply require complete, cached, exact model and
  recipe-image assets in the Controller/NAS cache. Missing assets block apply
  and offer **Prepare cache**. Once ready, apply distributes the exact assets
  to selected Sparks in parallel, skips verified local copies, safely replaces
  workloads, and reports per-Spark progress and readiness. NAS garbage
  collection removes only unreferenced local model objects.

## Operator guides

- [Fresh development installation](runbooks/fresh-development-install.md)
- [Controller bootstrap](runbooks/control-plane-bootstrap.md)
- [Tailscale ingress and fresh-install preflight](runbooks/tailscale.md)
- [`vonkctl` controller CLI](runbooks/vonkctl.md)
- [PostgreSQL authority administration](runbooks/authority-administration.md)
- [Node onboarding and health](runbooks/node-onboarding.md)
- [Agent installation and enrollment](operations/install-vonk-agent.md)
- [Telemetry and troubleshooting](runbooks/control-plane-telemetry.md)
- [Recipe and model switching](runbooks/model-switching.md)
- [Model and recipe identities](operators/model-catalog.md)
- [Standard recipe library](operators/recipe-library.md)
- [Execution harnesses](operators/execution-harnesses.md)

## Release and platform guides

- [Platform release publication](runbooks/platform-release-publication.md)
- [Agent package release](operations/agent-package-release.md)

Commands in these pages are plan-first: they expose revisions, placement,
resource checks, and affected nodes before mutation. State-changing CLI
operations require `--apply`. Credentials and private keys never belong in Git,
recipes, command arguments, or captured diagnostics.
