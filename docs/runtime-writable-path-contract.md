# Runtime writable-path contract

Built-in harnesses compile runtime writes into the writable `/outputs` mount.
The root filesystem and model mounts remain read-only; the persistent
`/outputs/cache` bind is kept at the installation level so a stopped container
can reuse compiled artifacts on its next launch. Runtime path environment names
are platform-owned and recipes cannot repeat or override them.

The source column records the current platform or recipe source used for the
audit. It is evidence for path selection, not a claim that every image has
been physically accepted. `llama-cpp` and `tensorrt-llm` are supported built-in
harnesses but have no current published recipe in the sibling catalog; their
contract is therefore limited to the generic XDG/home/temp paths until a
runtime-specific source proves an additional cache variable.

The indexed source set is deliberately concrete: `adapters/llm/vllm-openai/Dockerfile:15-25`,
`adapters/llm/laguna-s-vllm/Dockerfile:15-28`, and
`adapters/nvidia/qwen36-35b-vllm/Dockerfile:13-23` provide the vLLM
Python/Triton defaults; `adapters/qwen/flash-next-sglang-dual/Dockerfile:22-29`
and `adapters/llm/inkling-sglang-eight/Dockerfile:15-20` provide SGLang
examples. The current Diffusers, ComfyUI, and pipeline images provide the
shared Python framework defaults. These defaults are selected by the
platform-owned harness identity while compiling the canonical recipe's
execution, runtime, and topology projection. A runtime image or source-build
receipt cannot add a second writable root or change the selected destinations.

The read-only catalog audit at 2026-09-05 found 84 recipes across six active
families (vLLM 33, PyTorch pipeline 21, Diffusers 13, ComfyUI 10, SGLang 5,
and DS4 2). Twelve reserved path variables occurred in the two MIA vLLM
recipes before recipe commit `8da79f23`; that cleanup is complete and the
recipe copies are absent there.
Triton, TorchInductor, compiler, framework, and temporary paths are injected
from the engine harness contract for every compatible image, whether the
recipe consumes a direct image or a source-build receipt. Variant-specific
variables such as FlashInfer or NCCL trace destinations remain reserved so a
recipe cannot move them into the image root; they are supplied only by a
future harness contract with direct source evidence.

| Engine | Path | Default | Source | Validation | Test |
| --- | --- | --- | --- | --- | --- |
| vLLM | XDG cache | `/outputs/cache` | vLLM wrapper and `XDG_CACHE_HOME` contract | Exact harness value; recipe override rejected | `test_builtin_harnesses.py`, runtime spec tests |
| vLLM | engine cache | `/outputs/cache/vllm` | vLLM wrapper `VLLM_CACHE_ROOT`; vLLM source defaults below XDG cache | Exact harness value; must be under output mount | `test_runtime_writable_paths.py` |
| vLLM | Triton, TorchInductor, Torch extensions | `/outputs/cache/triton`, `/outputs/cache/torchinductor`, `/outputs/cache/torch_extensions` | Current vLLM recipe Dockerfiles | Exact harness values; recipe path variables rejected | `test_runtime_writable_paths.py` |
| vLLM variant paths | FlashInfer, TileLang, TVM, B12X compile and NCCL traces | Reserved platform paths; no recipe injection | Harness contract and direct runtime evidence required before enabling a variant | Recipe values are rejected until a matching harness contract exists | `test_builtin_harnesses.py`, `test_runtime_writable_paths.py` |
| SGLang | XDG cache, temp, Triton, Torch extensions/Inductor | `/outputs/cache`, `/outputs/tmp`, `/outputs/cache/triton`, `/outputs/cache/torch_extensions`, `/outputs/cache/torchinductor` | `adapters/qwen/flash-next-sglang-dual/Dockerfile` | Generic path set is injected by harness; recipe repeats and overrides are rejected | `test_runtime_writable_paths.py` |
| Diffusers, ComfyUI, PyTorch pipeline, DS4 | Python/Hugging Face, Torch, Triton and temp paths | `/outputs/cache/{huggingface,torch,triton,...}`, `/outputs/tmp` | Current sibling recipe Dockerfiles and runtime wrappers | Shared Python engine contract; no unknown path envs are added | `test_runtime_writable_paths.py` |
| llama.cpp | XDG cache, home and temp | `/outputs/cache`, `/outputs/cache/home`, `/outputs/tmp` | Built-in harness has no published recipe-specific cache source | Only proven generic paths are allowed | `test_runtime_writable_paths.py` |
| TensorRT-LLM | XDG cache, home and temp | `/outputs/cache`, `/outputs/cache/home`, `/outputs/tmp` | Built-in harness has no published recipe-specific cache source | Only proven generic paths are allowed | `test_runtime_writable_paths.py` |

The Rust workload validator repeats the security boundary: every compiled
writable path is absolute, unique, below `/outputs`, and accompanied by the
central environment values. OCI startup creates the installation cache and
binds it at `/outputs/cache`; it does not make the image root writable or
replace packaged image content.

Validation evidence is separated by boundary. The Python harness/runtime
contract suite covers structural projection and distribution-independent
engine defaults; the
recipe-library validator passed the 84-recipe catalog at recipe commit
`8da79f23` against this platform worktree. A disposable OrbStack container
(`orbstack` Docker 29.4.0, `python:3.12-bookworm`) started with `--read-only`,
`--cap-drop=ALL`, `no-new-privileges`, nonroot UID 10001, `--network none`, and
only the declared `/outputs` bind; it wrote the cache and temp probes
successfully. This is container evidence only. GPU execution, NCCL fabric,
model quality, and physical Spark qualification remain separate acceptance
lanes.
