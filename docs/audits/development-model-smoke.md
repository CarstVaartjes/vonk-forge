# Native v1 DS4 development qualification

Date: 2026-08-16

Status: structural and behaviorally faked container gates complete; physical
linux/arm64 GPU acceptance remains Task 9.

## Selected lane

The development single-node lane is the native v1 recipe
`config/recipes/deepseek-v4-flash-0731-ds4-single.json`. It runs on
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

Structural qualification is portable:

```bash
scripts/qualify-development-model \
  --recipe config/recipes/deepseek-v4-flash-0731-ds4-single.json \
  --level structural \
  --output .state/development-acceptance/ds4-structural.json
```

Container qualification is an executable build, start, health, invocation,
bounded stop, restart, and cleanup path. Run it on a linux/arm64 Spark with the
two exact model files installed beneath the artifact root:

```bash
scripts/run-development-slices \
  --phase model-single \
  --level container \
  --engine docker \
  --artifact-root /var/lib/vonk/models \
  --evidence-file .state/development-acceptance/ds4-container.json \
  --timeout-seconds 1800
```

Evidence is canonical JSON written atomically with mode `0600`. Qualification
fails closed on unsupported architecture, mutable image resolution, contract
failure, engine failure, unhealthy service, invocation failure, or cleanup
failure.

## Current publication gate

The x86_64 development host can execute structural qualification and the
bounded fake-engine behavior suite. A local container run exits 3 with
`status: environment-limited` because the recipes require linux/arm64. This is
an environment statement, not successful container qualification.

## Physical acceptance still required

Task 9 must run the real container path on DGX Spark hardware and retain GPU,
memory, latency, restart, and cleanup evidence. This audit does not claim
physical ARM64/GPU acceptance.
