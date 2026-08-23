"""Target-driven Diffusers artifact-job execution-harness compiler."""

from __future__ import annotations

from collections.abc import Mapping

from .common import (
    ArgumentSpec,
    compile_arguments,
    compile_environment,
    decimal,
    integer,
    one_of,
    projection,
    require_entrypoint,
    require_job_interface,
    require_mime_validator,
    validate_topology,
)
from .contracts import HarnessProjection

_MIME_TYPES = (
    "image/png",
    "image/jpeg",
    "audio/wav",
    "video/mp4",
    "application/octet-stream",
)
_ARGUMENTS = {
    "pipeline": ArgumentSpec(
        "--pipeline",
        validate=one_of(
            "text-to-image",
            "image-to-image",
            "image-edit",
            "image-to-layers",
            "text-to-audio",
            "text-to-video",
            "artifact",
        ),
    ),
    "output-mime": ArgumentSpec("--output-mime", validate=one_of(*_MIME_TYPES)),
    "num-inference-steps": ArgumentSpec(
        "--num-inference-steps", validate=integer(1, 1000)
    ),
    "guidance-scale": ArgumentSpec("--guidance-scale", validate=decimal(0.0, 100.0)),
    "true-cfg-scale": ArgumentSpec("--true-cfg-scale", validate=decimal(0.0, 100.0)),
    "cfg-normalize": ArgumentSpec("--cfg-normalize", validate=one_of("true", "false")),
    "width": ArgumentSpec("--width", validate=integer(64, 8192)),
    "height": ArgumentSpec("--height", validate=integer(64, 8192)),
    "layers": ArgumentSpec("--layers", validate=integer(1, 8)),
    "resolution": ArgumentSpec("--resolution", validate=integer(256, 4096)),
    "seed": ArgumentSpec("--seed", validate=integer(0, 2**63 - 1)),
}


class DiffusersHarnessCompiler:
    slug = "diffusers"
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
        require_entrypoint(recipe, ("/opt/vonk/bin/diffusers-job",))
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        interface = require_job_interface(
            recipe,
            frozenset({"image-job", "audio-job", "video-job", "artifact-job"}),
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
            recipe, frozenset({"HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"})
        )
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/diffusers-job",
                *arguments,
                "--output-dir",
                "/outputs",
            ),
            recipe=recipe,
            distribution=distribution,
            environment=environment,
        )
