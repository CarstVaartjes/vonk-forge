"""Validation of the Controller's artifact job documents."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from cluster_profiles.cli_artifact_jobs.values import _UUID
from cluster_profiles.cli_states import ARTIFACT_JOB_STATES, preparation
from cluster_profiles.control_client.errors import ControlMalformedResponse
from cluster_profiles.control_client.schema import validate_control_document


def _job(
    value: Mapping[str, object],
    *,
    expected_id: str | None = None,
    expected_run: str | None = None,
    expected_submit_request_id: str | None = None,
) -> dict[str, object]:
    job = validate_control_document("ArtifactJobResponse", dict(value))
    identifier = job.get("id")
    run_id = job.get("run_id")
    if not isinstance(identifier, str) or _UUID.fullmatch(identifier) is None:
        raise ControlMalformedResponse("artifact job response has an invalid identity")
    if expected_id is not None and identifier != expected_id:
        raise ControlMalformedResponse("artifact job response identifies another job")
    if expected_run is not None and run_id != expected_run:
        raise ControlMalformedResponse("artifact job response identifies another run")
    operation_id = job.get("operation_id")
    submit_request_id = job.get("submit_request_id")
    if (operation_id is None) != (submit_request_id is None):
        raise ControlMalformedResponse("artifact job submission identity is incomplete")
    if (
        expected_submit_request_id is not None
        and submit_request_id != expected_submit_request_id
    ):
        raise ControlMalformedResponse(
            "artifact job response identifies another submit request"
        )
    return job


def _same_job(value: Mapping[str, object], expected_id: str) -> None:
    _job(value, expected_id=expected_id)


def _declared_files(value: object, label: str) -> dict[str, dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        raise ControlMalformedResponse(f"artifact job {label} are invalid")
    result: dict[str, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ControlMalformedResponse(f"artifact job {label} are invalid")
        name = item.get("name")
        if not isinstance(name, str) or name in result:
            raise ControlMalformedResponse(f"artifact job {label} have invalid names")
        result[name] = dict(item)
    return result


def _match_input_binding(
    job: Mapping[str, object],
    create: Mapping[str, object] | None,
    declarations: list[dict[str, object]],
) -> None:
    actual = job.get("input_declarations")
    expected_by_name = {cast(str, item["name"]): item for item in declarations}
    if not isinstance(actual, (list, tuple)):
        raise ControlMalformedResponse("artifact job input declarations are invalid")
    actual_by_name = {
        name: {key: value for key, value in item.items() if key != "media_type"}
        for name, item in _declared_files(actual, "input declarations").items()
    }
    if actual_by_name != expected_by_name:
        raise ControlMalformedResponse(
            "artifact create receipt differs from the requested declarations"
        )
    if create is not None:
        for key, value in (
            ("interface", create.get("interface")),
            ("output_limits", create.get("output_limits")),
            ("timeout_seconds", create.get("timeout_seconds")),
        ):
            if job.get(key) != value:
                raise ControlMalformedResponse(
                    f"artifact create receipt differs from the requested {key}"
                )


def _create_receipt(
    value: Mapping[str, object],
    *,
    expected_run: str,
    expected_body: Mapping[str, object],
) -> dict[str, object]:
    job = _job(value, expected_run=expected_run)
    expected_inputs = expected_body.get("inputs")
    if not isinstance(expected_inputs, list):
        raise ControlMalformedResponse(
            "artifact create receipt cannot be compared with its request"
        )
    _match_input_binding(job, expected_body, expected_inputs)
    if preparation(job) is None and job["state"] not in ARTIFACT_JOB_STATES:
        raise ControlMalformedResponse("artifact create receipt has an invalid state")
    return job
