"""A recipe's slot declaration order never decides whether its jobs can run."""

from __future__ import annotations

import hashlib

from vonk_agent_protocol import RecipeJobRunRequest
from vonk_control.agent_jobs import AgentJobService

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    NOW,
    MutableClock,
    _mapping,
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
)


def _slot(slot_id: str, media_type: str, extension: str, *, min_files: int):
    return {
        "id": slot_id,
        "label": slot_id.title(),
        "description": f"The {slot_id} file",
        "media_types": [media_type],
        "extensions": [extension],
        "min_files": min_files,
        "max_files": 1,
        "max_file_bytes": 1024,
        "max_total_bytes": 1024,
    }


def _slots_out_of_id_order(document: dict[str, object]) -> None:
    # The library's job recipes declare slots in reading order (hunyuanocr:
    # "document" then "config"; qwen-image-edit: "prompt" then "image").
    interface = _mapping(document["interfaces"][0])  # type: ignore[index]
    interface["input"] = {
        "required": True,
        "media_types": ["application/json", "image/png"],
        "max_bytes": 4096,
        "slots": [
            _slot("input", "image/png", ".png", min_files=1),
            _slot("config", "application/json", ".json", min_files=0),
        ],
    }
    output = _mapping(interface["output"])
    output["slots"] = [
        _slot("image", "image/png", ".png", min_files=1),
        _slot("archive", "application/zip", ".zip", min_files=0),
    ]


def test_job_of_recipe_with_slots_in_reading_order_is_created_and_claimed(
    tmp_path,
) -> None:
    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path, recipe_transform=_slots_out_of_id_order)
    )
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))
    recipe_operations._agent_jobs = agent_jobs
    assert claim_agent(agent_jobs, node_id, "serial-0") is None

    job = create_artifact_job(
        service,
        **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000301"),
    )
    content = b"png"
    service.put_input(
        job.id,
        name="input.png",
        media_type="image/png",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        content=content,
    )
    service.finalize(job.id)
    service.submit(
        job.id, actor="operator", request_id="00000000-0000-4000-8000-000000000302"
    )

    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    assert isinstance(claim.payload, RecipeJobRunRequest)
    assert str(claim.payload.job_id) == job.id
