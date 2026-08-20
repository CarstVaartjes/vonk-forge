# Execution harness operations

An execution harness is the stable lifecycle contract between a recipe and the
Spark agent. It compiles declarative recipe inputs into the universal source,
build, distribute, install, run, health, route, stop, and uninstall operations.
Operators act through those operations and their preview digests; they do not
start a parallel container and then write Library state by hand.

See [Model and recipe identities](model-catalog.md) for model identity and revision concepts.

## Harness, distribution, and patch

A **harness** defines adapters, required capabilities, supported lifecycle, and
the compiler that turns a recipe into shell-free runtime intent. A **runtime
distribution** proves one exact upstream source and digest-pinned base image
implement that harness on a platform. It records dependencies, licenses,
security policy, offline behavior, and any verified distributed mechanism. A
**patch bundle** is an optional, immutable, recipe-referenced set of changes for
one exact target. It never silently modifies the harness or a resolved runtime.

This split permits several accepted distributions for one harness and permits a
recipe-local patch without inventing a ninth lifecycle. Add a new harness only
when an accepted target cannot implement the universal lifecycle through an
existing built-in.

## The eight built-in harnesses

The supported v1 seed is exactly `comfyui`, `diffusers`, `ds4`, `llama-cpp`,
`pytorch-pipeline`, `sglang`, `tensorrt-llm`, and `vllm`. A fresh development
database must resolve those eight `vonk-forge` execution-harness entities and
no placeholder harness state. Their names describe execution contracts, not a
promise that every model works with every distribution or topology; the recipe,
distribution capability, structural qualification, and Fleet admission still
have to agree.

## Interface publication

An `openai` interface follows the serving path
`client → Tailscale → Caddy → LiteLLM → accepted entrypoint`. The controller
publishes the recipe alias to LiteLLM only after all required ranks report fresh,
matching evidence and the route-serving lease is valid. Caddy owns static path
and trust boundaries; LiteLLM neither discovers containers nor resolves catalog
documents. Rank loss, stale evidence, stop, or lease loss withdraws the alias.

Artifact-producing interfaces are jobs rather than OpenAI model routes. Their
submission, progress, cancellation, and result artifacts use the declared
controller/job interface directly; a result location is not placed in LiteLLM.
Health and cleanup still use the same harness lifecycle and exact node evidence.

## Acceptance evidence

`scripts/accept-recipe` checks recipe node count before credentials or network
access. Structural qualification resolves every exact checked-in catalog
reference and source bundle. Read-only Fleet and agent snapshots bind each
operator selector to one certificate-bound `spk_…` identity, packaged agent
build and binary digests, certificate expiry, fresh inventory, capacity, and
fabric. Fleet selectors and SSH destinations are separate inputs: current
Fleet hostnames `spark-3542` and `spark-2297` map explicitly to inventory SSH
aliases `vonk-node-1` and `vonk-node-2`. The mapping is validated before any
network access and recorded in evidence. Read-only SSH preflight verifies
native `linux-arm64`, the exact NVIDIA driver and Docker runtime identities,
the native NVIDIA runtime, image manifest
access, and every model artifact URL. Dual-node preflight also runs
`scripts/validate_fabric.py --preflight-only`, binding selected Fleet IDs to
inventory aliases and checking reciprocal peers, both interfaces, HCAs, GID
indices, consumers, and bounded live path probes. Physical lifecycle work is
delegated to `scripts/run-development-slices`, the repository's canonical
public-API runner.

Evidence is canonical JSON in a mode-`0600` file. Its phase list is a strict
prefix of `authored`, `structurally-verified`, `container-verified`,
`spark-canary`, and `spark-accepted`. A phase advances only after independently
validating all exact runner outputs required at that point. State names without
an image digest, artifact-set digest, inference digest, cleanup operation, or
changed post-restart host boot ID cannot overstate acceptance. A
changed recipe, Library digest, node/certificate binding, qualification file,
API origin, topology, or noncanonical sidecar cannot resume older evidence.

At each restart checkpoint the runner records the host boot ID from serialized
Fleet telemetry. Heartbeat timestamps and Fleet `generated_at` are not restart
proof. Every selected node must return with a different boot ID before route
and inference evidence can be bound to the new identity; cleanup remains
deferred until that gate succeeds.

One-Spark acceptance pauses after canary inference for an offline restart.
Distributed acceptance additionally pauses for failure-rank loss, route
withdrawal, rank recovery, recovered inference, and a final offline restart.
The same command is rerun after each explicit operator action; checkpoint exit
code `4` means the evidence is valid but not complete. Only the full single-node
ladder, or the full distributed rank-loss/recovery ladder, cleanup, and changed
boot identities can write `spark-accepted`.
