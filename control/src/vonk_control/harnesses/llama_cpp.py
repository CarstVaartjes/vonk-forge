"""Target-driven llama.cpp execution-harness compiler."""

from __future__ import annotations

from collections.abc import Mapping

from .common import (
    ArgumentSpec,
    HarnessCompileError,
    compile_arguments,
    compile_environment,
    integer,
    model_file,
    one_of,
    projection,
    require_entrypoint,
    require_openai_interface,
    validate_topology,
)
from .contracts import HarnessProjection

_ARGUMENTS = {
    "model": ArgumentSpec("--model", validate=model_file(".gguf")),
    "m": ArgumentSpec("--model", validate=model_file(".gguf")),
    "n-gpu-layers": ArgumentSpec("--n-gpu-layers", validate=integer(0, 999)),
    "ngl": ArgumentSpec("--n-gpu-layers", validate=integer(0, 999)),
    "ctx-size": ArgumentSpec("--ctx-size", validate=integer(1, 10_000_000)),
    "c": ArgumentSpec("--ctx-size", validate=integer(1, 10_000_000)),
    "parallel": ArgumentSpec("--parallel", validate=integer(1, 1024)),
    "host": ArgumentSpec("--host", emit=False, validate=one_of("0.0.0.0")),
    "port": ArgumentSpec("--port", emit=False, validate=integer(1024, 65_535)),
}


class LlamaCppHarnessCompiler:
    slug = "llama-cpp"
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
        require_entrypoint(recipe, ("/opt/vonk/bin/llama-server",))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        if "--model" not in parsed:
            raise HarnessCompileError("llama.cpp requires a GGUF model path")
        port = require_openai_interface(recipe)
        node_count, _parallelism = validate_topology(
            topology, role, rank, modes=frozenset({"single"})
        )
        if node_count != 1:
            raise HarnessCompileError("llama.cpp topology must contain one node")
        environment = compile_environment(
            recipe,
            distribution,
            frozenset({"LLAMA_ARG_N_THREADS"}),
            engine_slug=self.slug,
        )
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/llama-server",
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
