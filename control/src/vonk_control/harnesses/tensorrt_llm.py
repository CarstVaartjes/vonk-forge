"""Target-driven TensorRT-LLM execution-harness compiler."""

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

# Only options documented by NVIDIA for ``trtllm-serve serve`` are admitted.
_ARGUMENTS = {
    "backend": ArgumentSpec("--backend", validate=one_of("pytorch", "_autodeploy")),
    "max-beam-width": ArgumentSpec("--max_beam_width", validate=integer(1, 32)),
    "max-batch-size": ArgumentSpec("--max_batch_size", validate=integer(1, 4096)),
    "max-num-tokens": ArgumentSpec("--max_num_tokens", validate=integer(1, 10_000_000)),
    "max-seq-len": ArgumentSpec("--max_seq_len", validate=integer(1, 10_000_000)),
    "tp-size": ArgumentSpec("--tp_size", validate=integer(1, 64)),
    "pp-size": ArgumentSpec("--pp_size", validate=integer(1, 64)),
    "ep-size": ArgumentSpec("--ep_size", validate=integer(1, 64)),
    "kv-cache-free-gpu-memory-fraction": ArgumentSpec(
        "--kv_cache_free_gpu_memory_fraction", validate=decimal(0.01, 1.0)
    ),
    "log-level": ArgumentSpec(
        "--log_level",
        validate=one_of(
            "internal_error", "error", "warning", "info", "verbose", "debug", "trace"
        ),
    ),
    "host": ArgumentSpec("--host", emit=False, validate=one_of("0.0.0.0")),
    "port": ArgumentSpec("--port", emit=False, validate=integer(1024, 65_535)),
}


class TensorRtLlmHarnessCompiler:
    slug = "tensorrt-llm"
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
        require_entrypoint(recipe, ("/usr/local/bin/trtllm-serve", "serve", "/models"))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        port = require_openai_interface(recipe)
        validate_topology(
            topology,
            role,
            rank,
            modes=frozenset({"single"}),
        )
        configured = (
            int(str(parsed.get("--tp_size", "1"))),
            int(str(parsed.get("--pp_size", "1"))),
            int(str(parsed.get("--ep_size", "1"))),
        )
        if configured != (1, 1, 1):
            raise HarnessCompileError(
                "TensorRT-LLM single-node harness does not support distributed "
                "parallelism or expert parallelism"
            )
        environment = compile_environment(
            recipe, frozenset({"NCCL_DEBUG", "HF_HUB_OFFLINE"})
        )
        return projection(
            slug=self.slug,
            command=(
                "/usr/local/bin/trtllm-serve",
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
