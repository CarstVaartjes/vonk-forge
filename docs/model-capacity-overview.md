# Vonk Forge GPU node Model Capacity Overview

Last researched: 2026-08-16

## At a glance

- `DeepSeek-V4-Flash-0731` is the only required model whose preferred service must span both GPU nodes.
- Nemotron 3 Super is also large, but NVIDIA's NVFP4 release is explicitly designed to run on one DGX Spark.
- Every other required model is expected to run on one GPU node individually.
- Fitting individually does not imply that arbitrary models may remain loaded together. Every recipe starts as exclusive and becomes shareable only after its exact N-way placement passes co-residency acceptance.
- The user-facing Library selects an accepted immutable recipe revision and mapping. There is no separate cluster-profile authority or hidden model fallback.
- The active physical qualification pass currently covers the two DeepSeek recipes. Other model and recipe candidates remain in the canonical recipe library until their exact immutable definitions pass acceptance; they are not presented as runnable defaults.

Each GPU node is marketed as having 128 GB of unified memory shared by the
operating system, CPU, and GPU. The cluster inventory exposes
`130,663,231,488` bytes, or about `121.69 GiB`, on each node. Admission uses the
measured per-node budgets—initially 110.27 GiB on GPU node 1 and 110.23 GiB on
GPU node 2 after an 8 GiB OS reserve—not the nominal capacity. Published VRAM
requirements and checkpoint sizes therefore indicate feasibility, not the
final admission limit. Cluster acceptance records measured resident weights,
activations or KV cache, runtime workspaces, operating-system headroom, and
recovered memory after shutdown.

## Official model versus GPU node-optimized recipe target

| Required model | Official release and published capacity evidence | Preferred Vonk Forge GPU node-optimized path | Placement | Initial residency | Evidence status |
| --- | --- | --- | --- | --- | --- |
| DeepSeek-V4-Flash-0731 | The [official checkpoint](https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash-0731) snapshot is about 155.44 GiB of weights, before KV cache and runtime workspaces. | The default is the audited [MiaAI-Lab dual-Spark recipe](https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-DSpark-2x-DGX-Spark). The accepted single-Spark alternative is DS4 v0.5.3 with the Q2-imatrix base plus DSpark drafter pair (93,691,352,992 bytes). MXFP4 remains deferred. | Both Sparks for the default service; one Spark for the DS4 alternative | `distributed` for Mia; `single` for DS4 | Both definitions are represented by exact v1 recipes; physical evidence remains definition-specific. |
| Nemotron 3 Super 120B-A12B | 120B total/12B active; the NVFP4 repository is about 80 GB and lists one DGX Spark as the minimum. | NVIDIA's exact [Nemotron 3 Super NVFP4](https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-NVFP4) with the official [DGX Spark Nemotron playbook](https://build.nvidia.com/spark/nemotron); compare vLLM and TensorRT-LLM. | One GPU node | `single-exclusive` | Official GPU node path; cluster context, KV, MTP, and throughput acceptance pending. |
| Nemotron 3 Nano Omni 30B-A3B | NVIDIA publishes BF16 at 62 GB, FP8 at 33 GB, and NVFP4 at 21 GB; one GPU node is supported. | Official [Nano Omni NVFP4](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4) through NVIDIA's [DGX Spark vLLM playbook](https://build.nvidia.com/spark/vllm/instructions); FP8 remains a measured comparison definition. | One GPU node | `single-exclusive`, then co-residency candidate | Official GPU node path; multimodal and exact-set acceptance pending. |
| Qwen-Image | Official [Qwen-Image](https://github.com/QwenLM/Qwen-Image) is a 20B MMDiT model. The generic peak-memory requirement is not the GPU node admission result. | [ModelOpt NVFP4 for SGLang](https://huggingface.co/lmsys/qwen-image-modelopt-nvfp4-sglang), served persistently through a GB10-native SGLang Diffusion build. | One GPU node | `single-exclusive` initially | Blackwell-optimized artifact exists; Vonk Forge GPU node build, output parity, and peak memory require cluster acceptance. |
| Qwen-Image-Edit-2511 | The official [Qwen-Image-Edit-2511](https://huggingface.co/Qwen/Qwen-Image-Edit-2511) release is a 20B image-edit model. | Select between [Nunchaku SVDQuant W4A4/NVFP4](https://huggingface.co/stuqiu/nunchaku-qwen-image-edit-2511) and [ModelOpt NVFP4 for SGLang](https://huggingface.co/lmsys/qwen-image-edit-2511-modelopt-nvfp4-sglang) using protected-region quality, memory, and speed results. | One GPU node | `single-exclusive` initially | Exact optimized candidates exist; provenance, GB10 reproducibility, and edit-quality acceptance pending. |
| Pixal3D | Official [Pixal3D](https://github.com/TencentARC/Pixal3D) provides 1536 standard and 1024 low-memory modes but no final GPU node peak figure. | Audited CUDA 13/ARM64/GB10 build of the official pipeline, using the [Super-Idol-Master](https://github.com/SidneyArt/Super-Idol-Master) integration only as a GPU node build reference. | One GPU node | `single-exclusive` initially | Expected to fit; custom-kernel build and measured standard/low-memory peaks pending. |
| TRELLIS.2 4B | Official [TRELLIS.2](https://github.com/microsoft/TRELLIS.2) requires at least 24 GB of GPU memory. | Audited GPU node build informed by [dgx-trellis2](https://github.com/raziel2001au/dgx-trellis2) and [Trellis2-DGX-Spark-Docker](https://github.com/dr-vij/Trellis2-DGX-Spark-Docker). | One GPU node | `single-exclusive` initially | Official model fits by published requirement; community GPU node builds require audit and output-parity validation. |
| Qwen3-VL-8B-Instruct | Official [Qwen3-VL-8B-Instruct](https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct) is an 8B multimodal model and recommends FlashAttention 2 for speed and memory savings. | GB10-native vLLM or SGLang service with optimized vision attention; no exact official 8B NVFP4 GPU node artifact was found in the current survey. | One GPU node | `single-exclusive`, then co-residency candidate | Capacity is comfortable; exact runtime, vision limits, KV budget, and quantized lane require measurement. |
| Laguna S 2.1 | Official [Laguna S 2.1](https://huggingface.co/poolside/Laguna-S-2.1) is a text-to-text 118B MoE with ~8B active parameters/token, 256 routed experts, and a 1,048,576-token context window. | Start with the official [Laguna S 2.1 NVFP4](https://huggingface.co/poolside/Laguna-S-2.1-NVFP4) vLLM path; compare FP8 and other loaders only as separate definitions. | One GPU node | `single-exclusive` initially | Newly cataloged as `laguna-s21-single`; GPU node-native image, memory envelope, quality, and lifecycle qualification pending. |
| SkinTokens / TokenRig | Official [SkinTokens](https://github.com/VAST-AI-Research/SkinTokens) requires at least 14 GB for inference and uses a Qwen3-0.6B backbone plus FSQ-CVAE. | Audited FP16 GPU node integration or GB10-native official build; Super-Idol-Master is a build reference, not a deployment pin. | One GPU node | `single-exclusive`, then co-residency candidate | Published requirement fits comfortably; ARM64 dependencies and rig-quality acceptance pending. |
| Step1X-3D | Official [Step1X-3D](https://github.com/stepfun-ai/Step1X-3D) reports 27 GB for geometry plus texture and 29 GB for the label-conditioned variant. | GB10-native build of the official sequential geometry and texture pipelines, releasing each completed stage when parity tests permit. | One GPU node | `single-exclusive` initially | Official requirement fits; no exact maintained GPU node-specific optimization was found. |
| TripoSG | Official [TripoSG](https://github.com/VAST-AI-Research/TripoSG) requires at least 8 GB. | GB10-native build of the official Diffusers pipeline with its separately verified RMBG dependency. | One GPU node | `single-exclusive`, then co-residency candidate | Official requirement fits comfortably; no exact maintained GPU node-specific optimization was found. |
| Hunyuan3D-Omni | Official [Hunyuan3D-Omni](https://github.com/Tencent-Hunyuan/Hunyuan3D-Omni) reports 10 GB for generation. | GB10-native official runtime with its `--flashvdm` acceleration enabled after output comparison. A Hunyuan3D 2.1 GPU node container is only a build reference because it is not Omni. | One GPU node | `single-exclusive`, then co-residency candidate | Official requirement fits comfortably; FlashVDM build and controlled-input acceptance pending. |

The current DeepSeek operational evidence is recorded in the
[`Mia dual-GPU node`](../inventory/reports/deepseek-mia-operational.json) and
[`DS4 single-GPU node`](../inventory/reports/deepseek-ds4-operational.json)
reports. DS4 measured a 67-second cold start, 110.86 GiB of consumed available
memory after its live gate run, a 32,768-token context, and an 8 GiB disk-KV
budget on GPU node 1.
`verified` is definition-specific: the DS4 single-GPU node definition requires its
offline artifact, image, architecture, identity, mapped/no-copy, reasoning, and
cache gates. Mia has additionally passed its sustained thermal, repeated
lifecycle, reboot/no-autostart, and canonical performance gates and is now
`accepted`. The `bleysg` DSpark work is tracked as a possible merge into the
DS4 branch; a merged release keeps the same DeepSeek model identity but gets a
new immutable runtime fingerprint and must repeat the DS4 gates. Laguna and the
remaining creative definitions are planned candidates.

The reproducible DS4 development lane, including its stricter 120 GB available
memory admission floor and two-rank replicated failure/recovery exercise, is
documented in [Development agent workload
acceptance](runbooks/development-agent-workloads.md). That lane validates the
development control path; it does not change the production maturity labels in
this table until the physical evidence is accepted.

## Placement interpretation

```text
Dual-GPU node default agent:
  GPU node 1 [DeepSeek TP rank 0] <== NCCL/RoCE ==> GPU node 2 [DeepSeek TP rank 1]

Single-Spark recipe or mixed assignments:
  Spark 1 [one or more exact recipe runs]      Spark 2 [one or more exact recipe runs]
          ^ exact accepted set                         ^ exact accepted set
```

The initial mixed arrangement keeps an exact single-Spark DeepSeek recipe on one
Spark while the other runs another exact accepted recipe. Nemotron Nano Omni is
another future lightweight resident candidate. These are target configurations,
not accepted combinations. Each
exact N-way set must pass combined startup, peak-memory, concurrent and
sustained inference, thermal, output-quality, memory-recovery, and clean-
shutdown tests before the controller permits it. Pairwise evidence never
authorizes a larger set.

## What “fits” does and does not mean

`Fits on one GPU node` means the official evidence and model scale are below one
GPU node's measured usable-memory budget with a credible runtime path. It does
not yet mean:

- the ARM64/CUDA 13/GB10 runtime builds without patches;
- the optimized checkpoint preserves required output quality;
- maximum resolution or context fits at useful concurrency;
- the recipe can coexist with another resident recipe; or
- every official and optimized checkpoint copy fits on local NVMe simultaneously.

Disk capacity is tracked separately. Before downloading the catalog, implementation records each immutable snapshot, auxiliary model, container image, and writable cache size, then compares the complete manifest with free local NVMe on both GPU nodes. Correctness and optimized checkpoint variants are not assumed to be deduplicated.

## Related design documents

- [Dual Vonk Forge GPU node architecture](architecture-overview.md)
- [Model catalog](operators/model-catalog.md)
- [Dual Vonk Forge GPU node platform design](superpowers/specs/2026-08-01-dual-vonk-node-platform-design.md)
- [Execution harness operations](operators/execution-harnesses.md)
