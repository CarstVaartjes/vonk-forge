from __future__ import annotations

import json

import pytest

from cluster_profiles.fleet_qualification import (
    QualificationObservationUnknown,
    QualificationQualityFailure,
    _assert_service_response,
)

LEAK: list[object] = [
    {"kind": "raw.not-contains", "values": ["<|", "<think>", "</think>"]}
]


def _chat(message: dict[str, object], **choice: object) -> dict[str, object]:
    return {
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, **choice}],
    }


def _check(response: dict[str, object]) -> None:
    _assert_service_response(response, json.dumps(response).encode(), LEAK)


def test_a_stop_token_named_in_server_metadata_is_not_a_leak() -> None:
    """SGLang names the stop it matched; the answer itself is clean (phi-4)."""
    _check(
        _chat(
            {
                "role": "assistant",
                "content": "The product of 17 multiplied by 19 is 323.",
                "reasoning_content": None,
                "tool_calls": None,
            },
            finish_reason="stop",
            matched_stop="<|end|>",
            provider_specific_fields={"matched_stop": "<|end|>"},
        )
    )


@pytest.mark.parametrize(
    "message",
    [
        {"content": "323<|im_end|>"},
        {"content": "<think>17 times 19</think>323"},
        {"content": "323", "reasoning_content": "<think>17 times 19"},
        {"content": [{"type": "text", "text": "323<|end|>"}]},
        {
            "content": None,
            "tool_calls": [{"function": {"name": "f", "arguments": "<|im_end|>"}}],
        },
    ],
)
def test_a_token_in_the_text_the_user_receives_still_fails(
    message: dict[str, object],
) -> None:
    with pytest.raises(QualificationQualityFailure):
        _check(_chat({"role": "assistant", **message}))


def test_completion_text_is_checked_too() -> None:
    with pytest.raises(QualificationQualityFailure):
        _check({"choices": [{"text": "323</think>"}]})


def test_a_body_that_is_not_a_json_object_is_checked_raw() -> None:
    with pytest.raises(QualificationQualityFailure):
        _assert_service_response(None, b"323<|im_end|> not json", LEAK)
    _assert_service_response(None, b"323 and nothing else", LEAK)


REASONING = "choices.0.message.reasoning_content"
NO_REASONING: list[object] = [{"kind": "path.empty", "path": REASONING}]
SOME_REASONING: list[object] = [{"kind": "path.nonempty", "path": REASONING}]


def _assert(response: dict[str, object], assertions: list[object]) -> None:
    _assert_service_response(response, json.dumps(response).encode(), assertions)


@pytest.mark.parametrize(
    "reasoning", [{}, {"reasoning_content": None}, {"reasoning_content": ""}]
)
def test_thinking_off_accepts_reasoning_absent_null_or_empty(
    reasoning: dict[str, object],
) -> None:
    _assert(_chat({"content": "391", **reasoning}), NO_REASONING)


def test_thinking_off_refuses_reasoning_and_a_missing_parent() -> None:
    with pytest.raises(QualificationQualityFailure):
        _assert(_chat({"content": "391", "reasoning_content": "hmm"}), NO_REASONING)
    with pytest.raises(QualificationObservationUnknown, match="is missing"):
        _assert({"choices": []}, NO_REASONING)


@pytest.mark.parametrize(
    ("reasoning", "failure"),
    [
        ({}, QualificationObservationUnknown),
        ({"reasoning_content": None}, QualificationQualityFailure),
        ({"reasoning_content": ""}, QualificationQualityFailure),
    ],
)
def test_thinking_on_still_requires_reasoning(
    reasoning: dict[str, object], failure: type[Exception]
) -> None:
    with pytest.raises(failure):
        _assert(_chat({"content": "391", **reasoning}), SOME_REASONING)
    _assert(_chat({"content": "391", "reasoning_content": "17*23"}), SOME_REASONING)
