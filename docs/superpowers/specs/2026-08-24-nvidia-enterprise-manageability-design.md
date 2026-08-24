# Optional NVIDIA DGX Spark Enterprise Manageability

## Status

Proposed design for issue #44.

## Decision

Vonk Forge will treat NVIDIA's DGX Spark Enterprise Lifecycle Integration
scripts as an optional, versioned platform-evidence provider. The existing Vonk
agent, outbound mTLS channel, workload authority, and native probes remain in
place.

The ownership boundary is:

> NVIDIA manages the machine. Vonk manages the AI running on it.

NVIDIA tooling may provide authoritative read-only evidence about device
identity, hardware, firmware, DGX OS, drivers, diagnostics, and reset history.
Vonk remains authoritative for recipes, model and runtime lifecycle, workload
placement, multi-node topology, RoCE and NCCL validation, reconciliation,
endpoint health, LiteLLM route publication, and workload rollback.

Availability is resolved per capability. There is no global
`nvidia_enterprise_manageability=true` switch that grants authority to every
vendor operation.

## Vendor Ground Truth

NVIDIA's Enterprise Manageability documentation links a downloadable
Enterprise Lifecycle Integration Scripts package. The package is reference
material for adaptation into an enterprise management platform; it is not
preinstalled on DGX Spark and it is not a resident NVIDIA control-plane
service.

The reviewed package is:

- source:
  `enterprise-lifecycle-integration-scripts-20260520-1602.zip`;
- published from NVIDIA's `docscontent.nvidia.com` documentation host;
- SHA-256:
  `0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3`;
- license: MIT, copyright NVIDIA Corporation and affiliates;
- target platform: Ubuntu 22.04 or 24.04 on ARM64;
- read-only entrypoints under the archive's `bin/` directory:
  `device_identity.py`, `hardware_config.py`, `firmware_reporter.py`,
  `os_build_identity.py`, `driver_inventory_reporter.py`,
  `software_inventory_reporter.py`, `spark_diagctl.py`, and
  `reset_reason_reporter.py`.

The upstream component installers primarily copy scripts into the unpacked
project's `bin/` directory; some offer an optional `/usr/local/bin/*.py` copy.
They do not define one stable system-wide installation contract. Vonk must not
mistake an arbitrary same-named executable on `PATH` for the reviewed package.

The package does not expose one reliable suite-wide semantic version. The
reviewed tools report individual versions: the inventory reporters are
primarily `1.0.0`, while `device_identity`, `spark_diagctl`, and
`reset_reason_reporter` report `1.1.0`.
Evidence must therefore identify both the pinned bundle and the invoked tool
version. A synthetic provider version must not hide this distinction.

All Python tools return a bounded JSON envelope on stdout:

```json
{
  "ok": true,
  "data": {},
  "errors": [],
  "meta": {
    "tool": "hardware_config",
    "version": "1.0.0",
    "collected_at_utc": "2026-08-24T10:00:00Z"
  }
}
```

The first implementation consumes only this structured interface. It does not
parse human-readable output and does not use NVIDIA's agentless-SSH integration
pattern.

## Goals

- Put existing native platform probes behind a provider boundary.
- Discover each installed and supported NVIDIA tool independently.
- Normalize supported read-only NVIDIA evidence into Vonk-owned schemas.
- Preserve bundle, tool, version, collection time, and provider provenance.
- Retain safe `vonk-native` fallback when NVIDIA tooling is absent or fails.
- Keep provider health independent from workload and inference health.
- Fail closed for unknown tool versions and malformed provider output.
- Keep all invocation local to the existing unprivileged Spark agent.

## Non-goals

- Replacing the Vonk Spark agent or its outbound mTLS control lane.
- Introducing routine inbound SSH or a second control plane.
- Exposing a generic shell or arbitrary NVIDIA command operation.
- Delegating recipes, models, runtimes, placement, fabric, or routes to NVIDIA.
- Installing or updating DGX OS, firmware, drivers, or system packages in the
  first implementation.
- Making the optional package a fresh-install prerequisite.
- Coupling recipes to the `nvidia-enterprise` provider when a capability is
  sufficient.
- Treating NVIDIA provider failure as an inference-route failure.

## Current Baseline

The Rust agent currently collects memory and disk state directly and invokes
fixed `nvidia-smi`, `nvidia-ctk`, Docker, and Podman paths through the bounded
`ProcessRunner`. The initial provider abstraction should preserve this wire
contract and behavior before adding new fields.

Node identity remains certificate-bound controller state. Neither NVIDIA's
device identity output nor a change in provider selection may create or rename
a Vonk node.

## Capability Model

Each capability has an independently resolved provider and state:

```text
available | degraded | unavailable | unsupported | unknown
```

The initial namespace and precedence are:

| Capability | Preferred provider | Safe fallback | First phase |
|---|---|---|---|
| `platform.identity` | `nvidia-enterprise` | `vonk-native` | read-only |
| `platform.inventory` | `nvidia-enterprise` | `vonk-native` | read-only |
| `platform.health` | `nvidia-enterprise` | `vonk-native` | read-only |
| `platform.firmware` | `nvidia-enterprise` | none | read-only |
| `platform.os` | `nvidia-enterprise` | `vonk-native` | read-only |
| `platform.diagnostics` | `nvidia-enterprise` | none | metadata only |
| `platform.reset_history` | `nvidia-enterprise` | none | read-only |
| `hardware.gpu` | `nvidia-enterprise` | `vonk-native` | read-only |
| `hardware.storage` | `nvidia-enterprise` | `vonk-native` | read-only |
| `hardware.network` | `nvidia-enterprise` | `vonk-native` | read-only |
| `hardware.thermal` | `nvidia-enterprise` | `vonk-native` | read-only |
| `fabric.roce` | `vonk-native` | none | unchanged |
| `fabric.nccl` | `vonk-native` | none | unchanged |
| `workload.*` | `vonk` | none | unchanged |
| `inference.*` | `vonk` | none | unchanged |

`platform.update` is deliberately absent from the initial provider set. It can
be introduced only with a separate maintenance design covering drain, idle
proof, reboot, reconnect, verification, and workload restoration.

Recipes consume capability names, not provider names. A provider constraint is
valid only when the provider itself is required by the operation.

## Discovery and Version Policy

Discovery runs locally at startup and periodically with jitter. It evaluates
each tool separately:

```text
fixed Vonk-managed path exists
    -> file is a regular root-owned, non-group-writable executable
    -> tool starts with a fixed read-only argument vector
    -> JSON envelope is bounded and valid
    -> tool name and version are allowlisted
    -> capability is available
```

Presence alone is not capability evidence. A missing executable is
`unavailable`; an unknown version is `unsupported`; timeout, non-zero exit,
malformed JSON, or failed self-report is `degraded` when previously usable and
`unavailable` otherwise.

The supported-version policy is explicit per tool. Unknown major versions fail
closed. Minor and patch compatibility is widened only after fixture and Spark
canary validation.

The bundle itself is never downloaded at agent runtime. A later optional
integration package may consume the reviewed ZIP as a digest-bound build input
and install only selected read-only tools under
`/usr/lib/vonk-forge/nvidia-enterprise/bin/`. Package installation verifies the
bundle digest and records a manifest containing the source bundle and per-file
digests. Operators who unpack NVIDIA's ZIP elsewhere must explicitly install
this integration package; the agent does not discover arbitrary paths.

## Agent Adapter Boundary

The Rust implementation extends the existing fixed `Program` enum with one
variant per approved NVIDIA script. Because upstream uses
`#!/usr/bin/env python3`, each variant instead executes `/usr/bin/python3` with
its Vonk-managed script path as an immutable first argument. The process runner
must prepend that fixed argument before operation-specific arguments. There is
no caller-provided executable or script path and no `PATH` lookup. Each
operation supplies a compile-time argument vector, timeout, stdout limit, and
stderr limit. No shell is involved.

Initial invocations are restricted to read-only modes such as:

- `device_identity --print`;
- `hardware_config --print`;
- `firmware_reporter --print --no_vendor_tools` until vendor-tool privileges
  are separately reviewed;
- `os_build_identity --print` without all-package expansion;
- `driver_inventory_reporter --print` with bounded raw fields;
- `spark_diagctl status`, `spark_diagctl health`, and `spark_diagctl gpu`;
- `reset_reason_reporter --print`.

The adapter validates:

- UTF-8 JSON with no trailing non-whitespace content;
- an object envelope with exact top-level `ok`, `data`, `errors`, and `meta`
  types;
- expected `meta.tool` and allowlisted `meta.version`;
- a parseable collection timestamp within a bounded clock-skew window;
- bounded array lengths, strings, object depth, and total encoded size;
- capability-specific required fields and value ranges;
- redaction of serials, addresses, paths, package data, and logs not present in
  the normalized Vonk schema.

Raw output is not sent in heartbeats. An explicitly requested audit artifact
may retain redacted raw evidence under a separate size limit and retention
policy.

## Normalized Evidence Contract

Provider state is separate from normalized inventory values:

```json
{
  "name": "platform.inventory",
  "provider": "nvidia-enterprise",
  "state": "available",
  "bundle": {
    "name": "enterprise-lifecycle-integration-scripts-20260520-1602",
    "sha256": "0eb1c93dd839b6bd4136cc8b79ea04a1e44fd637ff6afa6ee9568951a4c179f3"
  },
  "tool": {
    "name": "hardware_config",
    "version": "1.0.0"
  },
  "schema_version": 1,
  "collected_at": "2026-08-24T10:00:00Z",
  "verified_at": "2026-08-24T10:00:01Z"
}
```

Every normalized value carries a source reference to this capability evidence.
Provider transitions update provenance without changing node identity,
installed workload records, or endpoint ownership.

The agent continues reporting its existing inventory shape during the provider
abstraction slice. A versioned additive contract introduces provider evidence;
old controllers ignore it and new controllers accept native-only agents.

## Control-plane Persistence and API

The controller stores the latest state independently from append-only provider
events:

```text
node_capabilities
  node_id
  capability
  provider
  state
  bundle_name
  bundle_sha256
  tool_name
  tool_version
  schema_version
  first_seen_at
  last_verified_at
  last_changed_at
  details_json

node_capability_events
  event_id
  node_id
  capability
  previous_state
  next_state
  provider
  evidence_digest
  observed_at
```

`GET /api/v1/nodes/{node_id}/capabilities` returns the latest normalized
records, following the existing human-facing API namespace. Agent writes
continue through certificate-bound `/agent/v1` endpoints; browsers do not
submit provider state.

## Fallback and Failure Semantics

Resolution happens per capability. A valid NVIDIA result is preferred where the
table above allows it. Otherwise, a safe native probe may supply the value and
records `provider=vonk-native`.

NVIDIA failure never silently changes semantics for a mutating operation. If a
future operation requires NVIDIA's updater and that provider is unavailable,
the operation is unsupported rather than redirected to a native command.

Provider health and workload health are orthogonal:

```text
node connection:                 healthy
platform provider:               degraded
workload:                        healthy
inference route:                 published
```

Inference is withdrawn only when its own health or an explicit workload safety
policy fails, not because optional diagnostics are unavailable.

## Privilege and Security Model

The first implementation is unprivileged and read-only. It does not install
sudo rules and does not run the agent as root. Tools requiring mutation or
unbounded log access remain unavailable.

If a later typed operation needs elevation, it must use the existing narrow
root-helper pattern with:

- one operation identifier;
- fixed executable and argument templates;
- validated, bounded input types;
- no environment-controlled search path;
- no arbitrary files, shell fragments, or command passthrough;
- operation fencing, audit receipt, timeout, and output limits.

The controller never receives SSH access and never constructs an NVIDIA command
line.

## User Experience

The node view separates machine management from AI management:

```text
Platform Management
  NVIDIA Enterprise Manageability    Available
  Hardware inventory                 NVIDIA Enterprise
  Firmware inventory                 NVIDIA Enterprise
  Native fallback                    Ready

AI Workload Management
  Recipes and runtimes                Vonk Forge
  Workloads                           Healthy
  Inference routes                    Published
```

When the package is absent, the UI says that Vonk is using native monitoring
and that enhanced NVIDIA diagnostics are unavailable. It must also state that
AI workload management is unaffected. The normal view summarizes provider
ownership; an evidence drawer exposes per-capability tool versions,
timestamps, and failure reasons.

## Delivery Phases

1. **Provider abstraction**: wrap current native inventory behind capability
   providers without changing behavior or the existing wire contract.
2. **Discovery**: add fixed-path, version-allowlisted NVIDIA discovery and
   report individual capability states without selecting NVIDIA data.
3. **Read-only normalization**: add fixtures from the pinned bundle and Spark
   canaries for identity, inventory, OS, firmware, health, and reset history.
4. **Provider preference**: prefer validated NVIDIA evidence per capability and
   retain explicit safe native fallback.
5. **Typed diagnostics**: separately add bounded diagnostic metadata and audit
   artifacts; keep mutating crash configuration disabled.
6. **Platform maintenance**: design and implement updates only after drain,
   reboot, reconnect, verification, rollback, and privilege semantics are
   accepted.

## First-implementation Acceptance

- Existing native probes execute through the provider abstraction.
- Absence of every NVIDIA tool preserves current agent and workload behavior.
- Each installed NVIDIA tool is discovered and version-gated independently.
- Unsupported versions and malformed envelopes fail closed for that provider.
- Read-only identity, inventory, OS, firmware, health, and reset evidence is
  normalized with provenance and bounded fixtures.
- Provider changes do not alter node identity or installed workloads.
- Provider failure does not interrupt a healthy workload or inference route.
- The controller stores current capability state and append-only transitions.
- The UI distinguishes Platform Management from AI Workload Management.
- No SSH, generic command, shell, runtime download, or new root agent is added.
- Linux unit and integration tests cover native-only, supported NVIDIA,
  unsupported-version, malformed-output, timeout, and fallback cases.
- Physical DGX Spark canaries prove the pinned package on both enrolled Sparks
  before NVIDIA becomes the preferred provider.

## References

- [Issue #44](https://github.com/CarstVaartjes/vonk-forge/issues/44)
- [NVIDIA DGX Spark Enterprise Manageability](https://docs.nvidia.com/dgx/dgx-spark/enterprise-manageability.html)
- [NVIDIA Enterprise Lifecycle Integration](https://docs.nvidia.com/dgx/dgx-spark/enterprise-fleet-lifecycle.html)
- [NVIDIA Enterprise Lifecycle Integration Scripts package](https://docscontent.nvidia.com/dc/04/5167e1c14532bac843d48d29bf36/enterprise-lifecycle-integration-scripts-20260520-1602.zip)
- [Existing component review](2026-08-03-existing-components-review.md)
