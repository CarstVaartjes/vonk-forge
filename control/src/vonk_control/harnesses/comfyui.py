"""Target-driven immutable ComfyUI workflow execution-harness compiler."""

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
    require_literal_arguments,
    require_mime_validator,
    require_source_bundle_identity,
    sha256,
    validate_topology,
    workflow_file,
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
    "workflow": ArgumentSpec("--workflow", validate=workflow_file),
    "workflow-sha256": ArgumentSpec("--workflow-sha256", validate=sha256),
    "output-mime": ArgumentSpec("--output-mime", validate=one_of(*_MIME_TYPES)),
    "seed": ArgumentSpec("--seed", validate=integer(0, 2**63 - 1)),
}


class ComfyUiHarnessCompiler:
    slug = "comfyui"
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
        require_entrypoint(recipe, ("/opt/vonk/bin/comfyui-job",))
        require_source_bundle_identity(recipe)
        require_literal_arguments(
            recipe,
            frozenset({"workflow", "workflow-sha256"}),
            label="ComfyUI workflow identity",
        )
        arguments, parsed = compile_arguments(recipe, parameters, _ARGUMENTS)
        if "--workflow" not in parsed or "--workflow-sha256" not in parsed:
            raise HarnessCompileError("ComfyUI workflow identity is incomplete")
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
            recipe,
            distribution,
            frozenset({"COMFYUI_DISABLE_TELEMETRY", "HF_HUB_OFFLINE"}),
            engine_slug=self.slug,
        )
        return projection(
            slug=self.slug,
            command=(
                "/opt/vonk/bin/comfyui-job",
                *arguments,
                "--output-dir",
                "/outputs",
            ),
            recipe=recipe,
            distribution=distribution,
            environment=environment,
        )
