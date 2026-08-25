"""Target-driven SGLang execution-harness compiler."""

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
    "model-path": ArgumentSpec("--model-path", validate=one_of("/models")),
    "tp": ArgumentSpec("--tensor-parallel-size", validate=integer(1, 16)),
    "tp-size": ArgumentSpec("--tensor-parallel-size", validate=integer(1, 16)),
    "tensor-parallel-size": ArgumentSpec(
        "--tensor-parallel-size", validate=integer(1, 16)
    ),
    "context-length": ArgumentSpec("--context-length", validate=integer(1, 10_000_000)),
    "quantization": ArgumentSpec(
        "--quantization",
        validate=one_of("awq", "gptq", "fp8", "modelopt_fp4", "bitsandbytes"),
    ),
    "attention-backend": ArgumentSpec("--attention-backend", validate=one_of("triton")),
    "page-size": ArgumentSpec("--page-size", validate=integer(1, 1024)),
    "fp4-gemm-backend": ArgumentSpec("--fp4-gemm-backend", validate=one_of("marlin")),
    "moe-runner-backend": ArgumentSpec(
        "--moe-runner-backend", validate=one_of("marlin")
    ),
    "mamba-radix-cache-strategy": ArgumentSpec(
        "--mamba-radix-cache-strategy", validate=one_of("extra_buffer")
    ),
    "mem-fraction-static": ArgumentSpec(
        "--mem-fraction-static", validate=decimal(0.01, 1.0)
    ),
    "swa-full-tokens-ratio": ArgumentSpec(
        "--swa-full-tokens-ratio", validate=decimal(0.0, 1.0)
    ),
    "mamba-full-memory-ratio": ArgumentSpec(
        "--mamba-full-memory-ratio", validate=decimal(0.0, 1.0)
    ),
    "enable-multimodal": ArgumentSpec("--enable-multimodal", takes_value=False),
    "disable-prefill-cuda-graph": ArgumentSpec(
        "--disable-prefill-cuda-graph", takes_value=False
    ),
    "reasoning-parser": ArgumentSpec("--reasoning-parser", validate=one_of("inkling")),
    "tool-call-parser": ArgumentSpec("--tool-call-parser", validate=one_of("inkling")),
    "served-model-name": ArgumentSpec("--served-model-name"),
    "trust-remote-code": ArgumentSpec("--trust-remote-code", takes_value=False),
    "host": ArgumentSpec("--host", emit=False, validate=one_of("0.0.0.0")),
    "port": ArgumentSpec("--port", emit=False, validate=integer(1024, 65_535)),
}


class SglangHarnessCompiler:
    slug = "sglang"
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
        require_entrypoint(recipe, ("/opt/vonk/bin/sglang-serve",))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        if parsed.get("--model-path") != "/models":
            raise HarnessCompileError("SGLang model path is invalid")
        port = require_openai_interface(recipe)
        validate_topology(
            topology,
            role,
            rank,
            modes=frozenset({"single", "distributed"}),
        )
        tensor = int(str(parsed.get("--tensor-parallel-size", "1")))
        mode = topology.get("mode")
        distributed: tuple[str, ...] = ()
        rank_environment: tuple[tuple[str, str], ...] = ()
        if mode == "distributed":
            capability = distribution.get("capabilities")
            implementation = (
                capability.get("distributed_sglang")
                if isinstance(capability, Mapping)
                else None
            )
            parallelism = topology.get("parallelism")
            node_count = topology.get("node_count")
            if (
                patch is None
                or not isinstance(implementation, Mapping)
                or implementation.get("verified") is not True
                or implementation.get("mechanism") != "sglang-native"
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
                or parallelism.get("backend") != "native"
                or tensor != parallelism.get("tensor")
            ):
                raise HarnessCompileError(
                    "SGLang distributed topology requires a verified distributed SGLang distribution"
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
                    "SGLang distributed topology requires a complete launch contract"
                )
            rank_environment = tuple(
                (name, str(fabric_environment[name]))
                for name in sorted(expected_environment)
            )
            distributed = (
                "--nnodes",
                str(node_count),
                "--node-rank",
                str(rank),
                "--dist-init-addr",
                "VONK_MASTER_ADDR:VONK_MASTER_PORT",
            )
        elif tensor != 1:
            raise HarnessCompileError(
                "SGLang single-node harness does not support distributed parallelism"
            )
        environment = compile_environment(
            recipe,
            distribution,
            frozenset(
                {"NCCL_DEBUG", "HF_HUB_OFFLINE", "SGLANG_ENABLE_UNIFIED_RADIX_TREE"}
            ),
        )
        environment = (*environment, *rank_environment)
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/sglang-serve",
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
