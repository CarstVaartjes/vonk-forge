"""A package carries every file its serving checks stage, whatever the transport."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from vonk_control.recipe_packages import _validate_package_paths
from vonk_forge_contracts import read_recipe

FIXTURE = "qualification/fixtures/jfk-inaugural-10s.wav"


def _recipe():
    document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-audio-serving.json")
        .read_text(encoding="utf-8")
    )
    return read_recipe(document)


def _validate(extra: set[str]) -> None:
    recipe = _recipe()
    build = recipe.execution.build
    paths = {build.context.path, build.dockerfile, *extra}
    inputs = [
        {
            "kind": "oci-image",
            "reference": f"{build.base_image.repository}@sha256:{build.base_image.digest}",
        }
    ]
    _validate_package_paths(recipe, paths, inputs)


def test_the_staged_audio_file_of_a_multipart_check_must_be_in_the_package() -> None:
    # The multipart file is request.file.fixture; a closure that only read
    # request.fixture (a job check's field) never saw it.
    with pytest.raises(ValueError, match=FIXTURE):
        _validate(set())
    _validate({FIXTURE})
