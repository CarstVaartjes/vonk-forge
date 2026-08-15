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
    "quantization": ArgumentSpec(
        "--quantization", validate=one_of("awq", "gptq", "fp8", "bitsandbytes")
    ),
    "dtype": ArgumentSpec(
        "--dtype", validate=one_of("auto", "float16", "bfloat16", "float32")
    ),
    "kv-cache-dtype": ArgumentSpec(
        "--kv-cache-dtype",
        validate=one_of("auto", "fp8", "fp8_e4m3", "fp8_e5m2"),
    ),
    "served-model-name": ArgumentSpec("--served-model-name"),
    "tool-call-parser": ArgumentSpec("--tool-call-parser"),
    "enable-auto-tool-choice": ArgumentSpec(
        "--enable-auto-tool-choice", takes_value=False
    ),
    "enable-prefix-caching": ArgumentSpec("--enable-prefix-caching", takes_value=False),
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
        del patch
        require_entrypoint(recipe, ("/opt/vonk/bin/vllm", "serve", "/models"))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        port = require_openai_interface(recipe)
        validate_topology(
            topology,
            role,
            rank,
            modes=frozenset({"single"}),
        )
        tensor = int(str(parsed.get("--tensor-parallel-size", "1")))
        pipeline = int(str(parsed.get("--pipeline-parallel-size", "1")))
        if tensor != 1 or pipeline != 1:
            raise HarnessCompileError(
                "vLLM single-node harness does not support distributed parallelism"
            )
        environment = compile_environment(
            recipe, frozenset({"NCCL_DEBUG", "HF_HUB_OFFLINE"})
        )
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/vllm",
                "serve",
                "/models",
                *arguments,
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
            ),
            recipe=recipe,
            distribution=distribution,
            environment=environment,
        )
