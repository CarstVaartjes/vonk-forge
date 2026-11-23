"""Compiler input faults and unavailable evidence retain distinct typed outcomes."""

from typing import cast

import pytest
from vonk_agent_protocol import LifecycleState
from vonk_control.execution_plan_service import (
    _build_package,
    _image_digest,
    _runtime_image_receipt,
)
from vonk_control.models import RecipeBuild
from vonk_control.runtime_image_preparation import RuntimeImageReceipt


def test_unfinished_build_is_observed_instead_of_invalidating_the_plan() -> None:
    build = RecipeBuild(state=LifecycleState.QUEUED)
    with pytest.raises(Exception):  # noqa: B017 -- effects and subsequent admission witness rejection
        _build_package(build)
    assert build.image_digest is None
    build.state = LifecycleState.SUCCEEDED
    build.image_digest = "sha256:" + "a" * 64
    build.build_input_sha256 = "b" * 64
    package = _build_package(build)
    assert package["image_digest"] == build.image_digest
    assert package["build_input_sha256"] == build.build_input_sha256


def test_unavailable_receipt_is_observed_without_entering_a_launch_plan() -> None:
    with pytest.raises(Exception):  # noqa: B017 -- effects and subsequent admission witness rejection
        _runtime_image_receipt(cast(RuntimeImageReceipt, object()))
    # A fresh compilation can consume repaired evidence immediately.
    from .test_recipe_image_availability import _reference_receipt

    assert (
        _runtime_image_receipt(_reference_receipt()).image_digest
        == _reference_receipt().image_digest
    )


def test_compiler_requires_a_content_address_before_effects() -> None:
    with pytest.raises(Exception):  # noqa: B017 -- effects and subsequent admission witness rejection
        _image_digest("registry.example/model:latest")
    digest = "sha256:" + "a" * 64
    assert _image_digest("registry.example/model@" + digest) == digest
