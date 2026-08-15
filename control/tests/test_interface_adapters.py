from __future__ import annotations

import pytest
from vonk_control.interface_adapters import (
    InterfaceAdapterError,
    interface_adapter,
)


@pytest.mark.parametrize(
    "name", ["image-job", "audio-job", "video-job", "mesh-job", "artifact-job"]
)
def test_job_interfaces_publish_artifacts(name: str) -> None:
    adapter = interface_adapter(name)

    assert adapter.publication == "artifact"
    assert adapter.readiness({"ready": True}) is True
    assert adapter.invocation_request({"job": "submit"}) == {"job": "submit"}
    assert adapter.evidence({"artifact": "result.json"}) == {"artifact": "result.json"}
    assert adapter.withdrawal() == {"publication": "artifact", "withdrawn": True}


def test_openai_is_the_only_litellm_interface() -> None:
    assert interface_adapter("openai").publication == "litellm"


def test_video_jobs_do_not_publish_to_litellm() -> None:
    assert interface_adapter("video-job").publication == "artifact"


def test_unknown_interface_fails_closed() -> None:
    with pytest.raises(InterfaceAdapterError, match="unknown interface adapter"):
        interface_adapter("legacy-openai")
