"""A recipe whose artifact contract cannot compile is refused, never "retry"."""

from __future__ import annotations

import pytest
from vonk_agent_protocol import InvalidRequestReason, UnknownOutcomeError
from vonk_control.artifact_jobs import ArtifactJobInvalid

from .test_artifact_jobs import (
    _mapping,
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
)


def _required_input_without_required_slot(document: dict[str, object]) -> None:
    # The input is required but no slot requires a file: the revision's
    # contract can never compile.
    interface = _mapping(document["interfaces"][0])  # type: ignore[index]
    interface["input"] = {
        "required": True,
        "media_types": ["image/png"],
        "max_bytes": 4096,
        "slots": [
            {
                "id": "input",
                "label": "Input",
                "description": "The input image",
                "media_types": ["image/png"],
                "extensions": [".png"],
                "min_files": 0,
                "max_files": 1,
                "max_file_bytes": 1024,
                "max_total_bytes": 1024,
            }
        ],
    }


def test_job_create_against_uncompilable_recipe_contract_is_refused(
    tmp_path,
) -> None:
    _sessions, _operations, _queue, service, run_id, _node_id = (
        running_artifact_service(
            tmp_path, recipe_transform=_required_input_without_required_slot
        )
    )
    request = artifact_create_request(run_id, "00000000-0000-4000-8000-000000000311")

    with pytest.raises(ArtifactJobInvalid) as refused:
        create_artifact_job(service, **request)

    assert not isinstance(refused.value, UnknownOutcomeError)
    assert refused.value.typed_reason is InvalidRequestReason.UNSUPPORTED
