"""CLI removal consent is bound to the current Controller-owned review."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable, Mapping

import pytest

from cluster_profiles import cli, controller_cli
from cluster_profiles.cli_render import render_payload
from cluster_profiles.control_client import (
    ControlConflict,
    ControlForbidden,
    ControlMalformedResponse,
    ControlNotFound,
    ControlTransportError,
)

_REQUEST_KEY = "00000000-0000-4000-8000-000000000931"
_MODEL_DIGEST = "a" * 64
_REVIEW_DIGEST = "b" * 64


def _review(
    resource_kind: str,
    selector: str,
    *,
    digest: str = _REVIEW_DIGEST,
    with_model: bool | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "action": "remove",
        "resource_kind": resource_kind,
        "selector": selector,
        "target_identity": (
            _MODEL_DIGEST
            if resource_kind == "model"
            else "revision-00000000-0000-4000-8000-000000000001"
        ),
        "with_model": with_model,
        "assets": [
            {
                "kind": "model-object",
                "sha256": _MODEL_DIGEST,
                "expected_bytes": 20,
                "availability": "partial",
                "available_bytes": 7,
                "disposition": "remove",
            }
        ],
        "references": [
            {
                "classification": "saved-reference",
                "asset_kind": "model-object",
                "asset_sha256": _MODEL_DIGEST,
                "owner_kind": "profile",
                "owner_id": "profile-4",
                "state": "current",
                "detail": "Saved profile selects this model.",
                "reason": "The profile remains an authoritative reference.",
            }
        ],
        "active_work": [
            {
                "classification": "active-work",
                "asset_kind": "model-object",
                "asset_sha256": _MODEL_DIGEST,
                "owner_kind": "download",
                "owner_id": "operation-5",
                "state": "running",
                "detail": "An active transfer owns this object.",
                "reason": "The transfer has not settled.",
            }
        ],
        "blockers": [
            {
                "code": "cache.asset.partial",
                "detail": "The managed object is incomplete.",
                "retryable": True,
                "recovery_actions": ["inspect-owner"],
            }
        ],
        "observed_at": "2026-09-24T10:00:00Z",
        "review_digest": digest,
    }


def _unblocked_review(review: dict[str, object]) -> dict[str, object]:
    return {**review, "references": [], "active_work": [], "blockers": []}


class _FakeController:
    request_timeout_seconds = 2.0

    def __init__(
        self,
        review: dict[str, object],
        *,
        existing: dict[str, object] | None = None,
        before_post: Callable[[], None] | None = None,
        review_error: BaseException | None = None,
    ) -> None:
        self.review = review
        self.existing = existing
        self.before_post = before_post
        self.review_error = review_error
        self.calls: list[tuple[str, str, Mapping[str, object] | None, object]] = []
        self.accepted: dict[str, object] | None = None

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        query: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        del timeout_seconds
        self.calls.append((method, path, payload, query))
        if path.endswith("/remove-review"):
            if self.review_error is not None:
                raise self.review_error
            return self.review
        if method == "GET" and "/requests/" in path:
            if self.accepted is not None:
                return self.accepted
            if self.existing is not None:
                return self.existing
            raise ControlNotFound(404, "request not found")
        if method == "POST" and path.endswith("/remove"):
            if self.before_post is not None:
                self.before_post()
            assert payload is not None
            if path.startswith("/api/model/"):
                self.accepted = {
                    "action": "remove",
                    "selector": "publisher/model",
                    "request_key": payload["request_key"],
                    "model_content_sha256": payload["model_content_sha256"],
                    "review_digest": payload["review_digest"],
                    "operation_id": "model-operation-9",
                    "state": "queued",
                }
            else:
                self.accepted = {
                    "action": "remove",
                    "selector": "publisher/recipe",
                    "request_key": payload["request_key"],
                    "with_model": payload["with_model"],
                    "review_digest": payload["review_digest"],
                    "operation_id": "recipe-operation-9",
                    "state": "queued",
                }
            return self.accepted
        raise AssertionError(f"unexpected Controller request: {method} {path}")

    def profile_endpoints(
        self, number: int, alias: str | None = None
    ) -> controller_cli.FleetProfileEndpointsView:
        raise AssertionError(f"unexpected endpoint lookup: {number} {alias}")


class _TTYInput(io.StringIO):
    def isatty(self) -> bool:
        return True


class _TTYOutput(io.StringIO):
    def isatty(self) -> bool:
        return True


def _parse(*argv: str):
    return cli._parser().parse_args(argv)


def _accept_response_contracts(monkeypatch: pytest.MonkeyPatch) -> None:
    original = controller_cli.validate_control_document

    def validate(name: str, document: object) -> dict[str, object]:
        if name in {
            "CacheRemovalReview",
            "ModelCacheOperatorResponse",
            "RecipeOperatorResponse",
        }:
            assert isinstance(document, dict)
            return document
        return original(name, document)

    monkeypatch.setattr(controller_cli, "validate_control_document", validate)


@pytest.fixture(autouse=True)
def _fake_transport_reviews_are_contract_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FakeController supplies reviews after the HTTP contract boundary."""
    original = controller_cli.validate_control_document

    def validate(name: str, document: object) -> dict[str, object]:
        if name == "CacheRemovalReview":
            assert isinstance(document, dict)
            return document
        return original(name, document)

    monkeypatch.setattr(controller_cli, "validate_control_document", validate)


def test_json_review_command_is_read_only_and_returns_exact_owner_document(
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _review("model", "publisher/model")
    client = _FakeController(review)

    status = cli.main(
        ("--json", "model", "remove", "publisher/model", "--review"),
        control_client=client,
    )

    assert status == 0
    assert json.loads(capsys.readouterr().out) == review
    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("GET", "/api/model/publisher%2Fmodel/remove-review")
    ]


def test_noninteractive_yes_without_digest_refuses_before_controller_read() -> None:
    client = _FakeController(_review("model", "publisher/model"))
    args = _parse("--json", "model", "remove", "publisher/model", "--yes")

    with pytest.raises(ValueError, match="--review-digest SHA256 --yes"):
        controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert client.calls == []


def test_noninteractive_digest_without_yes_refuses_before_controller_read() -> None:
    client = _FakeController(_review("model", "publisher/model"))
    args = _parse(
        "--json",
        "model",
        "remove",
        "publisher/model",
        "--review-digest",
        _REVIEW_DIGEST,
    )

    with pytest.raises(ValueError, match="requires --yes"):
        controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert client.calls == []


def test_interactive_review_is_rendered_before_prompt_and_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = _TTYOutput()

    def review_precedes_post() -> None:
        assert rendered.getvalue().index("Review digest: " + _REVIEW_DIGEST) < (
            rendered.getvalue().index("[y/N]")
        )

    client = _FakeController(
        _unblocked_review(_review("model", "publisher/model")),
        before_post=review_precedes_post,
    )
    monkeypatch.setattr(sys, "stdin", _TTYInput("yes\n"))
    monkeypatch.setattr(sys, "stderr", rendered)
    monkeypatch.setattr(sys, "stdout", rendered)
    _accept_response_contracts(monkeypatch)
    args = _parse("model", "remove", "publisher/model", "--detach")

    result = controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert result["review_digest"] == _REVIEW_DIGEST
    assert client.accepted is not None
    assert client.accepted["model_content_sha256"] == _MODEL_DIGEST
    assert client.accepted["review_digest"] == _REVIEW_DIGEST
    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("GET", "/api/model/publisher%2Fmodel/remove-review"),
        ("POST", "/api/model/publisher%2Fmodel/remove"),
    ]
    transcript = rendered.getvalue()
    assert "Readiness: partial" in transcript


def test_review_presentation_keeps_owner_findings_and_blockers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    render_payload(_review("model", "publisher/model"), "model", action="preview")
    text = capsys.readouterr().out

    assert "Saved references:" in text
    assert "profile profile-4" in text
    assert "Active work:" in text
    assert "download operation-5" in text
    assert "Blocker: cache.asset.partial" in text
    assert "Retryable: yes" in text
    assert "Next: inspect-owner" in text


def test_stale_scripted_review_refuses_without_post_or_automatic_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeController(
        _unblocked_review(_review("model", "publisher/model", digest="c" * 64))
    )
    args = _parse(
        "--json",
        "model",
        "remove",
        "publisher/model",
        "--review-digest",
        _REVIEW_DIGEST,
        "--yes",
    )

    with pytest.raises(ControlConflict, match="review changed"):
        controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("GET", "/api/model/publisher%2Fmodel/remove-review")
    ]


def test_wrong_selector_review_refuses_before_consent_or_post() -> None:
    client = _FakeController(_review("model", "another/model"))
    args = _parse(
        "--json",
        "model",
        "remove",
        "publisher/model",
        "--review-digest",
        _REVIEW_DIGEST,
        "--yes",
    )

    with pytest.raises(ControlMalformedResponse, match="another selector"):
        controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("GET", "/api/model/publisher%2Fmodel/remove-review")
    ]


@pytest.mark.parametrize(
    "error",
    [
        ControlForbidden(403, "review permission denied", code="cache.review.denied"),
        ControlTransportError("review request timed out"),
    ],
)
def test_review_get_preserves_denial_and_timeout_classification(
    error: ControlForbidden | ControlTransportError,
) -> None:
    client = _FakeController(_review("model", "publisher/model"), review_error=error)
    args = _parse(
        "--json",
        "model",
        "remove",
        "publisher/model",
        "--review-digest",
        _REVIEW_DIGEST,
        "--yes",
    )

    with pytest.raises(type(error)) as caught:
        controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert caught.value is error
    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("GET", "/api/model/publisher%2Fmodel/remove-review")
    ]


def test_blocked_review_refuses_without_prompt_or_post_but_remains_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = _TTYOutput()
    client = _FakeController(_review("model", "publisher/model"))
    monkeypatch.setattr(sys, "stdin", _TTYInput("yes\n"))
    monkeypatch.setattr(sys, "stderr", rendered)
    monkeypatch.setattr(sys, "stdout", rendered)
    args = _parse("model", "remove", "publisher/model")

    with pytest.raises(
        ControlConflict, match="cache.asset.partial: The managed object is incomplete"
    ):
        controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert "[y/N]" not in rendered.getvalue()
    assert "Blocker: cache.asset.partial" in rendered.getvalue()
    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("GET", "/api/model/publisher%2Fmodel/remove-review")
    ]

    inspect_client = _FakeController(_review("model", "publisher/model"))
    result = cli.main(
        ("--json", "model", "remove", "publisher/model", "--review"),
        control_client=inspect_client,
    )
    assert result == 0
    assert [method for method, _, _, _ in inspect_client.calls] == ["GET"]


def test_same_key_replay_precedes_review_lookup_and_uses_stored_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt: dict[str, object] = {
        "action": "remove",
        "selector": "publisher/model",
        "request_key": _REQUEST_KEY,
        "model_content_sha256": _MODEL_DIGEST,
        "review_digest": _REVIEW_DIGEST,
        "operation_id": "model-operation-9",
        "state": "queued",
    }
    client = _FakeController(_review("model", "publisher/model"), existing=receipt)
    args = _parse(
        "--json",
        "model",
        "remove",
        "publisher/model",
        "--request-key",
        _REQUEST_KEY,
        "--review-digest",
        _REVIEW_DIGEST,
        "--yes",
        "--detach",
    )
    _accept_response_contracts(monkeypatch)

    result = controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert result == receipt
    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("GET", f"/api/model/requests/{_REQUEST_KEY}")
    ]


def test_same_key_cannot_be_replayed_with_a_different_review_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    receipt: dict[str, object] = {
        "action": "remove",
        "selector": "publisher/model",
        "request_key": _REQUEST_KEY,
        "model_content_sha256": _MODEL_DIGEST,
        "review_digest": "c" * 64,
        "operation_id": "model-operation-9",
        "state": "queued",
    }
    client = _FakeController(_review("model", "publisher/model"), existing=receipt)
    args = _parse(
        "--json",
        "model",
        "remove",
        "publisher/model",
        "--request-key",
        _REQUEST_KEY,
        "--review-digest",
        _REVIEW_DIGEST,
        "--yes",
    )
    _accept_response_contracts(monkeypatch)

    with pytest.raises(ControlMalformedResponse, match="another request"):
        controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("GET", f"/api/model/requests/{_REQUEST_KEY}")
    ]


def test_recipe_review_passes_explicit_keep_choice_in_query() -> None:
    review = _review("recipe", "publisher/recipe", with_model=False)
    client = _FakeController(review)
    args = _parse(
        "--json",
        "recipe",
        "remove",
        "publisher/recipe",
        "--keep-model",
        "--review",
    )

    result = controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert result == review
    assert client.calls == [
        (
            "GET",
            "/api/recipe/publisher%2Frecipe/remove-review",
            None,
            {"with_model": False},
        )
    ]


def test_scripted_recipe_remove_binds_digest_and_explicit_model_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _unblocked_review(_review("recipe", "publisher/recipe", with_model=False))
    client = _FakeController(review)
    _accept_response_contracts(monkeypatch)
    args = _parse(
        "--json",
        "recipe",
        "remove",
        "publisher/recipe",
        "--keep-model",
        "--review-digest",
        _REVIEW_DIGEST,
        "--yes",
        "--detach",
    )

    result = controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert result["review_digest"] == _REVIEW_DIGEST
    assert client.calls == [
        (
            "GET",
            "/api/recipe/publisher%2Frecipe/remove-review",
            None,
            {"with_model": False},
        ),
        (
            "POST",
            "/api/recipe/publisher%2Frecipe/remove",
            {
                "schema_version": 2,
                "request_key": _REQUEST_KEY,
                "with_model": False,
                "review_digest": _REVIEW_DIGEST,
            },
            None,
        ),
    ]


def test_recipe_remove_accepts_owner_normalized_selector_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    review = _unblocked_review(_review("recipe", "publisher/recipe", with_model=False))
    client = _FakeController(review)
    _accept_response_contracts(monkeypatch)
    args = _parse(
        "--json",
        "recipe",
        "remove",
        "Publisher/Recipe",
        "--keep-model",
        "--review-digest",
        _REVIEW_DIGEST,
        "--yes",
        "--detach",
    )

    result = controller_cli.run_controller(args, client, lambda: _REQUEST_KEY)

    assert result["selector"] == "publisher/recipe"
    assert [path for _, path, _, _ in client.calls] == [
        "/api/recipe/Publisher%2FRecipe/remove-review",
        "/api/recipe/Publisher%2FRecipe/remove",
    ]


def test_invalid_review_digest_is_rejected_by_parser(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        cli.main(
            (
                "--json",
                "model",
                "remove",
                "publisher/model",
                "--review-digest",
                "not-a-digest",
            )
        )
        == 2
    )
    assert "complete lowercase SHA-256" in capsys.readouterr().out
