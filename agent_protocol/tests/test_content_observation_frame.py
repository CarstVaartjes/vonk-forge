"""Python and native helper consume the same read-only inspection frame."""

from pathlib import Path

from vonk_agent_protocol import RecipeRunInspectionRequest, canonical_message


def test_content_observation_frame_round_trips_with_unused_fields_omitted():
    # Wrong implementation: the optional mode disappears or acquires null
    # defaults, so the native helper cannot parse the agent's canonical bytes.
    raw = (
        (Path(__file__).parents[1] / "fixtures/content-preparation-inspection.json")
        .read_bytes()
        .strip()
    )
    frame = RecipeRunInspectionRequest.model_validate_json(raw)
    assert frame.content_observation is True
    assert canonical_message(frame) == raw
