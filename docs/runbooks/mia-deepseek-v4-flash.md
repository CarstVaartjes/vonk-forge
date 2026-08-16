# Mia DeepSeek V4 Flash on two DGX Sparks

This runbook covers the native v1 two-node Mia recipe. It is genuine vLLM
multiprocessing tensor parallelism with TP=2. It is not a claim that the generic
vLLM harness supports arbitrary distributed execution.

Physical ARM64/GPU acceptance remains Task 9. The commands below are the real
executable qualification path, but `--level container` must be run on a
linux/arm64 DGX Spark host with Docker, both GPUs, and the exact model snapshot
already installed.

## Pinned release

The authoritative identities are:

- Mia recipe source:
  `https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark` at
  `f752cd04ab30f2cf42077dd8811a5e1e682d63e7`.
- Anemll distribution source: `https://github.com/Anemll/dspark-vllm-gx10`
  at `47503f8e38dadd4dededca798150db2619594fce`.
- Anemll linux/arm64 image:
  `ghcr.io/anemll/dspark-vllm-gx10@sha256:a83948492cf13df455170fb42885f5ef4db54fefe0feff0f841ecbff464ac9d8`.
- Official model snapshot: `deepseek-ai/DeepSeek-V4-Flash-DSpark` at
  `62af8fffb2f7030cac4de2f0169f5b8d1101b646`, public and ungated, with 74
  files totaling `166898666055` bytes (166,898,666,055-byte model checkpoint).
- Patched vLLM source:
  `https://github.com/vllm-project/vllm` at
  `752a3a504485790a2e8491cacbb35c137339ad34`.

The runtime image and the model checkpoint are separate immutable objects. An
exact verified cache hit does not redownload either object. Every first install
still requires full digest verification before an object can become trusted.

The source and final image retain the Mia MIT license, vLLM Apache-2.0 license,
and Vonk Forge notice. The image is labeled `MIT AND Apache-2.0`.

## Before the first run

No Hugging Face token is required because the selected snapshot is public and
ungated. Qualification performs no startup network fetch. Install every model
file and all source/image inputs before qualification, preserving the contract
paths and hashes.

Mia upstream bind-mounts and applies patches at startup. The native v1 source
bundle instead vendors and hashes those files, applies them while building the
image, and verifies the installed result. There is no startup patching, source
mutation, package installation, or network access.

The distribution capability binds one exact topology: two linux/arm64 nodes,
world size 2, tensor parallel size 2, one `entrypoint`, and one `worker`. The
compiler refuses this topology unless a verified distribution explicitly
implements it.

The controller and Rust runtime project the structured rendezvous values
`VONK_LOCAL_ADDR`, `VONK_MASTER_ADDR`, and `VONK_MASTER_PORT` for each rank.
The same verified capability projects the exact rank-specific fabric contract:
`NCCL_IB_HCA`, `NCCL_SOCKET_IFNAME`, `NCCL_IB_GID_INDEX`,
`GLOO_SOCKET_IFNAME`, and `TP_SOCKET_IFNAME`. The wrapper fails before launch
when any placement or fabric value is missing or differs.

Rank 1 runs headless. Rank 0 is the sole endpoint owner and may become ready
only after both ranks are healthy.

## Qualify the exact inputs

Structural qualification works without GPU hardware and proves strict v1
contract, reference, compiler, and adapter compatibility:

```bash
cd '<REPOSITORY_CHECKOUT>'
scripts/qualify-development-model \
  --recipe config/recipes/deepseek-v4-flash-0731-mia-dual.json \
  --level structural \
  --output '<EVIDENCE_DIRECTORY>/mia-structural.json'
```

On a supported DGX Spark host, execute the complete path:

```bash
scripts/run-development-slices \
  --api-base 'http://127.0.0.1:<LOCAL_API_PORT>' \
  --inference-base 'http://127.0.0.1:<LOCAL_INFERENCE_PORT>' \
  --admin-token-file '<EVIDENCE_DIRECTORY>/admin-token' \
  --inference-token-file '<LOCAL_SECRETS_DIR>/litellm-master-key' \
  --phase model-multinode \
  --qualification-file '<EVIDENCE_DIRECTORY>/mia-structural.json' \
  --builder-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_1_NODE_ID>' \
  --target-node '<SPARK_2_NODE_ID>' \
  --failure-node '<SPARK_2_NODE_ID>' \
  --evidence-file '<EVIDENCE_DIRECTORY>/mia-container.json' \
  --timeout-seconds 3600 \
  --stop-after inference-ok
```

The qualifier resolves the digest-pinned base image, verifies the expected
linux/arm64 manifest, builds offline from the retained context, starts worker
then entrypoint, checks collective and endpoint-owner readiness, invokes the
OpenAI-compatible endpoint, performs bounded stop/restart, and cleans up. It
writes canonical evidence and fails closed on every incomplete phase. Resume
with the identical command and evidence file after each documented failure or
restart action, advancing only when the corresponding checkpoint is proven.

## Failure, recovery, and cleanup

The lifecycle consumer withdraws the route as soon as either rank is failed or
stale. Recovery is bounded and ordered: stop the exact gang, start the worker,
then start the endpoint owner. The route is republished only when both ranks
provide fresh healthy start evidence and invocation succeeds again.

A failed installation is retried through one exact retry of the stored plan.
That retry preserves the same installation identity and immutable model and
image caches. It never re-plans against mutable inventory and never deletes
shared content to hide a failure. A second failure requires operator diagnosis.

Qualification evidence is non-secret canonical JSON, written atomically with
mode `0600`. Retain it with the exact recipe and environment inventory. Do not
claim physical acceptance from the bounded fake-engine tests or an x86_64
`environment-limited` result; that evidence belongs to Task 9.
