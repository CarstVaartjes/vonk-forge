# Recipe qualification decision matrix

This matrix is the operational decision record for the recipe-library checkout
used by the 2026-08-30 two-Spark qualification campaign. It covers every exact
recipe slug present in `recipes/` at the snapshot recorded below, including
recipes whose declared topology cannot run on the present fleet.

The words **executable Candidate** and **accepted** are deliberately not
interchangeable:

- **Executable Candidate (EC)** means the immutable recipe has a complete
  install/run contract and is eligible for structural validation and a canary.
  It does not prove that an image built, weights installed, a process became
  ready, or an inference/job completed on physical hardware.
- **Physically accepted** requires the controller-recorded acceptance evidence
  in this runbook for the exact recipe content digest, model revision, runtime
  distribution, agent release, and Spark topology. At this snapshot, **none of
  the rows is physically accepted**.

The present fleet consists of two enrolled DGX Sparks with about 121.7 GiB of
unified memory each (about 118.2 GiB observed free before recipe installation).
All node actions, including agent upgrades, use the authenticated controller
relay. SSH is not part of qualification or recovery.

## Inventory snapshot

The settled recipe-library checkout contains **76** `recipes/*.json` documents
and **236** catalog entities. This matrix is bound to recipe-library commit
`745a42b5daa3ac8010483421c45235e32e866672` and the generated
`catalog-index.json` SHA-256
`e864b644e374c76f594bcc4a394348844d4e5aa8d7dc78142f7d596b1fc2b55e`.

The repository gates are green for this snapshot:

- `tools/build-catalog-index` and `tools/build-catalog-index --check` passed;
- the cross-repository validator passed all 76 recipes, all 236 entities, and
  the secret scan;
- the focused fixture, campaign, and cross-repository validator suite passed 38
  tests; and
- the portable cluster/control suite passed 201 tests and 45 subtests, while
  Linux-only package and lifecycle gates remain delegated to Linux CI.

The checked-in runner registry contains 19 provenance-bound fixture records,
42 exact artifact contracts with 56 explicit smoke cases, 30 digest-bound
service contracts, and zero special/unresolved fixture dispositions. Together
those are all **72** exact
one- or two-Spark recipe contracts. Exactly four catalog recipes are omitted
from execution because their declared topologies require three, four, four,
and eight Sparks. Repository green means installable and directly qualifiable;
it is not physical acceptance. Hunyuan3D-Omni remains separately blocked by
its territory license in the Netherlands.

## Status and disposition vocabulary

The static/install column reports repository evidence, not physical evidence:

- **EC, repository-green**: the executable Candidate contract passed the
  catalog, library, cross-repository, secret, and platform gates above.
- **EC, focused fix**: a concrete static/runtime defect found by the audit was
  fixed and covered by focused tests in addition to the green broad gates.
- **Artifact lane green**: the bounded `recipe.job.run.v1` protocol, durable
  controller state, mTLS input/output transport, CLI/UI activation, output
  validation, cancellation, and retention are implemented and tested.
- **Build-unvalidated**: the recipe is structurally installable but its exact
  native image has not yet been built on Linux/arm64.

The final column is a scheduling decision for the current fleet:

- **qualify**: relevant and topology-compatible; enter the physical canary
  queue with any named gated-access or safe-capacity preflight.
- **superseded**: retain as historical/compatibility evidence, but qualify the
  named newer or safer variant first.
- **legal-blocked**: do not install or run in the fleet's jurisdiction.
- **unsupported topology**: requires more than the two available Sparks.

## Desired resident end state

The campaign does not uninstall successful recipes by default. It stops the
active runtime to release unified memory while retaining the installation,
downloaded weights, image, and reusable caches. It then proves warm-cache
redeploy by starting the same installation without a second install operation,
repeating the exact digest-bound smoke, and stopping it again.

Immediately before each new install, the runner refreshes controller telemetry
and proves that every target has room for the conservative artifact/install and
temporary-build envelope. If capacity cannot be proved, the recipe is blocked
before installation. `automatic_eviction` is always false: the runner never
silently removes another resident model. Once space is made through a separate,
reviewed controller plan, the campaign resumes idempotently.

The desired final state is therefore: every feasible one- and two-Spark recipe
remains installed, model-resident, and directly deployable whenever disk permits;
only runtime memory is released between canaries. The final
`run.residency-inventoried` record binds retained installation IDs, exact
revisions, operational state, node readiness, and the final fleet digest. Legal,
gated-access, build-validation, and greater-than-two-Spark exceptions remain
explicit and are never bypassed to reach that state.

## Language and multimodal service recipes

| Exact recipe slug | Topology | Currentness / variant role | Static / install status | Known blocker or fix | Physical canary required | Present two-Spark disposition |
|---|---:|---|---|---|---|---|
| `deepseek-v4-flash-0731-ds4-dspark-latency-single` | 1 Spark | Current latency/speculative DS4 profile | EC, repository-green | Guarded DSpark drafter path; prove fallback and speculative tokens on hardware | Yes, 1 Spark | qualify |
| `deepseek-v4-flash-0731-ds4-single` | 1 Spark | Current target-only, two-session concurrency baseline | EC, focused fix | Release wording now reflects ordered CUDA fallback, not native batching | Yes, 1 Spark | qualify |
| `deepseek-v4-flash-0731-mia-dual` | 2 Sparks | Current Mia distributed reference | EC, repository-green | Exact two-rank start/readiness, endpoint ownership, and rank-loss recovery remain unproved | Yes, exactly 2 Sparks | qualify |
| `deepseek-v4-flash-0731-mia-sparkinfer-single` | 1 Spark | Mia checkpoint through SparkInfer/EXL3 | EC, repository-green | High 116 GB steady envelope; verify admission headroom and speculative decode | Yes, 1 Spark | qualify |
| `deepseek-v4-flash-0731-sparkinfer-single` | 1 Spark | REAP-K216 SparkInfer alternative | EC, repository-green; capacity-blocked | High 116 GB steady envelope exceeds the checked lower-node free-memory authority by 1,053,716,480 bytes | No on current free memory | capacity-blocked |
| `gemma-4-26b-a4b-vllm-single` | 1 Spark | Current Gemma 4 plain-chat profile | EC, focused contract fix | Remains on vLLM 0.27.1; unsupported tool-use claims/flags were removed and thinking is disabled. Prove plain chat and absence of raw channel leakage | Yes, 1 Spark | qualify |
| `gemma-4-26b-a4b-vllm028-single` | 1 Spark | Current stable-vLLM Gemma 4 profile | EC, repository-green, new candidate | vLLM 0.28 uses its own exact alias and retains the Gemma 4 reasoning/parser and multimodal smoke contract; qualify beside the 0.27.1 compatibility control | Yes, 1 Spark | qualify |
| `glm-5-2-aqlm-vllm-triple` | 3 Sparks | Historical GLM 5.2 TP3/AQLM reference | EC, topology-inadmissible | No three-node placement exists on the present fleet | Yes on 3 Sparks, not now | unsupported topology |
| `glm-5-2-quanttrio-vllm-four` | 4 Sparks | Explicitly historical and superseded | EC, topology-inadmissible | Four-node topology; GLM 5.3 two-Spark profiles are newer | No; qualify GLM 5.3 TP2 | superseded |
| `glm-5-3-flash-nvfp4-kv-1m-abliterated-vllm-dual` | 2 Sparks | Current gated 1M-context abliterated specialist | EC, repository-green, gated | Gated weights; prove native MP startup, 1M profile, MTP4, and 120 GB/node safety | Yes, exactly 2 Sparks | qualify |
| `glm-5-3-flash-nvfp4-vllm-dual` | 2 Sparks | Current standard GLM 5.3 Ray TP2 profile | EC, focused static audit | Prove Ray formation, sparse MLA build, MTP4, multimodal input, and rank recovery | Yes, exactly 2 Sparks | qualify |
| `glm-5-3-flash-exl3-dflash2-vllm-dual` | 2 Sparks | Current Mia EXL3/DFlash2 TP2 profile | EC, repository-green, new candidate | Prove native-MP two-rank formation, EXL3 target, DFlash2 K7 decoding, 1M context, multimodal requests, and rank recovery | Yes, exactly 2 Sparks | qualify |
| `glm-5-3-flash-nvfp4-vllm-four` | 4 Sparks | Future-only TP4 counterpart; redundant on this fleet | EC, topology-inadmissible | Four-node topology offers no present-fleet qualification path | No on present fleet | unsupported topology |
| `inkling-975b-a41b-nvfp4-sglang-eight` | 8 Sparks | Future-only full Inkling profile | EC, topology-inadmissible | Requires eight Sparks; cannot be reduced without changing model/runtime semantics | No on present fleet | unsupported topology |
| `inkling-small-nvfp4-sglang-dual` | 2 Sparks | Current smaller Inkling multimodal TP2 profile | EC, repository-green | Prove SGLang two-rank formation, multimodal requests, tool calls, and recovery | Yes, exactly 2 Sparks | qualify |
| `laguna-s-2-1-nvfp4-vllm-single` | 1 Spark | Obsolete large Laguna S checkpoint/profile | EC, but unsafe current envelope; capacity-blocked | Pinned checkpoint is stale and the checked fleet memory envelope is 3,053,716,480 bytes short | No; qualify XS first | capacity-blocked |
| `laguna-xs-2-1-nvfp4-vllm-single` | 1 Spark | Current safer Laguna XS baseline | EC, repository-green | Prove model parser/tool path and memory envelope | Yes, 1 Spark | qualify |
| `lfm2-5-vl-3b-vllm-single` | 1 Spark | Current small vision-language/OCR baseline | EC, repository-green | Retains its specialized vLLM 0.27.1 runtime; prove multimodal preprocessing, OCR/layout, grounding, and parsed tool calls | Yes, 1 Spark | qualify |
| `lfm2-5-vl-3b-vllm028-single` | 1 Spark | Current stable-vLLM LFM 2.5 VL profile | EC, repository-green, new candidate | vLLM 0.28 uses an exact distinct alias while preserving OCR, grounding, multimodal preprocessing, and parsed-tool smoke coverage | Yes, 1 Spark | qualify |
| `ling-3-0-flash-dspark-sglang-single` | 1 Spark | Current Ling INT4 DSpark/SGLang profile | EC, focused fix; capacity-blocked | Exact DSpark dependency and immutable ledger revision corrected, but the checked fleet memory envelope is 1,053,716,480 bytes short | No on current free memory | capacity-blocked |
| `muse-glimmer-30b-bf16-vllm-single` | 1 Spark | Current multimodal/agentic specialist | EC, repository-green, upstream-pending tag | Uses its official Muse-specific CUDA 13 vLLM image; prove image, reasoning, and tool behavior on GB10 | Yes, 1 Spark | qualify |
| `nemotron-3-5-lightning-30b-a3b-vllm-dspark-latency-single` | 1 Spark | Current speculative low-concurrency latency profile | EC, focused harness fix; capacity-blocked | Typed attention/chunked-prefill/media options landed; the checked fleet memory envelope is 1,053,716,480 bytes short | No on current free memory | capacity-blocked |
| `nemotron-3-5-lightning-30b-a3b-vllm-single` | 1 Spark | Current target-only Lightning baseline | EC, focused harness fix | Use as control for DSpark latency delta; verify tool and reasoning parsers | Yes, 1 Spark | qualify |
| `nemotron-3-nano-30b-a3b-vllm-single` | 1 Spark | Older compact text baseline | EC, repository-green | vLLM 0.20 runtime is older; retain as compatibility control | Yes only after 3.5 baseline | superseded |
| `nemotron-3-nano-omni-30b-a3b-vllm-single` | 1 Spark | Distinct older Omni/text profile | EC, repository-green | Declared interface is text-only despite Omni lineage; prove exact exposed capabilities | Yes, 1 Spark | qualify |
| `nemotron-3-super-120b-a12b-vllm-single` | 1 Spark | Current maximum-quality single-Spark stress profile | EC, focused harness fix | 102 GB steady plus growth is tight; require guarded admission, OOM watchdog, and rollback | Yes, 1 Spark | qualify |
| `qwen3-5-9b-vllm-single` | 1 Spark | Small Qwen multimodal baseline | EC, focused fix | Migrated to stable vLLM 0.28 with writable non-root caches and pinned 8,192-token batching/CUDA-graph bounds | Yes, 1 Spark | qualify |
| `qwen3-6-27b-vllm-single` | 1 Spark | Dense Qwen 3.6 reasoning baseline | EC, focused fix | Model metadata corrected and migrated to stable vLLM 0.28 with pinned batching/CUDA-graph bounds | Yes, 1 Spark | qualify |
| `qwen3-6-35b-a3b-nvfp4-vllm-single` | 1 Spark | New NVIDIA sparse NVFP4 efficiency candidate | EC, repository-green, new candidate | Stable vLLM 0.28 distribution and NVIDIA adapter are repository-verified; exact arm64 build and service proof remain physical gates | Yes, 1 Spark | qualify |
| `qwen3-8-27b-fp8-vllm-single` | 1 Spark | Current lower-memory Qwen 3.8 vLLM baseline | EC, focused fix | HF manifest corrected to 80 files/30.89 GB and runtime migrated to stable vLLM 0.28 with explicit scheduler bounds | Yes, 1 Spark | qualify |
| `qwen3-8-27b-nvfp4-dspark-sglang-single` | 1 Spark | Current optimized DSpark/SGLang profile | EC, focused fix | Launcher now uses DSpark block size 7, continuous mode, compile, and 0.80 memory | Yes, 1 Spark | qualify |
| `qwen3-8-27b-vllm-single` | 1 Spark | Current BF16 fidelity/reference profile | EC, focused fix | Migrated to stable vLLM 0.28 with writable caches and explicit scheduler bounds; high 96 GB steady envelope still needs proof | Yes, 1 Spark | qualify |
| `qwen3-8-flash-next-nvfp4-sglang-dual` | 2 Sparks | Current Qwen 3.8 Flash Next TP2 profile | EC, repository-green | Prove two-rank SGLang/DSpark lifecycle, MTP, multimodal calls, and recovery | Yes, exactly 2 Sparks | qualify |
| `ui-mate-27b-vllm-single` | 1 Spark | Current GUI-agent specialist | EC, repository-green | Validate image preprocessing, action schema, tool loop, and bounded context | Yes, 1 Spark | qualify |

## Image, video, audio, and 3D artifact-job recipes

These recipes must use the bounded artifact-job lane. A successful container
exit is insufficient: the controller must receive an allowed output manifest,
verify content type and size, retain the digest, and make the artifact
retrievable. That lane is implemented end to end. Forty-two exact contracts
exercise 56 cases using 19 provenance-bound fixture records and no fallback or
special-case disposition. SkinTokens uses a deterministic mesh-only derivative of Khronos'
immutable CC-BY-4.0 RiggedFigure asset;
Step1X texture uses a deterministic CC0 triangle cube plus its reference image.
Both GLBs are structurally validated before upload and again by their adapters.

| Exact recipe slug | Topology | Currentness / variant role | Static / install status | Known blocker or fix | Physical canary required | Present two-Spark disposition |
|---|---:|---|---|---|---|---|
| `flux-2-klein-4b-comfyui-single` | 1 Spark | Current small/fast text-to-image baseline | EC, repository-green; artifact lane green | Exact-output fixture and bounded controller retrieval are manifest-bound; physical PNG proof remains | Yes, 1 Spark | qualify |
| `flux-2-klein-4b-nvfp4-comfyui-single` | 1 Spark | New official NVFP4 storage/memory-efficient FLUX baseline | EC, repository-green, new candidate; artifact lane green | Three immutable artifacts total 10.84 GB and declare 28 GB steady memory; prove SM121 execution and exact four-step PNG output | Yes, 1 Spark | qualify |
| `hunyuan-video-15-distilled-diffusers-single` | 1 Spark | Distilled text-to-video speed profile | EC, repository-green; artifact fixture current; legal block | Exact Tencent model authority excludes the EU, UK, and South Korea; the Netherlands campaign must reject it before mutation | No in this jurisdiction | legal-blocked |
| `hunyuan-video-15-i2v-step-distilled-diffusers-single` | 1 Spark | Step-distilled image-to-video speed profile | EC, repository-green; artifact fixture current; legal block | Exact Tencent model authority excludes the EU, UK, and South Korea; the Netherlands campaign must reject it before mutation | No in this jurisdiction | legal-blocked |
| `hunyuan-video-15-t2v-diffusers-single` | 1 Spark | Full text-to-video quality reference | EC, repository-green; artifact fixture current; legal block | Exact Tencent model authority excludes the EU, UK, and South Korea; the Netherlands campaign must reject it before mutation | No in this jurisdiction | legal-blocked |
| `hunyuan-video-foley-xl-pytorch-single` | 1 Spark | Foley XL quality/cost baseline | EC, repository-green; artifact fixture current; legal block | Exact Tencent model authority excludes the EU, UK, and South Korea; the Netherlands campaign must reject it before mutation | No in this jurisdiction | legal-blocked |
| `hunyuan-video-foley-xxl-pytorch-single` | 1 Spark | Foley XXL higher-quality variant | EC, repository-green; artifact fixture current; legal block | Exact Tencent model authority excludes the EU, UK, and South Korea; the Netherlands campaign must reject it before mutation | No in this jurisdiction | legal-blocked |
| `hunyuan3d-omni-pytorch-single` | 1 Spark | Current Hunyuan multimodal 3D profile | EC, focused fix, build-unvalidated; artifact lane green; legal block | Offline DINOv2 companion, seeded sampling, and strict GLB validation landed, but upstream license excludes EU, UK, and South Korea; Netherlands fleet must reject admission | No in this jurisdiction | legal-blocked |
| `hunyuanocr-1-5-vllm-dflash-single` | 1 Spark | Pending-tag DFlash OCR artifact-job specialist | EC, repository-green; artifact fixture current; legal block | Exact Tencent model authority excludes the EU, UK, and South Korea; HunyuanOCR must not be actionable in the Netherlands campaign | No in this jurisdiction | legal-blocked |
| `ltx-2-19b-dev-bf16-diffusers-single` | 1 Spark | Legacy LTX-2 development/fidelity reference | EC, build-unvalidated; capacity-blocked | Old LTX generation and the checked fleet memory envelope is 1,053,716,480 bytes short | No; qualify 2.5 after capacity rises | capacity-blocked |
| `ltx-2-19b-dev-fp4-pytorch-single` | 1 Spark | Legacy LTX-2 FP4 memory variant | EC, build-unvalidated; capacity-blocked | Old native path and the checked fleet memory envelope is 1,053,716,480 bytes short | No; qualify 2.5 after capacity rises | capacity-blocked |
| `ltx-2-19b-distilled-diffusers-single` | 1 Spark | Legacy LTX-2 distilled baseline | EC, focused adapter fix | Upscaler resolution was corrected, but the generation is superseded by LTX-2.5 | No; qualify 2.5 first | superseded |
| `ltx-2-19b-distilled-fp8-diffusers-single` | 1 Spark | Legacy lower-memory FP8 variant | EC, focused adapter fix | Upscaler resolution was corrected, but the generation is superseded by LTX-2.5 | No; qualify 2.5 first | superseded |
| `ltx-2-3-22b-distilled-1-1-diffusers-single` | 1 Spark | Recent 2.3 compatibility profile | EC, focused adapter fix | Hard-coded upscaler slug fixed; authenticated filtered 2.5 recipe is the primary target | No; qualify 2.5 first | superseded |
| `ltx-2-5-22b-distilled-bf16-diffusers-single` | 1 Spark | New primary LTX-2.5 BF16 baseline with optional FP8 layerwise-cast/offload profiles | EC, repository-green, build-unvalidated, gated; artifact fixture current; capacity-blocked | Authenticated 70.09 GB snapshot is current, but the checked fleet memory envelope is 1,053,716,480 bytes short | No on current free memory | capacity-blocked |
| `minimax-h3-diffusers-single` | 1 Spark | Full MiniMax H3 synchronized-media profile | EC, focused fix; artifact fixture current; legal block | Exact MiniMax model authority denies EU use; retain the fixture for lawful jurisdictions but reject the Netherlands campaign | No in this jurisdiction | legal-blocked |
| `minimax-h3-fl2va-diffusers-single` | 1 Spark | Slim MiniMax H3 FL2VA text/keyframe profile | EC, repository-green; artifact fixture current; legal block | Distinct FL2VA artifacts and keyframe contract are current, but exact MiniMax model authority denies EU use | No in this jurisdiction | legal-blocked |
| `moss-vl-realtime-11b-pytorch-single` | 1 Spark | Realtime session-replay artifact-job specialist | EC, repository-green; artifact lane green | Digest-bound schema-v1 session references an authenticated frame; exact H.264 replay and ordered authority/ack/terminal JSONL semantics are enforced | Yes, 1 Spark | qualify |
| `mova-360p-diffusers-single` | 1 Spark | Current lower-cost MOVA synchronized profile | EC, repository-green; artifact lane green | Exact fixture/output contract is manifest-bound; physical synchronized-media proof remains | Yes, 1 Spark | qualify |
| `mova-720p-diffusers-single` | 1 Spark | Current higher-quality MOVA profile | EC, repository-green; artifact lane green | Prove the 104 GB envelope after the 360p control succeeds | Yes, 1 Spark | qualify |
| `nvidia-qwen-image-flash-diffusers-single` | 1 Spark | Current NVIDIA fast image profile | EC, focused fix; artifact lane green | Incorrect 32/24 GB memory declaration corrected to match the 57.7 GB BF16 artifact | Yes, 1 Spark | qualify |
| `pixal3d-pytorch-single` | 1 Spark | Current image-to-3D alternative | EC, focused fix, build-unvalidated; artifact lane green | Strict GLB validation, early input validation, and read-only-safe FlexGEMM cache landed; native arm64 build remains | Yes, 1 Spark | qualify |
| `qwen-image-2512-comfyui-single` | 1 Spark | ComfyUI compatibility path for Qwen Image 2512 | EC, focused job-output fix; artifact lane green | Validate the exact manifest-bound one-output contract; Diffusers is the primary baseline | Yes after Diffusers, 1 Spark | qualify |
| `qwen-image-2512-fp8-lightning-comfyui-single` | 1 Spark | Current FP8 Lightning ComfyUI efficiency variant | EC, repository-green, new candidate; artifact fixture current | Exact ComfyUI workflow, FP8 checkpoint, Lightning LoRA, and one-PNG output are digest-bound | Yes after the base control, 1 Spark | qualify |
| `qwen-image-2512-diffusers-single` | 1 Spark | Primary Qwen Image 2512 fidelity baseline | EC, focused adapter fix; artifact lane green | Prompt/output adapters corrected; exact build and PNG canary remain | Yes, 1 Spark | qualify |
| `qwen-image-2512-lightning-diffusers-single` | 1 Spark | Fast Lightning LoRA variant | EC, repository-green; artifact lane green | Prove LoRA mount/activation and compare output to the base control | Yes after base, 1 Spark | qualify |
| `qwen-image-edit-2511-comfyui-single` | 1 Spark | Current official-core-compatible ComfyUI edit variant | EC, focused fix; artifact lane green | Replaced the unloadable sharded snapshot/per-job merge with Comfy-Org's immutable monolithic transformer, scaled-FP8 encoder, and VAE (50.50 GB total); prove one- and two-reference edits | Yes, 1 Spark | qualify |
| `qwen-image-edit-2511-fp8mixed-comfyui-single` | 1 Spark | Current mixed-FP8 ComfyUI edit efficiency variant | EC, repository-green; artifact fixture current | Exact mixed-FP8 workflow reuses the same one- or two-reference semantic output contract | Yes after the base control, 1 Spark | qualify |
| `qwen-image-edit-2511-int8-convrot-comfyui-single` | 1 Spark | Current INT8 ConvRot ComfyUI edit efficiency variant | EC, repository-green, new candidate; artifact fixture current | Exact INT8 ConvRot workflow reuses the same one- or two-reference semantic output contract | Yes after the base control, 1 Spark | qualify |
| `qwen-image-edit-2511-diffusers-single` | 1 Spark | Primary Qwen Image Edit baseline | EC, focused adapter fix; artifact lane green | Input-image/prompt adapter fixed; prove output and input immutability | Yes, 1 Spark | qualify |
| `qwen-image-edit-2511-lightning-diffusers-single` | 1 Spark | Fast edit Lightning LoRA variant | EC, repository-green; artifact lane green | Prove LoRA activation and fidelity delta after base edit canary | Yes after base, 1 Spark | qualify |
| `qwen-image-layered-diffusers-single` | 1 Spark | Current layered image specialist | EC, focused adapter fix; artifact lane green | Prove declared multi-layer output contract and bounded artifact count | Yes, 1 Spark | qualify |
| `skintokens-pytorch-single` | 1 Spark | Current rigging specialist | EC, repository-green, build-unvalidated; artifact lane green | A deterministic mesh-only Khronos RiggedFigure derivative is pinned by source revision, transformation recipe, SHA-256, CC-BY-4.0 license, and attribution; strict input and skinned-output GLB validation landed | Yes, 1 Spark | qualify |
| `step1x-3d-geometry-pytorch-single` | 1 Spark | Current geometry-only Step1X stage | EC, focused fix; artifact lane green | SciPy Voronoi replacement and missing dependency/global import defects fixed | Yes, 1 Spark | qualify |
| `step1x-3d-label-geometry-pytorch-single` | 1 Spark | Current labeled-geometry Step1X stage | EC, focused fix; artifact lane green | Same native fixes; prove labels are present and bound to valid geometry | Yes after geometry, 1 Spark | qualify |
| `step1x-3d-texture-pytorch-single` | 1 Spark | Current texture completion Step1X stage | EC, focused fix; artifact lane green | Deterministic nondegenerate cube and reference image exercise upstream xatlas texturing; strict input and textured-output GLB validation landed; prove the 72 GB envelope | Yes, 1 Spark | qualify |
| `trellis-2-4b-pytorch-single` | 1 Spark | Current TRELLIS.2 image-to-3D baseline | EC, focused fix, build-unvalidated; artifact lane green | Shared native adapter has strict GLB validation and read-only-safe FlexGEMM cache; exact build remains | Yes, 1 Spark | qualify |
| `triposg-pytorch-single` | 1 Spark | Current TripoSG image-to-3D profile | EC, focused license fix; artifact lane green | `diso==0.1.4` is CC BY-NC 4.0, not Apache; admit only non-commercial use | Yes for allowed use, 1 Spark | qualify |
| `wan-2-2-i2v-14b-comfyui-single` | 1 Spark | Current Wan 2.2 image-to-video quality profile | EC, focused fix; artifact lane green | Exact 640x640, 81-frame, 16 fps contract and strict MP4 validation landed | Yes, 1 Spark | qualify |
| `wan-2-2-t2v-14b-comfyui-single` | 1 Spark | Current Wan 2.2 text-to-video quality profile | EC, focused fix; artifact lane green | Exact 640x640, 81-frame, 16 fps contract and strict MP4 validation landed | Yes, 1 Spark | qualify |
| `wan-2-2-ti2v-5b-comfyui-single` | 1 Spark | Current smaller TI2V efficiency profile | EC, focused fix; artifact lane green | Exact 1280x704, 121-frame, 24 fps H.264 contract and strict output validation landed | Yes, 1 Spark | qualify |
| `wan-dancer-14b-pytorch-single` | 1 Spark | Current music-conditioned dance specialist | EC, repository-green; artifact fixture current; capacity-blocked | Manifest uses a deterministic digest-pinned one-second PCM music fixture, but the checked fleet memory envelope is 1,053,716,480 bytes short | No on current free memory | capacity-blocked |

## Generic acceptance procedure

Perform these steps against immutable content and record every response as
machine-readable evidence. A retry reuses the same request key when the first
outcome is uncertain. Never repair or deploy through SSH.

1. **Freeze and validate the catalog.** Regenerate the catalog index, check that
   the result is clean, and run the platform validator against the exact
   library and platform commits:

   ```bash
   tools/build-catalog-index
   tools/build-catalog-index --check
   /opt/vonk-forge/control/.venv/bin/python \
     /opt/vonk-forge/scripts/validate-recipe-library \
     --library-root /opt/vonk-forge-recipes \
     --platform-root /opt/vonk-forge \
     --json
   ```

2. **Upgrade through the controller relay.** Inspect the signed package and
   exact eligible nodes, then apply the returned digest one Spark at a time:

   ```bash
   vonkctl fleet upgrade candidate --json
   vonkctl fleet upgrade preview --strategy one-at-a-time --json
   vonkctl fleet upgrade apply --strategy one-at-a-time \
     --plan-digest PLAN_DIGEST --apply --json
   vonkctl fleet agents --json
   ```

   Do not continue until each selected node has reconnected under the same
   enrolled identity and reports the expected agent release and healthy
   inventory.

3. **Preview the digest-bound fleet campaign.** Preview is read-only. It
   inventories the live enrolled fleet and exact public catalog, applies legal
   and topology classifications, previews import and placement, and writes a
   hash-chained owner-only ledger:

   ```bash
   export VONK_OPERATOR_JURISDICTION=NL
   vonk-fleet-qualify \
     --ledger qualification-evidence.jsonl \
     > qualification-plan.json
   ```

   Use repeated `--recipe PUBLISHER/SLUG` options for a bounded canary; omit
   them only for the reviewed full campaign.

4. **Apply only the reviewed plan digest.** Apply regenerates the plan and
   refuses catalog or fleet drift. It processes one recipe at a time and uses
   deterministic idempotency keys for import, mapping, build, distribution,
   install, activation/load, smoke, and stop:

   ```bash
   vonk-fleet-qualify \
     --ledger qualification-evidence.jsonl \
     --plan-digest PLAN_DIGEST \
     --apply
   ```

   Before each install, require fresh controller storage telemetry and a
   `fits` capacity decision. Missing evidence or insufficient space is a clean
   pre-install block; it never authorizes silent eviction.

5. **Exercise the exact interface twice.** For OpenAI-compatible services, the
   runner uses the 105 digest-bound capability cases and verifies the exact
   `/models` alias plus the declared arithmetic, tool, reasoning, vision, OCR,
   or GUI-action contract. For artifact producers, it uses the durable
   controller artifact-job lifecycle with typed input slots, fixture hashes,
   parameters, output limits, cancellation, output download, and semantic
   assertions such as PNG dimensions, WAV duration, MP4 codec/frame rate, GLB
   structure, or ZIP membership.

6. **Prove warm-cache deployment and retention.** After the first successful
   smoke, stop the runtime, retain the installation/model/cache, start that same
   installation without a second install operation, repeat the exact smoke,
   and stop again. A forced uninstall is an explicit operator choice, not the
   campaign default. For two-Spark recipes, also record endpoint ownership,
   rank ordering, and the separately reviewed rank-loss/recovery tier.

7. **Inventory residency, then accept or retain Candidate.** At campaign end,
   require `run.residency-inventoried` and reconcile every successful recipe to
   its retained installation and exact fleet state. Acceptance requires the two
   successful digest-bound smokes, warm-cache redeploy, clean lifecycle,
   healthy post-run fleet, and signed evidence below. Otherwise preserve the
   observed disposition without deleting another resident model or bypassing a
   legal, gated-access, fixture, resource, or topology guard.

## Required evidence fields

Every per-recipe evidence record must contain at least:

| Area | Required fields |
|---|---|
| Campaign | UTC start/end; operator; controller origin; campaign ID; matrix revision |
| Source identity | platform commit; recipe-library commit; catalog index SHA-256; recipe publisher/slug/content SHA-256; release version |
| Model/runtime identity | model repository and immutable revision; artifact manifest SHA-256 and byte totals; runtime distribution digest; patch/source-bundle digest; built OCI image digest |
| Fleet identity | topology and world size; node IDs and role/rank; agent versions; certificate/enrollment continuity; OS/kernel/driver/CUDA inventory |
| Admission | legal/jurisdiction decision; license/use restriction; fresh free disk and unified memory; conservative per-node capacity requirement; `automatic_eviction: false`; placement digest; install plan digest; declared versus observed peaks |
| Installation | mapping/build/installation/operation IDs; request keys; artifact download/install observations; build network mode and reviewed hosts; retained installation ownership; timestamps and terminal state |
| Service checks | run ID; readiness latency; `/v1/models` identity; exact request fixture hashes; response/status hashes; parser/tool/multimodal assertions; first-token and completion timing |
| Artifact-job checks | job/run/operation IDs; signed job-claim digest; input manifest and per-file SHA-256/media type/bytes; parameters digest; timeout/output limits; exit code; output manifest and per-file SHA-256/media type/bytes; semantic validator output |
| Resilience | first stop; warm-cache redeploy run identity without reinstall; second digest-bound smoke; second stop; for distributed recipes, rank-loss observation and recovery result; scratch/output cleanup result |
| Residency | final `run.residency-inventoried` record; retained installation IDs and exact revisions; operational states; node readiness; final fleet digest; blocked-before-eviction capacity records |
| Outcome | `accepted` or retained `candidate`; concise reason; known issue link; reviewer; evidence bundle SHA-256/signature |

Repository CI and catalog publication can prove that a recipe remains
installable. Only the physical evidence above can change its qualification to
accepted.
