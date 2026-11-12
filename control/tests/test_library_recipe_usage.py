"""A recipe's library capabilities follow its API document, not its Models."""

from __future__ import annotations

import json
from importlib.resources import files

from vonk_control.library_projection.common import _recipe_usage
from vonk_forge_contracts import RecipeApi


def _example(name: str) -> RecipeApi:
    text = files("vonk_forge_contracts").joinpath("examples", name).read_text("utf-8")
    return RecipeApi.model_validate(json.loads(text))


def test_input_understanding_follows_the_api_modalities() -> None:
    # The decision example accepts image but neither video nor audio.
    model = [
        "chat",
        "image-understanding",
        "video-understanding",
        "audio-understanding",
    ]
    assert _recipe_usage(model, _example("recipe-api-decision.json")) == [
        "chat",
        "image-understanding",
    ]


def test_a_route_without_input_modalities_keeps_the_model_display() -> None:
    model = ["text-to-speech", "video-understanding"]
    assert _recipe_usage(model, _example("recipe-api-speech.json")) == model
    assert _recipe_usage(model, None) == model
