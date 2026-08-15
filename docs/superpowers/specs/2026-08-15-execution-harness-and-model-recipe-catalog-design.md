# Execution Harness and Model Recipe Catalog Design

**Date:** 2026-08-15

**Status:** Approved in operator design review

## Outcome

Replace the pre-production source-first recipe contract with a clean contract
that separates four concerns:

1. model identity from model artifacts;
2. reusable execution engines from model-specific runtime distributions and
   patches;
3. installation and execution recipes from both model and engine identity;
4. execution topology from request/response modality.

Vonk Forge will provide a curated set of excellent default recipes without
restricting the number of alternative or user-authored recipes. A recipe
selects one exact primary model version, one exact execution-harness version,
all exact dependency versions, one exact topology, and the complete procedure
for building, installing, running, validating, recovering, and stopping that
combination.

The existing prototype recipe contract is pre-production scaffolding. It is
replaced directly. There is no compatibility layer, legacy import mode, or
duplicate old catalog after the existing Mia and DS4 assets have been
recreated.

## Goals

- Establish one engine-independent Vonk execution lifecycle.
- Implement the small set of execution harnesses required by the target model
  catalog.
- Represent model group, model, and concrete model version as different
  identities.
- Make every recipe select the most detailed model version available rather
  than a mutable model family or convenience alias.
- Allow multiple recipes for the same model version when an engine, topology,
  memory, quality, or performance tradeoff is useful.
- Allow user-authored recipes and recipe-bundled custom adapters without
  changing the control plane for every new model.
- Migrate the current DS4 and Mia workloads onto the common structure before
  adding new model recipes.
- Support one Spark, two Sparks, and larger Spark clusters without hard-coded
  two-node assumptions.
- Distinguish genuine distributed inference from independent replicas.
- Test every published default recipe in proportion to its advertised status.
- Track models that lack downloadable weights or a viable topology without
  pretending they are installable.
- Keep secrets and model weights out of public runtime images.

## Non-goals

- Training or fine-tuning orchestration is not part of this inference-recipe
  pass.
- Vonk Forge does not determine an operator's jurisdiction, interpret a model
  license for them, or automatically hide recipes by territory.
- A hosted API is not represented as a local model version.
- Merely launching the same model on multiple nodes is not described as
  model-sharded or distributed inference.
- Every theoretically compatible engine does not need a recipe. Alternatives
  are authored when they provide a useful operational tradeoff.
- A model-specific patch does not automatically become a new global execution
  harness.

## Vocabulary and identity model

The catalog hierarchy is:

```text
ModelGroup
  `-- Model
        `-- ModelVersion

Recipe
  |-- selects exactly one primary ModelVersion
  |-- selects one ExecutionHarnessVersion
  |-- selects one RuntimeDistribution
  |-- may select one PatchBundle
  |-- selects exact dependency ModelVersions
  |-- defines installation
  |-- defines one topology
  `-- defines interfaces and validation
```

### Model group

A `ModelGroup` is the continuing product lineage users recognize. Examples are
DeepSeek Flash, Nemotron, Qwen Image, LTX, and TRELLIS.

It carries stable discovery metadata only: title, publisher, broad capability
tags, and links to its models. It does not carry artifacts, quantization,
runtime arguments, installation instructions, or an execution engine.

### Model

A `Model` is a specific upstream release or checkpoint generation inside a
group. Examples are DeepSeek V4 Flash 0731, Qwen Image 2512, and LTX 2.5.

The model records architecture and product-level capabilities. It does not
pretend that BF16, FP8, NVFP4, GGUF, a distilled checkpoint, or a publisher's
derived conversion are interchangeable.

### Model version

A `ModelVersion` identifies the actual immutable artifact set used for
execution. A sharded checkpoint, tokenizer, configuration, encoder, VAE,
drafter, adapter, or upscaler can make the set contain several physical files;
the identity still denotes one concrete runnable weight representation.

The version records:

- immutable repository and revision;
- exact artifact inventory and hashes;
- publisher and derivation lineage;
- format, precision, and quantization;
- parameter and active-parameter counts when known;
- supported context, resolution, frame, or sample limits;
- download and installed sizes;
- license identifier, URL, attribution, access gating, and operator-acceptance
  requirements;
- exact dependency versions;
- supersession and availability state.

`NVFP4` is therefore not merely a runtime argument. A DeepSeek V4 Flash 0731
NVFP4 version identifies the real NVFP4 artifact set and its provenance.

### Recipe

A `Recipe` is the complete installable and runnable binding. It selects:

- exactly one primary model version;
- exact auxiliary model versions;
- an execution harness and harness contract version;
- an exact runtime distribution or source revision;
- an optional exact patch bundle;
- the source-first build and installation procedure;
- one exact node count and topology;
- structured runtime parameters;
- resource and fabric requirements;
- one or more interface adapters;
- validation and acceptance cases.

The same model version can have several recipes. For example, DeepSeek V4
Flash 0731 can have a quantized DS4 single-Spark recipe and an official-weight
vLLM/Mia two-Spark recipe.

### Recipe revision

A recipe revision changes the execution binding without claiming that the
model artifacts changed. It can update a build fix, runtime distribution,
patch, argument, validator, or measured resource envelope. An installed recipe
remains pinned to its exact revision until the operator changes it.

## Clean recipe-contract replacement

The replacement contract is the first supported recipe contract and the only
writable recipe contract after this work. Its schema version is 1. The
incompatible pre-production prototype is discarded rather than treated as a
public version that requires migration or compatibility.

The current prototype shortcomings that require replacement are:

- model group, model, and model version are collapsed into free-form workload
  metadata and artifact entries;
- the runtime adapter is a model-specific string rather than a reusable engine
  identity;
- endpoint protocol is hard-coded to OpenAI;
- one recipe can contain several deployment profiles even though each profile
  needs independent acceptance and resource evidence;
- engine distribution and recipe-local patch lineage are not first-class;
- output validation cannot honestly represent images, audio, video, or meshes.

The v1 catalog is split into independently validated documents:

```text
config/model-groups/
config/models/
config/model-versions/
config/execution-harnesses/
config/runtime-distributions/
config/patch-bundles/
config/recipes/
config/model-targets/
```

The implementation may refine filenames, but these identities must not be
collapsed back into one monolithic recipe document.

## Universal execution contract

Every execution harness implements the same state machine:

```text
inspect -> prepare -> verify -> start -> ready -> invoke
                                     |        |
                                     |        `-> inspect
                                     `-> recover -> stop -> verify-stopped
```

The contract covers:

- immutable runtime and artifact verification;
- offline startup after installation;
- explicit role and rank identity;
- readiness distinct from process liveness;
- structured invocation through an interface adapter;
- bounded stop and cleanup;
- interrupted-start and interrupted-stop recovery;
- endpoint and artifact publication only after readiness;
- canonical evidence containing all selected identities and measurements.

The contract does not dictate whether a result is JSON text, an image, audio,
video, or a mesh.

## Execution harnesses

The initial harness set is target-driven and deliberately small.

| Harness | Responsibility | Initial targets |
|---|---|---|
| `vllm` | OpenAI-compatible LLM/VLM serving and supported distributed execution | DeepSeek/Mia, Nemotron, Laguna, Qwen, Gemma, GLM |
| `sglang` | LLM/VLM serving and supported diffusion pipelines | Qwen alternatives, canary media paths, MiniMax H3 where licensed |
| `tensorrt-llm` | NVIDIA-native optimized LLM and visual-generation execution | Nemotron and selected NVIDIA-qualified alternatives |
| `llama-cpp` | GGUF serving and low-dependency alternatives | compact or heavily quantized language-model alternatives |
| `ds4` | DS4's specialized target-plus-drafter execution | DeepSeek V4 Flash 0731 single Spark |
| `diffusers` | Reference diffusion pipelines | Qwen Image, LTX, Hunyuan, compatible media models |
| `comfyui` | Reproducible graph/workflow execution | image and media alternatives distributed as official workflows |
| `pytorch-pipeline` | Structured execution of upstream Python inference pipelines | 3D, rigging, MOVA, Foley, and specialized media models |

NVIDIA's own DGX Spark performance guidance covers TensorRT-LLM, vLLM,
SGLang, llama.cpp, and diffusion execution, making these native reference paths
rather than arbitrary Vonk-specific abstractions.

The repository already contains strict import compilers for vLLM, SGLang, and
llama.cpp. Those become components of the corresponding harnesses. They are not
maintained as a separate competing runtime model.

### Harness version

An `ExecutionHarnessVersion` defines:

- lifecycle protocol and commands;
- structured configuration schema;
- supported role shapes;
- supported topology kinds;
- interface-adapter compatibility;
- mount and state layout;
- security requirements;
- evidence schema;
- conformance tests.

A harness never selects a model.

### Runtime distribution

A `RuntimeDistribution` selects the actual engine implementation: an upstream
release, NVIDIA container, compatible fork, or source commit. It records exact
source and image identities, platform, dependencies, build requirements, and
supported harness version.

This distinction allows an Anemll vLLM fork and an NVIDIA vLLM container to
implement the same Vonk vLLM harness without claiming that their binaries are
identical.

### Patch bundles

Many useful Spark recipes patch vLLM or another engine. A patch does not bypass
the harness. The recipe selects a base runtime distribution and an immutable
patch bundle; the source-first build creates a derived runtime image whose
digest covers both.

Each patch bundle records:

- exact base source revision;
- ordered patch inventory and hashes;
- expected pre-patch file hashes;
- purpose and upstream issue/reference;
- compatible model versions and runtime distributions;
- structured argument or capability changes;
- removal condition;
- license and attribution;
- resulting source-bundle and image identities.

Patches are applied during a network-constrained build. Runtime containers do
not download or apply patches on startup.

Ownership rules are:

- a model-specific compatibility fix remains recipe-local;
- a reusable engine fix used by several recipes is promoted to a shared
  runtime distribution or harness release;
- a change to lifecycle or protocol semantics requires a new harness version;
- a runtime that no longer conforms to the engine harness requires a different
  harness or a recipe-bundled custom adapter.

The current Mia runtime becomes a vLLM harness plus a pinned Anemll/vLLM
distribution, pinned Mia patch bundle, exact DeepSeek version, and exact
two-Spark recipe.

## User-authored recipes and custom adapters

The curated catalog is not an allowlist of all possible models or recipes.
Users can add:

- another recipe using a built-in harness;
- another model version;
- another topology for an existing version;
- another runtime distribution compatible with a harness;
- or a recipe-bundled custom adapter implementing the universal lifecycle.

A bundled adapter receives no additional privilege merely because it is
custom. It must pass the same schema, source, build, security, lifecycle, and
evidence checks. This keeps the platform extensible without adding model names
or engine-specific branches to the control plane.

## Interfaces and output validation

Interface and output shape are orthogonal to execution engine. A Diffusers
harness can serve image, audio, or video pipelines; the chosen recipe selects
the appropriate interface and validators.

The initial interface vocabulary covers:

- OpenAI-compatible text and multimodal requests;
- image generation and image editing;
- asynchronous media jobs;
- artifact-producing jobs;
- direct internal conformance invocation.

Validators are composable and modality-specific:

- JSON/schema and non-empty semantic text;
- image decoding, dimensions, color mode, and deterministic fixture metadata;
- audio decoding, channels, sample rate, duration, and non-silence;
- video decoding, dimensions, frames, duration, and optional synchronized audio;
- mesh/container parsing, vertex/face/material counts, finite coordinates, and
  expected output format;
- generic artifact digest, size, and media type.

The validation layer must not require a separate execution harness merely
because a result is a file instead of a chat response.

## Topology model

Each recipe declares one exact topology and node count. Supported topology
kinds are:

- single;
- tensor parallel;
- pipeline parallel;
- expert parallel;
- context or sequence parallel;
- hybrid distributed;
- replicated service.

Distributed recipes identify the actual collective mechanism, rank roles,
rendezvous, fabric, address selection, start order, stop order, and failure
semantics. A `ray` or `mpi` launcher is orchestration, not proof of model
parallelism.

Replicated services are useful for throughput and availability but remain
explicitly labeled replicas. They are never described as one multi-node model
execution.

Default recipes use exact accepted node counts. The execution contract and
schemas contain no assumption that a cluster has exactly two nodes. A four-node
or larger recipe is first-class once it has matching acceptance evidence.

## Default and installed-state policy

Defaults are catalog pointers, not mutable model aliases inside recipes.

- A model group can select a current default model.
- A model can select a current default version for a purpose.
- A model version can select a default recipe for an exact topology.
- Alternative recipes remain visible.
- A fresh install or explicit reset selects the newest accepted default.
- An existing installation remains pinned and visible until its operator
  explicitly changes it.
- A successor hides a superseded version from normal fresh-install discovery
  only after acceptance; provenance and rollback records remain available.

## Initial target catalog

This list reflects source-first research completed on 2026-08-15. Exact source
commits, checkpoint revisions, artifact inventories, and licenses are resolved
again and pinned during implementation.

### Language, reasoning, coding, and multimodal understanding

| Model group | Model and versions | Intended recipes | Current disposition |
|---|---|---|---|
| DeepSeek Flash | DeepSeek V4 Flash 0731 official artifact set; DS4 target and drafter derivative | vLLM/Mia TP2; DS4 single | Primary dual- and single-Spark defaults |
| Nemotron | 3.5 Lightning 30B-A3B NVFP4; 3 Super 120B-A12B NVFP4; 3 Nano Omni 30B-A3B NVFP4 | vLLM single; selected TensorRT-LLM alternatives | In recipe scope |
| Nemotron | 3 Ultra 550B-A55B NVFP4 | multi-node vLLM/TensorRT-LLM candidate | Tracked; no viable accepted Spark topology yet |
| Laguna | S 2.1 NVFP4; XS 2.1 NVFP4 | vLLM single; S 2.1 TP2 experimental | In recipe scope |
| Qwen | Qwen3.5 9B; Qwen3.6 35B-A3B FP8/NVFP4; Qwen3.8 27B FP8 | vLLM/SGLang single; Qwen3.6 TP2 alternative | 3.6 mature candidate, 3.8 canary |
| Gemma | Gemma 4 26B-A4B | vLLM single | Independent VLM validation target |
| GLM | GLM 5.2 official and exact documented quantized derivative | vLLM TP4/EP candidate | Tracked until matching four-Spark acceptance is available |

Primary sources include the official DeepSeek V4 Flash 0731 model, the current
Mia two-Spark repository, DS4-on-Spark, NVIDIA's Nemotron repository and Spark
playbooks, Poolside's model collection, and official Qwen model cards.

Older DeepSeek preview artifacts, Nemotron 49B Super/9B Nano/8B VL, Laguna
XS.2, and Qwen3-VL-8B are superseded for fresh defaults after their successors
are accepted.

### Image generation and editing

| Model group | Model versions | Intended recipes | Current disposition |
|---|---|---|---|
| Qwen Image | 2512 BF16; 2512 Lightning four-step | Diffusers reference; SGLang/vLLM-Omni alternatives; ComfyUI workflow | Primary text-to-image targets |
| Qwen Image Edit | 2511 BF16; optional Lightning version | Diffusers reference; SGLang/ComfyUI alternatives | Primary image-editing targets |
| Qwen Image Layered | current BF16 artifact set | Diffusers/ComfyUI single | Specialty layered-image target |
| NVIDIA Qwen Image Flash | current released artifact set | Diffusers/SGLang/TensorRT visual generation | Canary pending GB10 acceptance |
| Qwen Image 2.0 and 3.0 | hosted products only | none | Tracked; no downloadable official weights |

The downloadable Qwen Image defaults remain 2512, Edit 2511, and Layered;
newer hosted product names do not become local model versions without official
weights and terms.

### 3D generation and rigging

| Model group | Model versions | Intended recipes | Current disposition |
|---|---|---|---|
| TRELLIS | TRELLIS.2 4B | PyTorch pipeline single | Highest-confidence 3D canary |
| Pixal3D | current TRELLIS.2-backed checkpoint | PyTorch pipeline single | In recipe scope |
| Step1X-3D | geometry, label-geometry, and texture artifact sets | PyTorch pipeline single | ARM64/SM121 canary required |
| TripoSG | Scribble checkpoint; base retained only for provenance | PyTorch pipeline single | Specialty canary; base superseded as default |
| SkinTokens | TokenRig and SkinTokens exact dependency set | PyTorch pipeline single | ARM64 dependency canary required |
| Hunyuan3D | Hunyuan3D-Omni current checkpoint | PyTorch pipeline single | Worldwide recipe target; operator reviews license |

TRELLIS.2 and Pixal3D support real synchronized multi-node training, not
model-sharded multi-node inference. No multi-Spark inference recipe is
advertised for them without a new executable mechanism and acceptance result.

### Video and audio generation

| Model group | Model versions | Intended recipes | Current disposition |
|---|---|---|---|
| LTX | LTX 2.5 distilled BF16; development BF16; official INT8/NVFP4 variants after canary | Diffusers/native PyTorch/ComfyUI single | Primary audio-video candidate |
| MOVA | MOVA 360p; MOVA 720p | PyTorch pipeline single | 360p first; 720p after memory acceptance |
| HunyuanVideo | 1.5 T2V/I2V and distilled versions | Diffusers/native PyTorch single | Worldwide recipe target; operator reviews license |
| HunyuanVideo Foley | XL and XXL | PyTorch pipeline single | Worldwide audio-generation target |
| MiniMax H3 | FL2VA and Ref2VA exact artifact sets | SGLang/Diffusers candidate | Worldwide recipe target; operator reviews license and topology |
| MiniMax Music | Music 3.0 hosted product | none | Tracked; no downloadable checkpoint |

LTX 2.5's published multi-GPU path is single-host and cannot span two
one-GPU Sparks. MOVA and HunyuanVideo contain genuine parallel primitives but
lack accepted cross-node Spark execution. MiniMax H3 has documented cross-node
attention on larger NVIDIA systems but no accepted Spark topology. These facts
are tracked without inventing multi-Spark defaults.

## Licensing and access

Model-version documents record exact license and access facts. Recipes surface
those facts before installation and cannot weaken them.

Vonk Forge does not:

- detect location or jurisdiction;
- encode excluded-territory enforcement;
- hide recipes by inferred operator identity;
- decide that research, testing, or noncommercial use creates an exception;
- redistribute weights whose terms prohibit redistribution.

License compliance is the operator's responsibility. The project can still
author globally useful recipes for publicly downloadable but restricted
weights. Local acceptance only claims what was actually and lawfully executed;
structural or container validation is not mislabeled as weight-backed Spark
acceptance.

## Source and practitioner registry

Upstream model authors remain the primary authority for identities, weights,
licenses, and supported runtimes. Spark-specific operational evidence is also
tracked from:

- NVIDIA DGX Spark documentation, porting guidance, playbooks, and forums;
- MiaAI-Lab;
- antirez and DS4-on-Spark maintainers;
- 0xSero;
- SparkBench;
- Sggin1/DGX-SPARK;
- elsung and other reproducible multi-Spark projects;
- Haruni image-generation measurements;
- reproducible community reports that publish exact commands, revisions, and
  measurements.

Community evidence can justify a canary or alternative but cannot override an
official license, artifact identity, or incompatible runtime claim.

## Security and reproducibility

Every harness and recipe preserves the existing trust boundaries:

- no secrets, provider tokens, signing keys, or model weights in public images;
- exact source commits, image digests, artifact revisions, and hashes;
- public build networking limited to declared hosts;
- offline runtime after installation;
- numeric non-root runtime identity;
- read-only root filesystem wherever the engine permits it;
- dropped capabilities and no-new-privileges;
- no Docker or Podman socket;
- model artifacts mounted read-only;
- writable state and generated outputs isolated from model artifacts;
- host networking, IPC, memlock, and fabric devices granted only by an exact
  topology capability that requires them;
- canonical evidence binds model version, dependencies, harness version,
  runtime distribution, patch bundle, recipe revision, built image digest,
  nodes, and measured result.

## Acceptance ladder

Harness acceptance precedes full model-recipe acceptance.

### Harness conformance

1. Schema and structured-command policy tests.
2. Synthetic lifecycle tests for prepare, verify, start, ready, invoke, stop,
   recovery, and evidence.
3. Security and mount-policy tests.
4. ARM64 image build and dependency audit.
5. Tiny permitted-model or synthetic GPU canary on one Spark.
6. Distributed synthetic collective canary for each distributed mechanism.

### Existing-runtime replacement

This is a pre-production cutover. The implementation does not preserve,
translate, or provide compatibility for prototype catalog, installation, run,
or acceptance records. Deploying the replacement requires a clean reset of the
recipe-domain development state. Only immutable model and build caches that are
independently verified by their content digests may be reused.

1. Replace DS4 with the DS4 harness and one exact single-Spark recipe.
2. Replace Mia with the vLLM harness, Anemll distribution, Mia patch bundle, and
   one exact two-Spark recipe.
3. Repeat all existing DS4 and Mia physical acceptance rather than reusing
   historical evidence.
4. Remove old model-specific runtime identities and every prototype contract
   path in the same cutover; there is no dual-contract period.

### Model recipe acceptance

1. Resolve and record exact upstream identities and terms.
2. Build the network-constrained ARM64 runtime image.
3. Verify exact artifact download and offline reuse.
4. Run a bounded low-resource canary.
5. Run normal inference and modality-specific output validation.
6. Measure startup peak, steady memory, runtime growth, disk, and duration.
7. Verify stop, restart, interrupted recovery, and uninstall behavior.
8. Verify route or artifact publication and withdrawal.
9. For distributed recipes, kill or isolate each rank and prove complete
   withdrawal and recovery.
10. Record quality and performance evidence appropriate to the model's role.

### Acceptance states

Recipe state is explicit:

- `authored`;
- `structurally-verified`;
- `container-verified`;
- `spark-canary`;
- `spark-accepted`;
- `default`;
- `blocked`;
- `superseded`.

Only `spark-accepted` recipes can become curated defaults. A recipe requiring
four nodes cannot become four-node accepted from a two-node simulation. A
restricted model tested only with synthetic artifacts does not become
weight-backed Spark accepted.

## Implementation slices

The implementation order is fixed:

1. Replace the recipe schema and identity model.
2. Implement shared lifecycle conformance fixtures.
3. Implement the target-driven execution harnesses.
4. Replace and freshly accept DS4.
5. Replace and freshly accept Mia.
6. Add language and multimodal-understanding model versions and recipes.
7. Add image model versions and recipes.
8. Add video and audio model versions and recipes.
9. Add 3D and rigging model versions and recipes.
10. Publish the curated catalog, alternatives, blocked tracker, provenance,
    architecture diagrams, and fresh-install/operator documentation.

Each slice is test-driven and ends with reviewable evidence. Large weight
downloads are reused through immutable content-addressed caches and are not
repeated merely because recipe metadata changes.

## Documentation outcome

The website and repository documentation explain:

- model group versus model versus model version;
- model version versus recipe revision;
- harness versus runtime distribution versus patch bundle;
- single, genuinely distributed, and replicated topologies;
- default selection and installed pinning;
- recipe authoring with built-in and custom adapters;
- license and gated-weight operator responsibilities;
- acceptance states and what each state proves;
- fresh installation and model installation on one or many Sparks.

## Primary references

- [NVIDIA DGX Spark playbooks](https://github.com/NVIDIA/dgx-spark-playbooks)
- [NVIDIA DGX Spark porting guide](https://docs.nvidia.com/dgx/dgx-spark-porting-guide/)
- [DeepSeek V4 Flash 0731](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731)
- [Mia DeepSeek V4 Flash two-Spark recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
- [DS4 on Spark](https://github.com/Entrpi/ds4-on-spark)
- [NVIDIA Nemotron](https://github.com/NVIDIA-NeMo/Nemotron)
- [Qwen Image](https://github.com/QwenLM/Qwen-Image)
- [TRELLIS.2](https://github.com/microsoft/TRELLIS.2)
- [Pixal3D](https://github.com/TencentARC/Pixal3D)
- [Step1X-3D](https://github.com/stepfun-ai/Step1X-3D)
- [SkinTokens](https://github.com/VAST-AI-Research/SkinTokens)
- [LTX-2](https://github.com/Lightricks/LTX-2)
- [MOVA](https://github.com/OpenMOSS/MOVA)
- [HunyuanVideo 1.5](https://github.com/Tencent-Hunyuan/HunyuanVideo-1.5)
- [HunyuanVideo-Foley](https://github.com/Tencent-Hunyuan/HunyuanVideo-Foley)
- [MiniMax H3](https://github.com/MiniMax-AI/MiniMax-H3)
