from __future__ import annotations

import pytest
from vonk_control.interface_adapters import (
    InterfaceAdapterError,
    interface_adapter,
)


class CallbackString(str):
    def __new__(cls, value: str):
        instance = super().__new__(cls, value)
        instance.hash_called = False
        return instance

    def __hash__(self) -> int:
        self.hash_called = True
        return str.__hash__(self)


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


def test_interface_name_rejects_a_string_subclass_before_using_it() -> None:
    name = CallbackString("openai")

    with pytest.raises(InterfaceAdapterError, match="unknown interface adapter"):
        interface_adapter(name)

    assert name.hash_called is False
