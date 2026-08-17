"""Target-driven DS4 engine execution-harness compiler."""

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
    "draft-model": ArgumentSpec("--mtp", validate=model_file(".gguf")),
    "ctx-size": ArgumentSpec("--ctx", validate=integer(1, 10_000_000)),
    "batch-size": ArgumentSpec("--batched-session", validate=integer(1, 65_536)),
    "host": ArgumentSpec("--host", emit=False, validate=one_of("0.0.0.0")),
    "port": ArgumentSpec("--port", emit=False, validate=integer(1024, 65_535)),
}


class Ds4HarnessCompiler:
    """Compile DS4 as an engine; model identity never selects this class."""

    slug = "ds4"
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
        require_entrypoint(recipe, ("/opt/vonk/bin/ds4-serve",))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        if "--model" not in parsed or "--mtp" not in parsed:
            raise HarnessCompileError("DS4 requires target and draft model paths")
        port = require_openai_interface(recipe)
        validate_topology(
            topology,
            role,
            rank,
            modes=frozenset({"single"}),
        )
        environment = compile_environment(
            recipe, frozenset({"DS4_LOG_LEVEL", "HF_HUB_OFFLINE"})
        )
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/ds4-serve",
                *arguments,
                "--dspark",
                "--cuda",
                "--host",
                "0.0.0.0",
                "--port",
                str(port),
            ),
            recipe=recipe,
            distribution=distribution,
            environment=environment,
        )
