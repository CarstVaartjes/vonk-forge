"""Target-driven vLLM execution-harness compiler."""

from __future__ import annotations

from collections.abc import Mapping

from .common import (
    ArgumentSpec,
    HarnessCompileError,
    compile_arguments,
    compile_environment,
    decimal,
    integer,
    one_of,
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
    "long-prefill-token-threshold": ArgumentSpec(
        "--long-prefill-token-threshold", validate=integer(1, 10_000_000)
    ),
    "block-size": ArgumentSpec("--block-size", validate=integer(1, 1024)),
    "quantization": ArgumentSpec(
        "--quantization", validate=one_of("awq", "gptq", "fp8", "bitsandbytes")
    ),
    "dtype": ArgumentSpec(
        "--dtype", validate=one_of("auto", "float16", "bfloat16", "float32")
    ),
    "kv-cache-dtype": ArgumentSpec(
        "--kv-cache-dtype",
        validate=one_of("auto", "fp8", "fp8_e4m3", "fp8_e5m2", "nvfp4_ds_mla"),
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
    "async-scheduling": ArgumentSpec("--async-scheduling", takes_value=False),
    "mamba-backend": ArgumentSpec(
        "--mamba-backend", validate=one_of("triton", "flashinfer")
    ),
    "mamba-ssm-cache-dtype": ArgumentSpec(
        "--mamba-ssm-cache-dtype",
        validate=one_of("auto", "bfloat16", "float16", "float32"),
    ),
    "mamba-cache-mode": ArgumentSpec("--mamba-cache-mode", validate=one_of("align")),
    "enable-flashinfer-autotune": ArgumentSpec(
        "--enable-flashinfer-autotune", takes_value=False
    ),
    "speculative-config": ArgumentSpec("--speculative-config"),
    "tokenizer-mode": ArgumentSpec("--tokenizer-mode", validate=one_of("deepseek_v4")),
    "moe-backend": ArgumentSpec(
        "--moe-backend", validate=one_of("flashinfer_b12x", "marlin")
    ),
    "reasoning-parser": ArgumentSpec(
        "--reasoning-parser",
        validate=one_of("deepseek_v4", "glm45", "nemotron_v3", "poolside_v1", "qwen3"),
    ),
    "reasoning-config": ArgumentSpec("--reasoning-config"),
    "default-chat-template-kwargs": ArgumentSpec("--default-chat-template-kwargs"),
    "generation-config": ArgumentSpec("--generation-config", validate=one_of("vllm")),
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
        require_entrypoint(recipe, ("/opt/vonk/bin/vllm", "serve", "/models"))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
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
            parallelism = topology.get("parallelism")
            node_count = topology.get("node_count")
            if (
                patch is None
                or not isinstance(implementation, Mapping)
                or implementation.get("verified") is not True
                or implementation.get("mechanism") != "vllm-mp"
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
                or parallelism.get("backend") != "mp"
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
                "mp",
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
            frozenset(
                {
                    "CUTE_DSL_ARCH",
                    "DG_JIT_NVCC_COMPILER",
                    "DG_JIT_USE_NVRTC",
                    "DSPARK_MAX_INFLIGHT_PREFILLS",
                    "FLASHINFER_CUDA_ARCH_LIST",
                    "FLASHINFER_DISABLE_VERSION_CHECK",
                    "FLASHINFER_WORKSPACE_BASE",
                    "HF_HUB_DISABLE_XET",
                    "HF_HUB_OFFLINE",
                    "NCCL_CROSS_NIC",
                    "NCCL_CUMEM_ENABLE",
                    "NCCL_DEBUG",
                    "NCCL_IB_ADDR_FAMILY",
                    "NCCL_IB_DISABLE",
                    "NCCL_IB_ROCE_VERSION_NUM",
                    "NCCL_IGNORE_CPU_AFFINITY",
                    "NCCL_NET",
                    "NCCL_NVLS_ENABLE",
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
                    "VLLM_MEMORY_PROFILER_ESTIMATE_CUDAGRAPHS",
                    "VLLM_NO_USAGE_STATS",
                    "VLLM_NVFP4_GEMM_BACKEND",
                    "VLLM_PREFIX_CACHE_RETENTION_INTERVAL",
                    "VLLM_SPARSE_INDEXER_MAX_LOGITS_MB",
                    "VLLM_USE_B12X_MOE",
                    "VLLM_USE_FLASHINFER_SAMPLER",
                    "VLLM_USE_FLASHINFER_MOE_FP4",
                    "VLLM_USE_BREAKABLE_CUDAGRAPH",
                }
            ),
        )
        environment = (*environment, *rank_environment)
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/vllm",
                "serve",
                "/models",
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
        )
