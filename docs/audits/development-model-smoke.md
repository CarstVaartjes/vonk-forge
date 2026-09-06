# Native v1 DS4 structural qualification

Date: 2026-08-16

Status: structural qualification and behaviorally faked lifecycle gates
complete. This record does not claim the Spark hardware gate.

## Selected lane

The development single-node lane is the native v1 recipe
`../vonk-forge-recipes/recipes/deepseek-v4-flash-0731-ds4-single.json`. It runs on
`linux/arm64` and uses DS4's current 128-GB default: the imatrix mixed
quantization with IQ2_XXS gate/up and Q2_K down projections. It is not NVFP4.

Deleted prototype development catalogs are not accepted inputs and have no
compatibility reader.

## Immutable identities

- Canonical DS4 source: `https://github.com/antirez/ds4` at
  `84cc882352757baf628a1776badf7cc54d584e28`.
- Model repository: `antirez/deepseek-v4-gguf` at
  `e7f04037032990db0346398d249baf9fb9df1ccc`.
- Target GGUF:
  `DeepSeek-V4-Flash-IQ2XXS-w2Q2K-AProjQ8-SExpQ8-OutQ8-chat-v2-imatrix-0731.gguf`,
  86,720,111,488 bytes, SHA-256
  `ca22ae2f838e14077c22bc1c1417b71b45b5e5a3687bd96c2ac6e17fdb6261c0`.
- DSpark support GGUF: `DeepSeek-V4-Flash-DSpark-support-0731.gguf`,
  5,989,114,272 bytes, SHA-256
  `7e319924541db3f7a163ed7e11d7532a70d48228ab59d36cb81e1d4511885360`.
- Runtime base: `nvcr.io/nvidia/cuda:13.0.1-runtime-ubuntu24.04` at
  `sha256:36050649ad1acc5d3de2c26620191c25850fb12a5771b6c22996033003d952e4`.

All source, image, and model references are immutable. The build and runtime
are offline after installation.

## Qualification behavior

Structural qualification is portable and resolves the model entities and
build context from an exact checkout of the canonical recipe repository. This
checkout is a dev/CI qualification input only; the production Controller
follows automatically refreshed global repository metadata:

```bash
scripts/qualify-recipe \
  --recipe ../vonk-forge-recipes/recipes/deepseek-v4-flash-0731-ds4-single.json \
  --library-root ../vonk-forge-recipes \
  --platform-root . \
  --level structural \
  > .state/development-acceptance/ds4-structural.json
```

Execute Spark acceptance through Controller Run/Switch as documented in
[Development workload acceptance](../runbooks/development-agent-workloads.md).
Retain the Controller plan digest, operation state, exact artifact receipts,
route transitions, restart observations, and cleanup result separately from
the qualifier output. Run the declared serving checks with the qualifier's
`--serving-url` and `--evidence-ledger` options after the route is active.

## Current publication gate

The development host can execute structural qualification and the bounded
fake-engine behavior suite. The qualifier's container level is currently
unavailable pending production `CompiledExecutionPlan` materializer linkage;
its `environment-limited` result is not successful container qualification.

## Spark acceptance still required

The production materializer must first make the real container path executable.
That path must then run on DGX Spark hardware and retain GPU, memory, latency,
restart, and cleanup evidence before this recipe can move from
candidate/structurally-verified to `spark-accepted` and then `default`. This
audit does not claim physical ARM64/GPU acceptance.
