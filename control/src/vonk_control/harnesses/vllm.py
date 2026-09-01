"""Target-driven vLLM execution-harness compiler."""

from __future__ import annotations

import json
from collections.abc import Mapping
from math import isfinite

from .common import (
    ArgumentSpec,
    HarnessCompileError,
    compile_arguments,
    compile_environment,
    decimal,
    integer,
    model_artifact_mounts,
    one_of,
    primary_model_artifact_mount,
    projection,
    require_entrypoint,
    require_openai_interface,
    validate_topology,
)
from .contracts import HarnessProjection

_ARGUMENTS = {
    "max-model-len": ArgumentSpec("--max-model-len", validate=integer(1, 10_000_000)),
    "gpu-memory-utilization": ArgumentSpec(
        "--gpu-memory-utilization", validate=decimal(0.01, 1.0)
    ),
    "tensor-parallel-size": ArgumentSpec(
        "--tensor-parallel-size", validate=integer(1, 16)
    ),
    "tp": ArgumentSpec("--tensor-parallel-size", validate=integer(1, 16)),
    "pipeline-parallel-size": ArgumentSpec(
        "--pipeline-parallel-size", validate=integer(1, 16)
    ),
    "max-num-seqs": ArgumentSpec("--max-num-seqs", validate=integer(1, 65_536)),
    "max-num-batched-tokens": ArgumentSpec(
        "--max-num-batched-tokens", validate=integer(1, 10_000_000)
    ),
    "max-cudagraph-capture-size": ArgumentSpec(
        "--max-cudagraph-capture-size", validate=integer(1, 65_536)
    ),
    "kv-cache-memory-bytes": ArgumentSpec(
        "--kv-cache-memory-bytes", validate=integer(1, 128_000_000_000)
    ),
    "kv-cache-memory": ArgumentSpec(
        "--kv-cache-memory", validate=integer(1, 128_000_000_000)
    ),
    "compilation-config": ArgumentSpec(
        "--compilation-config", validate=lambda value: _json_object(value)
    ),
    "long-prefill-token-threshold": ArgumentSpec(
        "--long-prefill-token-threshold", validate=integer(1, 10_000_000)
    ),
    "block-size": ArgumentSpec("--block-size", validate=integer(1, 16_384)),
    "quantization": ArgumentSpec(
        "--quantization",
        validate=one_of("awq", "gptq", "fp8", "modelopt_fp4", "bitsandbytes"),
    ),
    "dtype": ArgumentSpec(
        "--dtype", validate=one_of("auto", "float16", "bfloat16", "float32")
    ),
    "kv-cache-dtype": ArgumentSpec(
        "--kv-cache-dtype",
        validate=one_of(
            "auto", "fp8", "fp8_e4m3", "fp8_e5m2", "fp8_ds_mla", "nvfp4_ds_mla"
        ),
    ),
    "decode-context-parallel-size": ArgumentSpec(
        "--decode-context-parallel-size", validate=integer(1, 16)
    ),
    "dcp-comm-backend": ArgumentSpec(
        "--dcp-comm-backend", validate=one_of("ag_rs", "a2a")
    ),
    "mm-encoder-tp-mode": ArgumentSpec(
        "--mm-encoder-tp-mode", validate=one_of("data", "weights")
    ),
    "hf-overrides": ArgumentSpec(
        "--hf-overrides", validate=lambda value: _json_object(value)
    ),
    "served-model-name": ArgumentSpec("--served-model-name"),
    "tool-call-parser": ArgumentSpec("--tool-call-parser"),
    "enable-auto-tool-choice": ArgumentSpec(
        "--enable-auto-tool-choice", takes_value=False
    ),
    "enable-prefix-caching": ArgumentSpec("--enable-prefix-caching", takes_value=False),
    "language-model-only": ArgumentSpec("--language-model-only", takes_value=False),
    "enable-prompt-tokens-details": ArgumentSpec(
        "--enable-prompt-tokens-details", takes_value=False
    ),
    "enable-chunked-prefill": ArgumentSpec(
        "--enable-chunked-prefill", takes_value=False
    ),
    "disable-chunked-prefill": ArgumentSpec(
        "--no-enable-chunked-prefill", takes_value=False
    ),
    "enforce-eager": ArgumentSpec("--enforce-eager", takes_value=False),
    "skip-mm-profiling": ArgumentSpec("--skip-mm-profiling", takes_value=False),
    "async-scheduling": ArgumentSpec("--async-scheduling", takes_value=False),
    "mamba-backend": ArgumentSpec(
        "--mamba-backend", validate=one_of("triton", "flashinfer")
    ),
    "mamba-ssm-cache-dtype": ArgumentSpec(
        "--mamba-ssm-cache-dtype",
        validate=one_of("auto", "bfloat16", "float16", "float32"),
    ),
    "mamba-cache-mode": ArgumentSpec("--mamba-cache-mode", validate=one_of("align")),
    "attention-backend": ArgumentSpec(
        "--attention-backend",
        validate=one_of("FLASH_ATTN", "FLASHINFER", "TRITON_ATTN"),
    ),
    "enable-flashinfer-autotune": ArgumentSpec(
        "--enable-flashinfer-autotune", takes_value=False
    ),
    "disable-flashinfer-autotune": ArgumentSpec(
        "--no-enable-flashinfer-autotune", takes_value=False
    ),
    "speculative-config": ArgumentSpec("--speculative-config"),
    "tokenizer-mode": ArgumentSpec("--tokenizer-mode", validate=one_of("deepseek_v4")),
    "moe-backend": ArgumentSpec(
        "--moe-backend", validate=one_of("flashinfer_b12x", "marlin")
    ),
    "reasoning-parser": ArgumentSpec(
        "--reasoning-parser",
        validate=one_of(
            "deepseek_r1",
            "deepseek_v4",
            "gemma4",
            "glm45",
            "muse_glimmer",
            "nano_v3",
            "nemotron_v3",
            "poolside_v1",
            "qwen3",
            "super_v3",
        ),
    ),
    "reasoning-parser-plugin": ArgumentSpec(
        "--reasoning-parser-plugin",
        validate=one_of(
            "/models/nano_v3_reasoning_parser.py",
            "/models/super_v3_reasoning_parser.py",
            "/models/target/super_v3_reasoning_parser.py",
        ),
    ),
    "reasoning-config": ArgumentSpec("--reasoning-config"),
    "default-chat-template-kwargs": ArgumentSpec("--default-chat-template-kwargs"),
    "generation-config": ArgumentSpec(
        "--generation-config", validate=one_of("auto", "vllm")
    ),
    "allowed-local-media-path": ArgumentSpec(
        "--allowed-local-media-path", validate=one_of("/inputs")
    ),
    "limit-mm-per-prompt": ArgumentSpec(
        "--limit-mm-per-prompt", validate=lambda value: _limited_mm_per_prompt(value)
    ),
    "media-io-kwargs": ArgumentSpec(
        "--media-io-kwargs", validate=lambda value: _media_io_kwargs(value)
    ),
    "video-pruning-rate": ArgumentSpec(
        "--video-pruning-rate", validate=lambda value: _video_pruning_rate(value)
    ),
    "chat-template": ArgumentSpec(
        "--chat-template",
        validate=one_of(
            "/models/target/chat_template.thinking-off.jinja",
            "/opt/vonk/templates/glm53-chat-template-mm.jinja",
        ),
    ),
    "chat-template-content-format": ArgumentSpec(
        "--chat-template-content-format", validate=one_of("openai")
    ),
    "mm-processor-cache-gb": ArgumentSpec(
        "--mm-processor-cache-gb", validate=integer(0, 128)
    ),
    "no-enable-prefix-caching": ArgumentSpec(
        "--no-enable-prefix-caching", takes_value=False
    ),
    "trust-remote-code": ArgumentSpec("--trust-remote-code", takes_value=False),
    "host": ArgumentSpec("--host", emit=False, validate=one_of("0.0.0.0")),
    "port": ArgumentSpec("--port", emit=False, validate=integer(1024, 65_535)),
}


class VllmHarnessCompiler:
    slug = "vllm"
    contract_version = 1

    def compile(
        self,
        recipe: Mapping[str, object],
        distribution: Mapping[str, object],
        patch: Mapping[str, object] | None,
        parameters: Mapping[str, object],
        topology: Mapping[str, object],
        role: str,
        rank: int,
    ) -> HarnessProjection:
        primary_artifact_id, primary_mount = primary_model_artifact_mount(recipe)
        artifact_mounts = model_artifact_mounts(recipe)
        if len(artifact_mounts) not in {1, 2}:
            raise HarnessCompileError(
                "vLLM recipes require one target and at most one companion artifact"
            )
        _require_role_artifacts(recipe, role, artifact_mounts)
        require_entrypoint(recipe, ("/opt/vonk/bin/vllm", "serve", primary_mount))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        _validate_chunked_prefill_controls(parsed)
        local_media_input = parsed.get("--allowed-local-media-path") is not None
        _validate_reasoning_plugin(parsed, primary_mount=primary_mount)
        _validate_speculative_config(
            parsed.get("--speculative-config"),
            primary_artifact_id=primary_artifact_id,
            artifact_mounts=artifact_mounts,
        )
        port = require_openai_interface(recipe)
        mode = topology.get("mode") if isinstance(topology, Mapping) else None
        validate_topology(
            topology,
            role,
            rank,
            modes=frozenset({"single", "distributed"}),
        )
        tensor = int(str(parsed.get("--tensor-parallel-size", "1")))
        pipeline = int(str(parsed.get("--pipeline-parallel-size", "1")))
        distributed: tuple[str, ...] = ()
        rank_environment: tuple[tuple[str, str], ...] = ()
        if mode == "distributed":
            capability = distribution.get("capabilities")
            implementation = (
                capability.get("distributed_vllm")
                if isinstance(capability, Mapping)
                else None
            )
            mechanism = (
                implementation.get("mechanism")
                if isinstance(implementation, Mapping)
                else None
            )
            parallelism = topology.get("parallelism")
            node_count = topology.get("node_count")
            if (
                patch is None
                or not isinstance(implementation, Mapping)
                or implementation.get("verified") is not True
                or mechanism not in {"vllm-mp", "vllm-ray"}
                or implementation.get("topology_mode") != "distributed"
                or implementation.get("node_count") != node_count
                or implementation.get("world_size") != node_count
                or not isinstance(parallelism, Mapping)
                or implementation.get("tensor_parallel_size")
                != parallelism.get("tensor")
                or implementation.get("pipeline_parallel_size")
                != parallelism.get("pipeline")
                or implementation.get("data_parallel_size") != parallelism.get("data")
                or implementation.get("endpoint_role") != "entrypoint"
                or implementation.get("worker_role") != "worker"
                or implementation.get("rank_loss_withdraws_endpoint") is not True
                or implementation.get("fabric") != "nccl-roce"
                or parallelism.get("backend")
                != ("ray" if mechanism == "vllm-ray" else "mp")
            ):
                raise HarnessCompileError(
                    "vLLM distributed topology requires a verified distributed vLLM distribution"
                )
            if tensor not in (1, int(str(parallelism["tensor"]))) or pipeline not in (
                1,
                int(str(parallelism["pipeline"])),
            ):
                raise HarnessCompileError(
                    "vLLM arguments conflict with distributed parallelism"
                )
            launch = implementation.get("launch")
            rendezvous = (
                launch.get("rendezvous") if isinstance(launch, Mapping) else None
            )
            profiles = (
                launch.get("rank_profiles") if isinstance(launch, Mapping) else None
            )
            profile = (
                profiles[rank]
                if isinstance(profiles, list) and 0 <= rank < len(profiles)
                else None
            )
            fabric_environment = (
                profile.get("environment") if isinstance(profile, Mapping) else None
            )
            expected_environment = {
                "GLOO_SOCKET_IFNAME",
                "NCCL_IB_GID_INDEX",
                "NCCL_IB_HCA",
                "NCCL_SOCKET_IFNAME",
                "TP_SOCKET_IFNAME",
            }
            if (
                not isinstance(rendezvous, Mapping)
                or rendezvous
                != {
                    "local_address_environment": "VONK_LOCAL_ADDR",
                    "master_address_environment": "VONK_MASTER_ADDR",
                    "master_port_environment": "VONK_MASTER_PORT",
                    "master_role": "entrypoint",
                }
                or not isinstance(profiles, list)
                or len(profiles) != node_count
                or not isinstance(profile, Mapping)
                or profile.get("rank") != rank
                or profile.get("role") != role
                or not isinstance(fabric_environment, Mapping)
                or set(fabric_environment) != expected_environment
                or any(
                    type(value) is not str or not value
                    for value in fabric_environment.values()
                )
            ):
                raise HarnessCompileError(
                    "vLLM distributed topology requires a complete launch contract"
                )
            rank_environment = tuple(
                (name, str(fabric_environment[name]))
                for name in sorted(expected_environment)
            )
            distributed = (
                "--tensor-parallel-size",
                str(parallelism["tensor"]),
                "--pipeline-parallel-size",
                str(parallelism["pipeline"]),
                "--distributed-executor-backend",
                "mp" if mechanism == "vllm-mp" else "ray",
                "--nnodes",
                str(node_count),
                "--node-rank",
                str(rank),
                *(("--headless",) if role == "worker" else ()),
            )
        elif tensor != 1 or pipeline != 1:
            raise HarnessCompileError(
                "vLLM single-node harness does not support distributed parallelism"
            )
        environment = compile_environment(
            recipe,
            distribution,
            frozenset(
                {
                    "CUTE_DSL_ARCH",
                    "DG_JIT_NVCC_COMPILER",
                    "DG_JIT_USE_NVRTC",
                    "DSPARK_MAX_INFLIGHT_PREFILLS",
                    "FLASHINFER_CUDA_ARCH_LIST",
                    "FLASHINFER_DISABLE_VERSION_CHECK",
                    "FLASHINFER_WORKSPACE_BASE",
                    "GLM52_B12X_MLA",
                    "GLM52_B12X_SCORE_MODE",
                    "GLM52_BIND_HOST_TRITON",
                    "GLM52_MQA_LOGITS_TRITON",
                    "GLM52_PAGED_MQA_TOPK_CHUNK_SIZE",
                    "GLM52_PAGED_MQA_TRITON",
                    "GLM_MOE_AQLM_CB",
                    "GLM_MOE_AQLM_STREAM",
                    "GLM_MOE_LANE_ROWS",
                    "GLM_NVFP4_STREAM",
                    "GLM_NVFP4_LUT256",
                    "GLM_SHARED_EXPERTS_DEBUG",
                    "HF_HUB_DISABLE_XET",
                    "HF_HUB_OFFLINE",
                    "NCCL_CROSS_NIC",
                    "NCCL_CUMEM_ENABLE",
                    "NCCL_DEBUG",
                    "NCCL_BUFFSIZE",
                    "NCCL_GIN_ENABLE",
                    "NCCL_GRAPH_MIXING_SUPPORT",
                    "NCCL_IB_ADDR_FAMILY",
                    "NCCL_IB_DISABLE",
                    "NCCL_IB_MERGE_NICS",
                    "NCCL_IB_ROCE_VERSION_NUM",
                    "NCCL_IB_SUBNET_AWARE_ROUTING",
                    "NCCL_IGNORE_CPU_AFFINITY",
                    "NCCL_MAX_NCHANNELS",
                    "NCCL_MIN_NCHANNELS",
                    "NCCL_NET",
                    "NCCL_NVLS_ENABLE",
                    "NCCL_P2P_DISABLE",
                    "NCCL_PROTO",
                    "NCCL_SHM_DISABLE",
                    "PYTORCH_CUDA_ALLOC_CONF",
                    "TILELANG_CACHE_DIR",
                    "TILELANG_CLEANUP_TEMP_FILES",
                    "TORCH_CUDA_ARCH_LIST",
                    "TRANSFORMERS_OFFLINE",
                    "TRITON_CACHE_DIR",
                    "VLLM_ALLOW_LONG_MAX_MODEL_LEN",
                    "VLLM_B12X_W4A16_FORCE_BLOCKS_PER_SM",
                    "VLLM_B12X_W4A16_FORCE_BLOCKS_MAX_M",
                    "VLLM_EXECUTE_MODEL_TIMEOUT_SECONDS",
                    "VLLM_FLASHINFER_MOE_BACKEND",
                    "VLLM_GLM_TP_PAD",
                    "VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS",
                    "VLLM_MTP_INDEX_SHARE",
                    "VLLM_NCCL_SO_PATH",
                    "VLLM_NO_USAGE_STATS",
                    "VLLM_NVFP4_GEMM_BACKEND",
                    "VLLM_PREFIX_CACHE_RETENTION_INTERVAL",
                    "VLLM_SPARSE_INDEXER_MAX_LOGITS_MB",
                    "VLLM_USE_B12X_MOE",
                    "VLLM_DISABLE_FP8_W8A16",
                    "VLLM_MARLIN_USE_ATOMIC_ADD",
                    "VLLM_USE_FLASHINFER_SAMPLER",
                    "VLLM_USE_FLASHINFER_MOE_FP4",
                    "VLLM_USE_BREAKABLE_CUDAGRAPH",
                    "VLLM_WORKER_MULTIPROC_METHOD",
                }
            ),
        )
        environment = (*environment, *rank_environment)
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/vllm",
                "serve",
                primary_mount,
                *arguments,
                *distributed,
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
            ),
            recipe=recipe,
            distribution=distribution,
            environment=environment,
            allow_local_media_input=local_media_input,
        )


def _validate_reasoning_plugin(
    parsed: Mapping[str, str | bool], *, primary_mount: str
) -> None:
    parser = parsed.get("--reasoning-parser")
    plugin = parsed.get("--reasoning-parser-plugin")
    required = {
        "nano_v3": f"{primary_mount}/nano_v3_reasoning_parser.py",
        "super_v3": f"{primary_mount}/super_v3_reasoning_parser.py",
    }
    expected = required.get(parser) if isinstance(parser, str) else None
    if expected is not None and plugin != expected:
        raise HarnessCompileError(
            f"{parser} reasoning parser requires its immutable model-snapshot plugin"
        )
    if expected is None and plugin is not None:
        raise HarnessCompileError(
            "reasoning parser plugin is only valid for a reviewed custom parser"
        )


def _require_role_artifacts(
    recipe: Mapping[str, object],
    role: str,
    artifact_mounts: tuple[tuple[str, str], ...],
) -> None:
    artifacts = recipe.get("artifacts")
    if type(artifacts) is not list:
        raise HarnessCompileError("vLLM role artifact declarations are invalid")
    declared: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise HarnessCompileError("vLLM role artifact declarations are invalid")
        artifact_id = artifact.get("id")
        roles = artifact.get("roles")
        mount = artifact.get("mount")
        target = mount.get("target") if isinstance(mount, Mapping) else None
        if (
            len(artifacts) == 1
            and artifact_id is None
            and roles is None
            and target == "/models"
            and role == "entrypoint"
        ):
            # Preserve the minimal synthetic conformance recipe accepted by common.py.
            declared.add("model")
            continue
        if (
            type(artifact_id) is not str
            or type(roles) is not list
            or any(type(item) is not str for item in roles)
            or len(set(roles)) != len(roles)
        ):
            raise HarnessCompileError("vLLM role artifact declarations are invalid")
        if role in roles:
            declared.add(artifact_id)
    required = {artifact_id for artifact_id, _target in artifact_mounts}
    if declared != required:
        raise HarnessCompileError(
            "vLLM role does not declare every required model artifact"
        )


def _validate_speculative_config(
    value: str | bool | None,
    *,
    primary_artifact_id: str,
    artifact_mounts: tuple[tuple[str, str], ...],
) -> None:
    if value is None:
        if len(artifact_mounts) > 1:
            raise HarnessCompileError(
                "vLLM companion artifact requires a speculative config model path"
            )
        return
    if type(value) is not str:
        raise HarnessCompileError("vLLM speculative config JSON is invalid")
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise HarnessCompileError("vLLM speculative config JSON is invalid") from error
    if not isinstance(parsed, Mapping):
        raise HarnessCompileError("vLLM speculative config JSON is invalid")
    companion_mounts = {
        target
        for artifact_id, target in artifact_mounts
        if artifact_id != primary_artifact_id
    }
    if "model" not in parsed:
        if companion_mounts:
            raise HarnessCompileError(
                "vLLM companion artifact requires a speculative config model path"
            )
        return
    model = parsed["model"]
    if type(model) is not str or model not in companion_mounts:
        raise HarnessCompileError(
            "vLLM speculative config model path must name one declared companion artifact"
        )


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate JSON member")
        result[name] = value
    return result


def _json_object(value: str) -> bool:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, Mapping)


def _limited_mm_per_prompt(value: str) -> bool:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return False
    return (
        isinstance(parsed, Mapping)
        and bool(parsed)
        and set(parsed) <= {"audio", "image", "video"}
        and all(type(limit) is int and 0 <= limit <= 16 for limit in parsed.values())
    )


def _media_io_kwargs(value: str) -> bool:
    try:
        parsed = json.loads(
            value,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(parsed, Mapping) or set(parsed) != {"video"}:
        return False
    video = parsed["video"]
    if (
        not isinstance(video, Mapping)
        or not video
        or not set(video) <= {"fps", "num_frames"}
    ):
        return False
    fps = video.get("fps")
    if fps is not None and (
        type(fps) not in {int, float} or not isfinite(fps) or not 0 < fps <= 60
    ):
        return False
    num_frames = video.get("num_frames")
    return num_frames is None or type(num_frames) is int and 1 <= num_frames <= 256


def _video_pruning_rate(value: str) -> bool:
    try:
        parsed = float(value)
    except ValueError:
        return False
    return isfinite(parsed) and 0 <= parsed < 1


def _validate_chunked_prefill_controls(parsed: Mapping[str, str | bool]) -> None:
    if (
        parsed.get("--enable-chunked-prefill") is True
        and parsed.get("--no-enable-chunked-prefill") is True
    ):
        raise HarnessCompileError("vLLM chunked prefill controls conflict")


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid JSON constant: {value}")
