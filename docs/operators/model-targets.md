# Model target ledger

This is the honest inventory behind the Library. It is broader than the set of
recipes that are currently runnable on the enrolled Sparks: a target can be an
accepted recipe, a candidate awaiting exact artifacts and Spark evidence, or a
blocked upstream that must remain out of the normal catalog.

The machine-readable ledger lives in
[`config/model-targets/`](../../config/model-targets/). Each entry names the
**model group**, **model**, and exact **version** conceptually, then records the
candidate harnesses, supported topology shape, upstream starting point, and
readiness state. A precision or checkpoint variant is a separate version; it is
never an argument that silently changes an existing recipe.

## Readiness states

| State | Meaning in the product | Visible as a runnable default? |
| --- | --- | --- |
| `accepted` | Exact artifacts, image/runtime, topology, lifecycle, and output evidence have passed the current acceptance ladder. | Yes |
| `candidate` | A useful target with an upstream starting point, but at least one exact artifact, ARM64 runtime, resource envelope, or Spark acceptance gate is still missing. | No; visible in the target ledger only |
| `blocked` | We deliberately do not have a downloadable checkpoint or an accepted fleet topology. | No |

As of 2026-08-16, only the two DeepSeek V4 Flash 0731 definitions are
accepted: the single-Spark DS4 IQ2/Q2 recipe and the two-Spark official DSpark
Mia recipe. The rest of the ledger is discovery and qualification work, not a
promise that a hosted model, community container, or mutable tag can be
installed.

## Why candidate entries are useful

The ledger lets the UI show the difference between “we want to support this”
and “this is safe to run.” It also keeps model research reproducible: when a
candidate becomes a recipe, its exact upstream commit, artifact hashes, runtime
image digest, source bundle, topology, and validation evidence are copied into
the immutable v1 catalog entities. The candidate entry itself does not grant
execution authority.

The current list covers language and reasoning, image generation/editing, 3D
generation/rigging, video, and audio/music. It includes both one-Spark and
multi-Spark possibilities, but does not assume that adding nodes automatically
creates a distributed recipe. Each topology must be accepted independently.

## Adding a target

1. Add or update a ledger entry with an upstream project URL and a clear state.
2. For a candidate, pin the exact model revision and artifact hashes before
   creating a model-version entity; do not use `main`, `latest`, or an unpinned
   download URL in a recipe.
3. Select an existing built-in harness whenever it can implement the lifecycle.
   Add a new harness only when the target cannot satisfy the universal
   inspect/prepare/verify/start/ready/invoke/recover/stop contract.
4. Build a digest-pinned ARM64 runtime distribution and, when needed, a
   recipe-local patch bundle. Patches apply during image build, never at
   container startup.
5. Author one recipe per topology and run structural, container, and Spark
   acceptance. Only then change the entry to `accepted` and expose it as a
   default.

License and acceptable-use metadata remains the operator's responsibility. The
ledger deliberately has no territory-filtering or jurisdiction-automation
field: it tracks technical readiness, not legal advice.

## Research sources

The starting points are primary upstream repositories and model cards, such as
[NVIDIA Nemotron 3 Super NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4),
[NVIDIA Nemotron 3 Nano Omni NVFP4](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4),
[Qwen Image](https://github.com/QwenLM/Qwen-Image),
[TRELLIS.2](https://github.com/microsoft/TRELLIS.2),
[Step1X-3D](https://github.com/stepfun-ai/Step1X-3D), and
[Hunyuan3D-Omni](https://github.com/Tencent-Hunyuan/Hunyuan3D-Omni). Research
links are discovery inputs; only immutable revisions and measured evidence
enter a recipe.
