# Multi-Runtime Model Profiles Design

Date: 2026-08-02
Status: approved, including model-definition and cluster-profile amendments

## 2026-08-03 scope amendment

The active implementation and qualification pass is LLM-only: DeepSeek/Mia,
DS4 and a possible `bleysg` merge into the Entrpi DS4 branch, Nemotron,
Qwen3-VL, and Laguna S 2.1. The image and 3D families below remain part of
the broader approved catalog, but are deferred from the current serving pass.
TripoSG and TokenRig are not LLMs: they are image-to-3D and rigging systems,
so their isolated adapters remain fail-closed and are not active LLM endpoints.

## Purpose

Make every requested model runnable on the two-Vonk Forge-GPU node platform without forcing incompatible model families through one inference engine. DeepSeek Flash 0731 remains the default agent model. Delivery priority controls implementation order, not delivery scope or runtime routing; every model in this document is required.

The developer machine owns the creative pipeline and decides which model to call. The GPU nodes expose model capabilities and generated artifacts; they do not own the end-to-end asset workflow.

## Required model set

The concise [model capacity overview](../../model-capacity-overview.md) compares official releases, preferred GPU node-optimized paths, published memory evidence, placement, and validation status.

Delivery priority is the ordered enum `default`, `essential`, `recommended`,
or `secondary`. It determines implementation and qualification order only. It
does not make a model optional and is never used to route a live request.

| Model | Pipeline function | Delivery priority |
| --- | --- | --- |
| DeepSeek-V4-Flash-0731 | Default agent, reasoning, tool use, and pipeline control | `default` |
| Nemotron 3 Super 120B-A12B | Officially optimized alternative agent for reasoning, tools, and long-context work | `recommended` |
| Nemotron 3 Nano Omni 30B-A3B | Lightweight multimodal agent that can remain available on one GPU node while the other runs a creative model | `recommended` |
| Qwen-Image | Text-to-image concepts and clean reference images | `essential` |
| Qwen-Image-Edit-2511 | Alternate views, corrections, material edits, and texture-projection images | `essential` |
| Pixal3D | Primary high-fidelity image-to-3D geometry and PBR generation | `essential` |
| TRELLIS.2 4B | Alternative image-to-3D generation and Pixal3D foundation | `essential` |
| Qwen3-VL-8B-Instruct | Turntable evaluation, defect detection, prompt rewriting, and candidate ranking | `recommended` |
| Laguna S 2.1 | Agentic coding, long-horizon reasoning, and text-to-text tool use | `recommended` |
| SkinTokens / TokenRig | Skeleton and skin-weight generation | `recommended` |
| Step1X-3D | Independent geometry-plus-texture alternative | `secondary` |
| TripoSG | Fast draft and high-volume image-to-geometry generation | `secondary` |
| Hunyuan3D-Omni | Controlled 3D generation from image, point, voxel, bounding-box, or pose inputs | `secondary` |

## Decision

The platform exposes two configuration concepts:

| Concept | Responsibility |
| --- | --- |
| Model Definition | One atomic runnable variant: stable model identity, exact checkpoints and auxiliary artifacts, container or source build, optimized loader, loading and residency method, lifecycle commands, resource envelope, placement constraints, health and endpoint behavior, immutable pins, and qualification state. |
| Cluster Profile | The complete desired active state of both GPU nodes: zero or more Model Definitions assigned to each node, their start and stop order, and the stable capability aliases exposed to clients. |

The unit users activate is a **Cluster Profile**, not an individual model. A
Cluster Profile declares the complete set of Model Definitions that are
simultaneously active on GPU node 1 and GPU node 2. Individual model start and stop
switches are not part of the public control model because they could create an
unmeasured combination.

One model may have several Model Definitions. DeepSeek, for example, has
separate dual-GPU node and single-GPU node definitions, and may have official
correctness, Mia/vLLM, DS4, quantized, or low-memory candidates. A Model
Definition selects exactly one such runnable path. Its internal loader and
provisioning details are not additional user-facing activation layers and are
not interchangeable without creating and qualifying a new definition.

Model Definitions explicitly describe these independent dimensions:

| Dimension | Examples in this platform |
| --- | --- |
| Artifact acquisition | Complete Hugging Face snapshot, GGUF, auxiliary checkpoints, pinned container image, or pinned source build. |
| Loader/runtime | vLLM, DS4, SGLang Diffusion, Diffusers, or a model-specific native pipeline. |
| Loading/residency | Fully resident, persistent service, memory-mapped, staged sequential phases, accepted offload, NCCL tensor parallel, or experimental cross-host pipeline. |
| Placement | Either single GPU node, an exact GPU node, both GPU nodes exclusively, or an accepted shareable combination. |
| Maturity | `planned`, `prepared`, `verified`, `accepted`, or `rejected`. |

Model Definition maturity is stored in
`inventory/reports/model-definitions.json`. Records are keyed by a
content-addressed definition fingerprint and retain their transition history,
timestamp, evidence references, and rejection reason. The allowed progression
is `planned -> prepared -> verified -> accepted`; a `verified` candidate may
instead become `rejected`. `planned` means the definition is cataloged but its
artifacts or adapter may not be installed. `prepared` means immutable artifacts
are present. `verified` means offline integrity and runtime prerequisites pass.
`accepted` additionally requires the model-specific lifecycle, quality,
resource, and performance gates. Only accepted definitions may satisfy an
activatable Cluster Profile.

A rejected fingerprint remains visible but disabled and cannot be activated or
advertised. It may be reconsidered only if the rejection was erroneous and the
audit records the correction. Changed pins or behavior produce a new definition
fingerprint and candidate rather than mutating the rejected record.

Use a common Model Definition contract with runtime-specific adapters. The
controller treats lifecycle operations uniformly while each adapter preserves
the best loader and provisioning behavior for its model family.

Do not make ComfyUI, Diffusers, vLLM, or any other single runtime the platform-wide loader. ComfyUI may later run on the developer or external service host as a client, but it is not the source of truth for GPU node process lifecycle or Cluster Profile state.

## Initial cluster profiles and stable DeepSeek identity

The canonical default profile ID is `agent-full-dual`. The convenience selector
`default` resolves to that canonical ID but is not itself a profile ID. The
profile dedicates both GPU nodes to the accepted dual-GPU node DeepSeek Model
Definition. Other profiles normally keep the accepted single-GPU node DeepSeek
Model Definition active on GPU node 1 while GPU node 2 hosts an accepted combination
of creative Model Definitions.

The following are profile intents, not claims that every combination has
already passed admission:

| Cluster Profile | GPU node 1 Model Definitions | GPU node 2 Model Definitions | Stable aliases |
| --- | --- | --- | --- |
| `agent-full-dual` (canonical default) | DeepSeek dual rank 0 | DeepSeek dual rank 1 | `deepseek` → dual DeepSeek definition |
| `agent-long-dual` | DeepSeek long-context dual rank 0 | DeepSeek long-context dual rank 1 | `deepseek` → long-context dual DeepSeek definition |
| `creative-3d` | DeepSeek single | Pixal3D, TRELLIS.2, Qwen3-VL | `deepseek` → single DeepSeek; model-specific creative aliases |
| `image-authoring` | DeepSeek single | Qwen-Image, Qwen-Image-Edit-2511 | `deepseek` → single DeepSeek; model-specific image aliases |
| `rigging` | DeepSeek single | accepted TokenRig and supporting evaluation definitions | `deepseek` → single DeepSeek; model-specific rigging aliases |
| `agent-nemotron-super` | Nemotron 3 Super | idle | `nemotron-super` |
| `agent-nemotron-nano-omni` | Nemotron 3 Nano Omni | idle | `nemotron-nano-omni` |
| `geometry-step1x` | DeepSeek single | Step1X-3D | `deepseek` → single DeepSeek; `step1x-3d` |
| `geometry-triposg` | DeepSeek single | TripoSG | `deepseek` → single DeepSeek; `triposg` |
| `geometry-hunyuan3d-omni` | DeepSeek single | Hunyuan3D-Omni | `deepseek` → single DeepSeek; `hunyuan3d-omni` |

The canonical initial Model Definition IDs are:

| Model Definition ID | Intended runnable variant |
| --- | --- |
| `deepseek-agent-dual` | Mia/vLLM DeepSeek service spanning both GPU nodes |
| `deepseek-long-dual` | controlled million-token Mia/vLLM DeepSeek service spanning both GPU nodes |
| `deepseek-agent-single` | audited DS4 DeepSeek GGUF service on one GPU node |
| `nemotron-super-single` | accepted NVIDIA Nemotron 3 Super service on one GPU node |
| `nemotron-nano-omni-single` | accepted NVIDIA Nemotron 3 Nano Omni service on one GPU node |
| `qwen-image-single` | accepted Qwen-Image optimized service on one GPU node |
| `qwen-image-edit-2511-single` | accepted Qwen-Image-Edit-2511 optimized service on one GPU node |
| `pixal3d-single` | accepted Pixal3D service on one GPU node |
| `trellis2-4b-single` | accepted TRELLIS.2 4B service on one GPU node |
| `qwen3-vl-8b-single` | accepted Qwen3-VL-8B-Instruct service on one GPU node |
| `laguna-s21-single` | accepted Laguna S 2.1 NVFP4 service on one GPU node |
| `tokenrig-single` | accepted SkinTokens/TokenRig service on one GPU node |
| `step1x-3d-single` | accepted Step1X-3D service on one GPU node |
| `triposg-single` | accepted TripoSG service on one GPU node |
| `hunyuan3d-omni-single` | accepted Hunyuan3D-Omni service on one GPU node |

A logical ID identifies the intended runnable variant. Its immutable content
fingerprint identifies one exact set of pins and behavior. Cluster Profiles
resolve logical IDs through `locks/model-definitions.toml` and include the
resolved fingerprints in their own content hash.

Multiple Model Definitions in a profile are active simultaneously. Listing a
cached model or definition does not make it active. A multi-definition profile becomes
activatable only after the exact combined placement passes co-residency,
concurrent inference, thermal, output-quality, stop/restart, and memory-recovery
acceptance.

Both DeepSeek Model Definitions expose the client-facing OpenAI-compatible model name
`deepseek`. Clients do not select internal Model Definition variants such as
`single`, `dual`, `full`, `lite`, DS4, or Mia as OpenAI model names.
Descriptive Cluster Profile IDs such as `agent-full-dual` may contain those
terms. The active Cluster Profile chooses the backing definition. The
dual-GPU node definition is the faster default; the single-GPU node definition preserves DeepSeek availability while
freeing GPU node 2 for creative models. Status, diagnostic logs, and artifact
provenance still record the exact Model Definition, loader, checkpoint, and
Cluster Profile so the abstraction does not hide operational identity.

## Runtime adapter contract

The retained Spark-agent workload runtime owns the common acquisition and
materialization contract beneath these lifecycle operations. Every future
adapter receives only verified generation paths and capabilities from the
runtime engine. Resumable ranged transfer, durable progress, disk
reservation, restart recovery, cancellation, partial quarantine, digest/size
verification, atomic promotion, rollback generations, leases, repair, and
garbage collection are mandatory shared engine behavior and must not be
reimplemented by individual model adapters.

Every adapter implements these operations:

```text
prepare -> verify -> start -> health -> infer -> stop -> verify-release
```

| Operation | Required behavior |
| --- | --- |
| `prepare` | Download or synchronize pinned artifacts to local NVMe without starting inference. |
| `verify` | Validate source/image pins, model manifests, architecture, dependencies, free disk, and declared placement. |
| `start` | Start only the declared processes and mounts; distributed Model Definitions obey their rank order. |
| `health` | Prove model identity and runtime readiness, not only that a TCP port is open. |
| `infer` | Accept the Model Definition's declared request schema and write outputs to its declared local artifact path. |
| `stop` | Drain where a gateway exists, terminate within the Model Definition timeout, and retain diagnostic logs. |
| `verify-release` | Prove processes exited and available memory returned within the configured tolerance. |

The controller treats adapters uniformly but does not translate one model
family's internal launch commands into another's. Each adapter may remain
directly operable by a human over SSH for recovery and diagnosis, but
production preparation and lifecycle reconciliation use the outbound
agent/package path and never fall back to SSH.

## Loader and placement matrix

| Model Definition candidate | Preferred loader and precision | Placement | Residency |
| --- | --- | --- | --- |
| DeepSeek 0731 Mia service | Audited MiaAI-Lab/Anemll vLLM; BF16 model dtype, block-scaled FP8 E4M3 weights with UE8M0 scales, and padded `nvfp4_ds_mla` KV cache | both GPU nodes, TP=2 over NCCL | exclusive, persistent |
| DeepSeek 0731 DS4 GGUF | Audited DS4 v0.5.3 GB10/GPU node CUDA build with the Q2-imatrix base and DSpark drafter pair | one GPU node by default; optional two-GPU node TCP layer pipeline | mapped/registered no-copy |
| DeepSeek 0731 DS4 branch variant | The same DS4 model with `bleysg` DSpark work merged into the Entrpi branch; release and drafter pins change together | one GPU node initially | model-owned mapped/registered no-copy |
| Nemotron 3 Super 120B-A12B | NVIDIA DGX Spark vLLM NVFP4 playbook; TensorRT-LLM comparison definition | either single GPU node | persistent, single-exclusive initially |
| Nemotron 3 Nano Omni 30B-A3B | NVIDIA DGX Spark vLLM playbook with BF16 correctness and FP8/NVFP4 optimized definitions | either single GPU node | persistent; shareable only after combined-load tests |
| Qwen-Image | accepted ModelOpt NVFP4 SGLang DGX Spark path; official Diffusers as non-serving correctness oracle | either single GPU node | persistent, fully resident |
| Qwen-Image-Edit-2511 | accepted Nunchaku NVFP4 or ModelOpt FP8 DGX Spark path, selected by quality and performance; DiffSynth as oracle | either single GPU node | persistent, fully resident |
| Pixal3D | audited DGX Spark-native Pixal3D/TRELLIS.2 build with official fully resident or staged mode | either single GPU node | fully resident; official staged mode as fallback |
| TRELLIS.2 4B | audited CUDA 13/ARM64 DGX Spark build of the official Microsoft pipeline | either single GPU node | fully resident |
| Qwen3-VL-8B-Instruct | accepted GB10-native vLLM or SGLang build with optimized vision attention | either single GPU node | persistent server with paged KV cache |
| Laguna S 2.1 | official Laguna S 2.1 NVFP4 vLLM path; FP8 and forked loaders are separate comparison definitions | either single GPU node | persistent MoE service with bounded KV cache |
| SkinTokens / TokenRig | audited FP16 DGX Spark integration or GB10-native TokenRig build | either single GPU node | persistent Qwen3-0.6B plus FSQ-CVAE |
| Step1X-3D | GB10-native build of the official Step1X geometry and texture pipelines | either single GPU node | sequential stage residency |
| TripoSG | GB10-native build of the official TripoSG Diffusers pipeline | either single GPU node | persistent lightweight worker |
| Hunyuan3D-Omni | GB10-native official runtime with accepted FlashVDM acceleration | either single GPU node | persistent lightweight worker |

`either single GPU node` means the controller may place the Model Definition on GPU node 1
or GPU node 2 only when its complete verified cache and compatible image exist on
that node. It never migrates a live request.

## Loader-specific rules

### DeepSeek with Mia/vLLM

- Both nodes keep the complete verified Hugging Face snapshot on local NVMe.
- vLLM partitions runtime tensors across TP rank 0 and rank 1; the two nominal 128 GB unified-memory domains do not become one coherent 256 GB address space.
- Start GPU node 2's worker before GPU node 1's head; stop the head before the worker.
- Use explicit fabric interfaces, HCAs, GID indexes, offline cache mode, capacity limits, and pinned sampling presets.
- This is the default DeepSeek service because it uses the validated NCCL/RoCE fabric and supports concurrent API serving.

### DeepSeek with DS4

- Store the checked Q2-imatrix base GGUF and DSpark drafter on local NVMe and use DS4's `mmap` path.
- Production uses mapped/registered no-copy startup. Never set `DS4_CUDA_COPY_MODEL` or enable `DS4_MODEL_ANON_HUGE`; full-copy startup is prohibited. Set `DS4_NO_UPDATE_CHECK=1`.
- MXFP4 remains deferred until both loader support and measured one-GPU node admission exist. DS4 v0.5.3 rejects GGUF type 39, and the available 155,976,458,848-byte MXFP4 GGUF does not fit one GPU node's visible memory.
- Use one GPU node for the single-GPU node DeepSeek Model Definition. Treat DS4's documented two-host TCP layer pipeline as a separate experimental Model Definition: it divides layers and KV state but adds an inter-node hop to every decoded token and does not use NCCL tensor parallelism.
- SSD streaming paths documented for other backends are not assumed valid for GPU node CUDA.

### Nemotron

- Keep DeepSeek 0731 as the default agent; Nemotron Cluster Profiles are explicit alternatives rather than an automatic replacement.
- Start Nemotron 3 Super from NVIDIA's DGX Spark NVFP4 vLLM recipe. Pin its Marlin/CUTLASS MoE backend, FP8 KV-cache setting, MTP setting, reasoning parser, tool-call parser, context, and concurrency limits. TensorRT-LLM is a measured comparison definition, not an assumed upgrade.
- Start Nemotron 3 Nano Omni with the official NVIDIA DGX Spark vLLM recipe. Preserve BF16 as the semantic reference and evaluate the official FP8 and NVFP4 artifacts separately.
- Nano Omni is the first candidate for a lightweight resident agent beside a creative profile on the other GPU node. It still begins as `single-exclusive`; only a recorded exact-set co-residency test can make it `single-shareable` in a named Cluster Profile.
- Super and Nano expose OpenAI-compatible endpoints and receive the same pinned sampling, reasoning, tool-use, concurrency, and long-context admission controls as DeepSeek.

### Qwen image generation and editing

- Use official Diffusers output only as the correctness oracle.
- Serve Qwen-Image through the accepted ModelOpt NVFP4 SGLang DGX Spark path. Serve Qwen-Image-Edit through the accepted Nunchaku NVFP4 or ModelOpt FP8 path, selected by the cluster's quality, memory, and throughput results.
- Keep DiffSynth as the Qwen-Image-Edit-2511 compatibility reference and as an offload fallback. Its staged and disk-offload modes are not enabled merely because they use less CUDA allocator space.
- Cache-based denoising, quantization, Lightning/distilled checkpoints, or approximate step skipping require separate Model Definitions because they may change output quality.
- SGLang's documented multi-GPU diffusion modes do not establish two-host GPU node support. Cross-host execution remains disabled until a strict fabric-only acceptance test proves it; one GPU node has sufficient capacity for the requested image models.

### Pixal3D and TRELLIS.2

- Use each official repository and checkpoint as the acceptance oracle, while the deployed Model Definition uses an audited CUDA 13, ARM64, GB10 GPU node build.
- Run the standard fully resident path first. Pixal3D's official `--low_vram` mode may stage components by pipeline phase when allocator headroom or co-residency requires it.
- CPU offload does not create a second physical memory pool on GB10 unified memory. It may relieve CUDA allocator pressure, but total host memory remains the admission constraint.
- Multi-node code in these repositories is training/data-tooling support, not evidence of distributed inference. Initial inference is single-GPU node.
- Community ComfyUI and low-memory forks are candidates only after source, license, checkpoint, kernel, output-quality, and maintenance audits. They cannot replace the official baseline before comparison.

### Qwen3-VL

- Serve the 8B Instruct checkpoint through a persistent vLLM or SGLang endpoint rather than loading Transformers for every evaluation.
- Leave explicit space for image/video feature tensors when setting KV utilization and request concurrency.
- Pin processor settings, maximum pixels/frames, context, sampling, structured-output behavior, and the vision attention backend.

### Step1X-3D

- Preserve its two-stage geometry-then-texture flow and official model separation.
- Release or offload a completed stage before the next stage only when the accepted wrapper proves identical artifacts and improved peak memory.
- Published distributed features for training or rendering do not make inference a dual-GPU node Model Definition.

### TripoSG, TokenRig, and Hunyuan3D-Omni

- Use persistent single-GPU node workers to avoid reloading weights for each asset.
- TripoSG uses its official Diffusers pipeline and separately verified RMBG dependency.
- TokenRig loads both the autoregressive rigging checkpoint and the SkinTokens FSQ-CVAE; its output test must validate skeleton hierarchy and normalized skin weights, not only GLB syntax.
- Hunyuan3D-Omni serves through the accepted FlashVDM Model Definition. The official non-FlashVDM path remains its correctness oracle and diagnostic fallback.

## Optimized artifact policy and current survey

Every model keeps a non-serving correctness Model Definition based on its
official upstream checkpoint and runtime. A user-selectable Cluster Profile
always uses the best accepted Vonk Forge GPU node-optimized Model Definition available
for the exact model. An optimized checkpoint, quantization, kernel fork, or
GPU node-specific container remains quarantined only until it proves equivalent
enough for its declared use; after acceptance the applicable Cluster Profiles
select it. Reduced memory use alone is not sufficient.

If no exact GPU node path is available, implementation produces and benchmarks an ARM64/CUDA 13/GB10-native build of the official runtime. The model is not considered complete merely because a generic upstream command happens to run. The generic Model Definition remains available only for qualification, regression comparison, and recovery diagnostics.

Candidate status has four meanings:

1. **Official GPU node path:** NVIDIA or the model owner publishes a DGX Spark recipe or artifact for the exact model.
2. **GPU node community path:** a DGX Spark-specific integration exists, but source, image, checkpoint, licensing, and results require independent audit.
3. **Upstream optimization:** the exact model has an optimized artifact or mode, but two-GPU node or GB10 validation is not established.
4. **No exact GPU node path found:** the current primary-source survey found no maintained optimization for the exact requested model; this is not evidence that none can exist.

| Model | Best optimized candidate found on 2026-08-02 | Status and adoption rule |
| --- | --- | --- |
| DeepSeek-V4-Flash-0731 | Mia's dual-DGX Spark vLLM recipe with MTP and padded `nvfp4_ds_mla`; DS4 v0.5.3 Q2-imatrix plus DSpark pair; NVIDIA `DeepSeek-V4-Flash-NVFP4` | Mia remains the first dual-GPU node service candidate. The audited DS4 pair is the single-GPU node candidate, subject to runtime admission. MXFP4 remains deferred until DS4 has loader support and measured one-GPU node admission. |
| Nemotron 3 Super 120B-A12B | NVIDIA's exact NVFP4 checkpoint and DGX Spark vLLM/TensorRT-LLM playbook | Official GPU node path and preferred initial profile. Validate the pinned Marlin/CUTLASS, FP8 KV, MTP, reasoning, and tool settings on this cluster. |
| Nemotron 3 Nano Omni 30B-A3B | NVIDIA's DGX Spark vLLM BF16/FP8/NVFP4 recipes and exact FP8 artifact | Official GPU node path. BF16 is the semantic reference; FP8 and NVFP4 compete on quality, memory, and throughput. |
| Qwen-Image | `lmsys/qwen-image-modelopt-nvfp4-sglang` plus NVIDIA's published NVFP4-on-DGX Spark path | Official/upstream GPU node path. Compare against official BF16 Diffusers using fixed prompt, text-rendering, and identity fixtures before promotion. |
| Qwen-Image-Edit-2511 | Nunchaku SVDQuant W4A4/NVFP4 build reporting DGX Spark measurements; ModelOpt FP8 transformer; community FP8 checkpoint | Exact GPU node community and upstream optimized paths. Audit Nunchaku and compare edit fidelity and protected-region preservation against BF16 before promotion. |
| Pixal3D | Official `--low_vram` staging; Super-Idol-Master DGX Spark integration | Upstream optimization plus recent community GPU node integration. The official fully resident path remains the reference; the community ARM64 patches are audited independently. |
| TRELLIS.2 4B | `dgx-trellis2` and `Trellis2-DGX-Spark-Docker` | GPU node community paths. Use them as CUDA 13/ARM64 build references, not as trusted deployment pins, until reproducibility and output parity pass. |
| Qwen3-VL-8B-Instruct | vLLM/SGLang FlashAttention path; no exact official 8B NVFP4 GPU node artifact found | Upstream optimization. Do not substitute NVIDIA's different-size Qwen3-VL NVFP4 artifacts for the required 8B model. Benchmark BF16, FP8, and an audited weight-quantized 8B candidate if available. |
| SkinTokens / TokenRig | FP16 DGX Spark integration in Super-Idol-Master | GPU node community path. No dedicated exact-model optimized loader was found; audit the integration while retaining official TokenRig as the non-serving correctness oracle. |
| Step1X-3D | Official sequential geometry/texture loading and offload controls | No exact GPU node path found. Build the official runtime for ARM64/GB10 and measure phase release rather than assuming a community quantization. |
| TripoSG | Official lightweight Diffusers pipeline | No exact GPU node path found. Its published memory requirement already makes single-GPU node serving practical; produce and measure the GB10-native build before declaring its Model Definition accepted. |
| Hunyuan3D-Omni | Official FlashVDM mode | Upstream optimization. A DGX Spark container for Hunyuan3D 2.1 is useful as an ARM64 build reference but is not the requested Omni model and cannot replace it. |

Before an optimized Model Definition becomes selectable, its checked-in
evidence must include immutable source, container, checkpoint, and
quantization-recipe pins; proof of `aarch64` and GB10 `sm_121` compatibility;
offline startup; model-specific output comparison; memory and throughput
measurements; license and provenance review; and three clean lifecycle cycles.
A community claim or benchmark is discovery evidence, never an acceptance
result for this cluster. Once a definition passes, applicable Cluster Profiles
point to it by default; users do not have to opt into GPU node optimization
manually.

Production pins live in `locks/model-definitions.toml`. Each definition has a
corresponding decision record at
`docs/audits/<model-definition-id>.md` that explains the selected source commit,
container digest, checkpoints, processors, quantization recipe, generation
parameters, licenses, rejected candidates, and evidence references. Research
snapshot commits below remain discovery evidence and never override this lock
file implicitly.

The Mia-first audit selected
`b131b2a22164675890dd1465fd8862b5cfb6ff13` as the planned production
candidate, replacing the earlier provisional
`914c35bd7d5607560048e4467c3fdd42e892e297` configuration pin. The candidate is
still planned and not accepted: only exact runtime, checkpoint, and acceptance
evidence may advance its definition.

## Memory and residency policy

Each GPU node is marketed as a 128 GB unified-memory host, but the platform
inventory records `130,663,231,488` visible bytes, or about `121.69 GiB`, on
each node. Admission uses measured bytes from the inventory and never the
nominal capacity. Initial clean available memory was `126,990,147,584` bytes on
GPU node 1 and `126,946,283,520` bytes on GPU node 2. Reserving exactly `8 GiB`
(`8,589,934,592` bytes) for the operating system establishes the initial
per-node Model Definition budgets:

| Node | Visible total | Clean available baseline | OS reserve | Initial Model Definition budget |
| --- | ---: | ---: | ---: | ---: |
| GPU node 1 | 130,663,231,488 B | 126,990,147,584 B | 8,589,934,592 B | 118,400,212,992 B (110.27 GiB) |
| GPU node 2 | 130,663,231,488 B | 126,946,283,520 B | 8,589,934,592 B | 118,356,348,928 B (110.23 GiB) |

The source of truth is `inventory/reports/capacity.json`, which records the
boot ID, sample time, visible total, clean baseline, reserve, derived budget,
and measurement command. A fresh accepted baseline replaces these initial
values through an auditable report update; implementations do not invent a
round-number limit. The admission calculation is per node:

```text
resident weights
+ replicated encoders and processors
+ KV cache or diffusion/3D activations
+ CUDA graphs, kernels, and scratch space
+ container and operating-system headroom
<= measured per-node Model Definition budget
```

Model Definitions have one of these placement classes:

1. `dual-exclusive`: reserves both GPU nodes, such as the Mia DeepSeek definition.
2. `single-exclusive`: uses one GPU node and forbids other GPU Model Definitions there until measured otherwise.
3. `single-shareable`: may coexist only in explicitly accepted exact Model Definition sets.
4. `dual-pipeline-experimental`: uses both hosts through a non-NCCL model-specific pipeline, such as DS4 TCP.

Every Model Definition begins as exclusive on its selected node. Co-residency
is enabled only for an exact N-way set in a named Cluster Profile. Pairwise
acceptance does not imply three-way or larger acceptance. The checked-in record
is keyed by the Cluster Profile definition hash and sorted Model Definition
hashes. It contains the exact startup order, clean baseline, each standalone
peak, combined peak and peak delta, concurrent-inference result, sustained-run
and thermal results, stop/restart and memory-recovery results, and semantic
output-quality results for every definition in the set. Admission sums measured
peak deltas against the per-node budget; an unmeasured definition or exact set
remains exclusive and makes the target profile non-activatable.

## Distributed fabric admission

Every distributed Model Definition pins and verifies the accepted direct-fabric
state before startup. One physical QSFP cable connects one `200000 Mb/s` port
on each GPU node. The port is exposed through two Linux Ethernet/RoCE functions
because the NIC reaches the SoC over two PCIe Gen5 x4 links. The functions are
not independent 200 Gb/s physical rails and their duplicated link-rate reports
must not be added to claim 400 Gb/s.

The pinned state requires:

- physical link rate `200000 Mb/s`, net-device MTU `1500`, and RoCE path MTU
  `1024`;
- both inventory-pinned interface-to-HCA functions up and selected by NCCL;
- GID index `3` on both HCAs; and
- no default route on either fabric subnet.

NVIDIA Sync treats `184 Gb/s` as the lower bound for the 200 Gb/s physical-link
speed test. The equivalent command-line acceptance gate runs simultaneous RDMA
writes across both functions, sums only measurements from the same timed
interval, and requires at least `184 Gb/s` in each node-to-node direction. The
existing sequential single-function tests remain diagnostics and regression
checks; they do not prove physical-link bandwidth.

The strict gates are:

| Metric | Accepted evidence | Admission floor |
| --- | ---: | ---: |
| Physical negotiated link rate | 200 Gb/s | exactly 200 Gb/s |
| Simultaneous aggregate RDMA write, each direction | 185.14 Gb/s | at least 184 Gb/s |
| Single-function RDMA write diagnostic | worst result 108.88 Gb/s | at least 98.01 Gb/s per function and direction |
| Single-function RDMA read diagnostic | worst result 80.42 Gb/s | at least 72.37 Gb/s per function and direction |
| Fixed 8-byte write-latency p99 | 2.03–2.22 us by function/direction | no more than 125% of the corresponding accepted baseline |
| Monitored RDMA error-counter growth | zero | exactly zero |
| NCCL bus bandwidth | 19.308 GB/s | at least 17.44 GB/s |

The per-function and NCCL regression floors are 90% of the recorded accepted
results; the aggregate floor is NVIDIA's independent lower bound. The accepted
2026-08-02 run reached 185.14 Gb/s in each direction, established fixed
8-byte/10,000-iteration latency distributions for both functions and
directions, and observed no monitored error counter before or after traffic.
The fabric itself therefore no longer blocks a distributed Model Definition;
all definition-specific gates still apply. Subsequent admission requires p99
latency no more than 125% of the corresponding function/direction baseline.
The test command, payload, iterations, per-function results, aggregate, result
overlap interval, counter snapshots, and command evidence are recorded in
`inventory/reports/rdma-nccl.json`.

## Storage layout

Each GPU node uses local NVMe:

```text
/srv/models/
|-- snapshots/       immutable HF, safetensors, GGUF, and auxiliary checkpoints
|-- manifests/       revisions, filenames, sizes, hashes, and license metadata
|-- runtime-cache/   writable JIT, kernel, vLLM, SGLang, and framework caches
`-- outputs/         generated images, meshes, textures, rigs, and reports
```

- Snapshot mounts are read-only during serving where the runtime permits.
- Writable runtime caches and generated outputs use separate mounts.
- A Model Definition verifies every primary and auxiliary checkpoint before offline start.
- The NAS may archive or distribute artifacts later, but it is never the live checkpoint, JIT, KV, temporary-latent, or output-work path.
- A model needed on either GPU node is synchronized and verified independently on both nodes before it is declared portable.

## API and pipeline boundary

The developer machine orchestrates the asset pipeline. Caddy and the future control host advertise capabilities but do not compose the creative workflow.

The platform exposes two endpoint classes:

- OpenAI-compatible endpoints for DeepSeek, Nemotron, and Qwen3-VL, and for image runtimes only where the selected upstream provides a compatible API.
- Typed job endpoints for image generation, image editing, 3D generation, texturing, and rigging. These return a job identifier, status, runtime/model identity, pinned parameters, and artifact references.

DeepSeek clients always request the stable model name `deepseek`. Activating a
different Cluster Profile may change its internal single- or dual-GPU node Model
Definition, but does not change the client URL, authentication, request schema,
or model name. A profile switch can still require a bounded maintenance window
when the old and new definitions cannot coexist; stable naming prevents
client reconfiguration but does not falsely promise spare-compute failover.

Generated artifacts remain on GPU node-local output storage during initial testing and are retrieved through SSH. The later gateway may provide an authenticated artifact route after size, timeout, and NAS-transfer behavior are measured.

## Switching and failure behavior

Until the external control host exists, `vonkctl` runs on the developer
machine. It stores atomic controller state at `.state/vonkctl/state.json` and
serializes transitions with `.state/vonkctl/switch.lock`. The lock records the
PID, host identity, and timestamp. A stale-lock override is explicit and is
refused when the PID is live or the lock is younger than the configured
threshold. Moving this controller to the future control host preserves the same
state and locking contract.

Before a switch, the controller checks the target's placement, declared
conflicts, content-pinned cache manifest, Model Definition maturity, adapter
availability, free memory, free disk, boot IDs, runtime image, and required
fabric state. A distributed target additionally requires the strict fabric
test state and latency baseline.

Profile activation is the only state-changing public operation. The controller
may retain an unchanged Model Definition, such as the same accepted
single-GPU node DeepSeek definition on GPU node 1, only when its definition hash,
health, endpoint, and placement are identical in both profiles. Changed
definitions follow their
declared stop and start order. Convenience commands that mention a model must
resolve to an accepted named Cluster Profile; they never toggle that model into
the current state independently.

A failed start or failed quality gate leaves every process started for the
target stopped. It does not delete snapshots, runtime caches, inputs, outputs,
or diagnostic logs. The prior heavyweight profile is not automatically
restarted.

Temporary use may request an explicit restoration target, for example:

```text
vonkctl profile activate creative-3d --restore agent-full-dual
```

`--restore` stores the canonical accepted Cluster Profile ID and is not a
profile-level `restore_home` property. Restoration is a separate, fully gated
profile transition and begins only after the temporary activation and requested
job complete successfully and its outputs have been recovered. If activation,
inference, quality validation, or output recovery fails, the controller fails
to stopped and does not restart the restoration target automatically.

Distributed and GPU-heavy Cluster Profiles never auto-start after reboot.
Lightweight Cluster Profiles may gain auto-start only through a later explicit design
change.

## Acceptance requirements

Every required model receives at least one Model Definition and an initial
Cluster Profile intent. Every accepted Model Definition receives:

1. pinned source, image, checkpoint, processors, auxiliary models, and generation parameters;
2. a successful native ARM64/GB10 container build or an audited compatible image;
3. offline cache verification;
4. cold-start time, warm-start time, clean memory baseline, peak memory, recovered memory, disk use, and thermal measurements;
5. a deterministic or seed-controlled fixture where the runtime supports it;
6. model-specific semantic output validation;
7. three start-infer-stop cycles with no orphan process or material memory drift; and
8. direct SSH-tunnel validation before any Caddy advertisement.

Model-specific minimums are:

| Model family | Required semantic result |
| --- | --- |
| DeepSeek | correct language, reasoning/tool behavior, no repetition/XML leakage, declared concurrency behavior |
| Nemotron Super / Nano Omni | correct reasoning mode, tool calls, declared multimodal behavior, context and concurrency limits, and no parser leakage |
| Qwen-Image | valid image dimensions plus prompt/content and text-rendering fixture checks |
| Qwen-Image-Edit | instructed edit occurs while protected identity/regions remain within the fixture tolerance |
| Pixal3D / TRELLIS.2 / Step1X-3D / TripoSG / Hunyuan3D-Omni | valid nonempty geometry; declared texture/PBR channels where supported; rendered turntable acceptance |
| Qwen3-VL | expected defect classification, ranking, and structured response for pinned turntable fixtures |
| TokenRig | nonempty acyclic skeleton, valid joint references, normalized bounded skin weights, and loadable rigged artifact |

Availability means every required model has at least one accepted Model
Definition in an accepted Cluster Profile, and that profile can be selected,
started, used, stopped, and reselected reproducibly. The profile inventory test
fails if any required model lacks a profile intent, even before its definition
has reached acceptance. Availability does not mean every known definition
remains resident simultaneously.

## Research snapshot

The design was checked on 2026-08-02 against these upstream source snapshots. They are research inputs, not deployment pins; implementation audits resolve immutable production pins and image digests.

| Project | Reviewed commit |
| --- | --- |
| DS4 | v0.5.3, peeled commit `4ad370b4a338efe9723a386673c0e04f6e214108`; see the immutable DS4 audit |
| MiaAI-Lab dual DGX Spark | `b131b2a22164675890dd1465fd8862b5cfb6ff13` |
| Qwen-Image | `6b5e1f5cec987d404be5ac6657db3b9aacb56a89` |
| SGLang | `8d106c3d79ef885f2fc0684f1915ebc404acfbe8` |
| DiffSynth-Studio | `6e2b14bc73ff317229b2a28487fe09250bbf463f` |
| Pixal3D | `cdbb2bbffbf4e6f298b5f2af3d1d76a8d823d2af` |
| TRELLIS.2 | `75fbf0183001ed9876c8dbb35de6b68552ee08bd` |
| SkinTokens | `273b691d35989d71cd17ff2895fdc735097b92d1` |
| Step1X-3D | `cb5ac944709c6c913109070c7b90c3447f57f3d4` |
| TripoSG | `fc5c40990181e2a756c4e0b1c2f4d6b5202faf8c` |
| Hunyuan3D-Omni | `4d47c0cc2bd0c4281963a7314ab330a5af36bfa8` |

## References

- [DS4 audited source](https://github.com/Entrpi/ds4)
- [MiaAI-Lab DeepSeek dual-DGX Spark recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark)
- [NVIDIA DeepSeek-V4-Flash NVFP4](https://huggingface.co/nvidia/DeepSeek-V4-Flash-NVFP4)
- [DS4 on Spark](https://github.com/Entrpi/ds4-on-spark)
- [NVIDIA Nemotron DGX Spark playbook](https://build.nvidia.com/spark/nemotron)
- [NVIDIA vLLM DGX Spark playbook](https://build.nvidia.com/spark/vllm/instructions)
- [NVIDIA Nemotron 3 Super 120B-A12B NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4)
- [NVIDIA Nemotron 3 Nano Omni 30B-A3B Reasoning FP8](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-FP8)
- [Qwen-Image](https://github.com/QwenLM/Qwen-Image)
- [Qwen-Image ModelOpt NVFP4 for SGLang](https://huggingface.co/lmsys/qwen-image-modelopt-nvfp4-sglang)
- [NVIDIA: NVFP4 Qwen-Image on DGX Spark](https://blogs.nvidia.com/blog/dgx-spark-and-station-open-source-frontier-models/)
- [Nunchaku Qwen-Image-Edit-2511](https://huggingface.co/stuqiu/nunchaku-qwen-image-edit-2511)
- [SGLang Diffusion](https://docs.sglang.io/docs/sglang-diffusion)
- [DiffSynth-Studio](https://github.com/modelscope/DiffSynth-Studio)
- [Pixal3D](https://github.com/TencentARC/Pixal3D)
- [TRELLIS.2](https://github.com/microsoft/TRELLIS.2)
- [NVIDIA forum: TRELLIS.2 on DGX Spark](https://forums.developer.nvidia.com/t/trellis-2-on-dgx-spark/355816)
- [dgx-trellis2](https://github.com/raziel2001au/dgx-trellis2)
- [TRELLIS.2 DGX Spark Docker](https://github.com/dr-vij/Trellis2-DGX-Spark-Docker)
- [Super-Idol-Master DGX Spark integration](https://github.com/SidneyArt/Super-Idol-Master)
- [Qwen3-VL-8B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct)
- [SkinTokens / TokenRig](https://github.com/VAST-AI-Research/SkinTokens)
- [Step1X-3D](https://github.com/stepfun-ai/Step1X-3D)
- [TripoSG](https://github.com/VAST-AI-Research/TripoSG)
- [Hunyuan3D-Omni](https://github.com/Tencent-Hunyuan/Hunyuan3D-Omni)
- [Hunyuan3D 2.1 DGX Spark Docker build reference](https://github.com/dr-vij/Hunyuan3D-2.1-DGX-Spark-Docker)
- [NVIDIA DGX Spark NGC best practices](https://docs.nvidia.com/dgx/dgx-spark/ngc.html)
- [NVIDIA DGX Spark clustering](https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html)
- [NVIDIA DGX Spark playbooks](https://github.com/NVIDIA/dgx-spark-playbooks)
