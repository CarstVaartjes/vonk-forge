"""Keep the job ingress strict while all producers share canonical ordering."""

import pytest
from pydantic import ValidationError
from vonk_control.artifact_job_api import ArtifactJobCreate
from vonk_control.compiled_artifact_contract import ArtifactOutputLimits
from vonk_forge_contracts import canonical_media_types


@pytest.mark.parametrize(
    "media",
    [
        ["video/mp4", "application/x-ndjson"],
        ["video/mp4", "video/mp4"],
        ["Video/MP4"],
        ["video/mp4; codecs=h264"],
    ],
)
def test_controller_rejects_noncanonical_output_limits(media: list[str]) -> None:
    with pytest.raises(ValidationError):
        ArtifactOutputLimits.model_validate(
            {
                "max_files": 2,
                "max_file_bytes": 1024,
                "max_total_bytes": 2048,
                "allowed_media_types": media,
            }
        )


def test_controller_accepts_canonical_moss_list_from_shared_producer() -> None:
    limits = ArtifactOutputLimits.model_validate(
        {
            "max_files": 2,
            "max_file_bytes": 1024,
            "max_total_bytes": 2048,
            "allowed_media_types": canonical_media_types(
                ["video/mp4", "application/x-ndjson", "video/mp4"]
            ),
        }
    )
    assert limits.allowed_media_types == ("application/x-ndjson", "video/mp4")
    request = ArtifactJobCreate.model_validate(
        {
            "interface": "artifact-job",
            "parameters": {},
            "inputs": [],
            "timeout_seconds": 60,
            "output_limits": limits.model_dump(mode="json"),
        }
    )
    assert request.output_limits.allowed_media_types == limits.allowed_media_types


def test_job_create_api_rejects_the_original_moss_order() -> None:
    with pytest.raises(
        ValidationError, match="allowed output media types are not canonical"
    ):
        ArtifactJobCreate.model_validate(
            {
                "interface": "artifact-job",
                "parameters": {},
                "inputs": [],
                "timeout_seconds": 60,
                "output_limits": {
                    "max_files": 2,
                    "max_file_bytes": 1024,
                    "max_total_bytes": 2048,
                    "allowed_media_types": ["video/mp4", "application/x-ndjson"],
                },
            }
        )
