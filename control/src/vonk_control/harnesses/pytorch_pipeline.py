"""Target-driven signed-bundle PyTorch pipeline execution-harness compiler."""

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
    require_job_interface,
    require_mime_validator,
    require_source_bundle_identity,
    source_bundle_file,
    validate_topology,
)
from .contracts import HarnessProjection

_MIME_TYPES = (
    "image/png",
    "image/jpeg",
    "audio/wav",
    "video/mp4",
    "model/gltf-binary",
    "application/octet-stream",
    "application/zip",
)
_ARGUMENTS = {
    "entrypoint": ArgumentSpec("--entrypoint", validate=source_bundle_file),
    "output-mime": ArgumentSpec("--output-mime", validate=one_of(*_MIME_TYPES)),
    "timeout-seconds": ArgumentSpec("--timeout-seconds", validate=integer(1, 3600)),
    "seed": ArgumentSpec("--seed", validate=integer(0, 2**63 - 1)),
}


class PytorchPipelineHarnessCompiler:
    slug = "pytorch-pipeline"
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
        require_entrypoint(recipe, ("/opt/vonk/bin/pytorch-pipeline",))
        require_source_bundle_identity(recipe)
        runtime = recipe.get("runtime")
        recipe_arguments = (
            runtime.get("arguments") if isinstance(runtime, Mapping) else None
        )
        bundle_entrypoint = (
            next(
                (
                    item.get("value")
                    for item in recipe_arguments
                    if isinstance(item, Mapping) and item.get("name") == "entrypoint"
                ),
                None,
            )
            if type(recipe_arguments) is list
            else None
        )
        if type(bundle_entrypoint) is not str or not source_bundle_file(
            bundle_entrypoint
        ):
            raise HarnessCompileError(
                "PyTorch entrypoint must be inside the signed source bundle"
            )
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        if "--entrypoint" not in parsed:
            raise HarnessCompileError(
                "PyTorch entrypoint must be inside the signed source bundle"
            )
        interface = require_job_interface(
            recipe,
            frozenset(
                {
                    "image-job",
                    "audio-job",
                    "video-job",
                    "mesh-job",
                    "artifact-job",
                }
            ),
        )
        output_mime = str(parsed.get("--output-mime", ""))
        require_mime_validator(recipe, interface, output_mime)
        validate_topology(
            topology,
            role,
            rank,
            modes=frozenset({"single", "data_parallel"}),
        )
        environment = compile_environment(
            recipe,
            distribution,
            frozenset({"HF_HUB_OFFLINE", "PYTORCH_CUDA_ALLOC_CONF"}),
            engine_slug=self.slug,
        )
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/pytorch-pipeline",
                *arguments,
                "--output-dir",
                "/outputs",
            ),
            recipe=recipe,
            distribution=distribution,
            environment=environment,
        )
