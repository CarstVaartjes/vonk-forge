"""Target-driven SGLang execution-harness compiler."""

from __future__ import annotations

from collections.abc import Mapping

from .common import (
    ArgumentSpec,
    HarnessCompileError,
    compile_arguments,
    compile_environment,
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
        "--quantization", validate=one_of("awq", "gptq", "fp8", "bitsandbytes")
    ),
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
        del patch
        require_entrypoint(recipe, ("/opt/vonk/bin/sglang-serve",))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        if parsed.get("--model-path") != "/models":
            raise HarnessCompileError("SGLang model path is invalid")
        port = require_openai_interface(recipe)
        validate_topology(
            topology,
            role,
            rank,
            modes=frozenset({"single"}),
        )
        tensor = int(str(parsed.get("--tensor-parallel-size", "1")))
        if tensor != 1:
            raise HarnessCompileError(
                "SGLang single-node harness does not support distributed parallelism"
            )
        environment = compile_environment(
            recipe, frozenset({"NCCL_DEBUG", "HF_HUB_OFFLINE"})
        )
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/sglang-serve",
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
