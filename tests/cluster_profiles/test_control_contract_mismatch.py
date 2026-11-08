"""A Controller on another control contract gets a typed answer, not protocol_invalid.

Incident 2026-11-08: a Controller built with public contracts 4.7.0 answered
``/api/recipe/library`` with a reasoning effort that had become an object
tagged by ``kind``. Every older vonkctl rejected it against the OpenAPI it ships,
re-read it until its deadline and exited ``controller.protocol_invalid``; nothing
said that ``vonkctl update --apply`` was the way out.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Self

import jsonschema
import pytest
from vonk_control.cli_update_contract import CliUpdateContract
from vonk_control.library_contract import (
    FreshnessPolicy,
    LibraryFacetValues,
    RecipeLibraryResponse,
)
from vonk_control.platform_observation import ApiRuntimeObservation

from cluster_profiles import cli
from cluster_profiles.control_client import ControlClient
from cluster_profiles.control_client import client as client_module
from cluster_profiles.runtime_identity import RuntimeBuildIdentity

SHIPPED = "a" * 64
ERROR_SCHEMA = (
    Path(__file__).resolve().parents[2] / "schemas/vonkctl/error-v2.schema.json"
)


@pytest.fixture(autouse=True)
def observation_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))


class _Response:
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"
        self._body = json.dumps(payload).encode()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, maximum: int) -> bytes:
        return self._body[:maximum]


def _newer_library() -> dict[str, object]:
    library = RecipeLibraryResponse(
        generated_at=datetime(2026, 11, 8, tzinfo=UTC),
        recipes=[],
        facets=LibraryFacetValues(usage=[], family=[], version=[], quantization=[]),
        next_cursor=None,
        freshness_policy=FreshnessPolicy(),
    ).model_dump(mode="json")
    # The 4.7.0 change in miniature: a value this CLI's contract knows as a
    # string arrives as an object tagged by ``kind``.
    library["next_cursor"] = {"kind": "levels", "key": "reasoning_effort"}
    return library


def _compatibility(served: str) -> dict[str, object]:
    return CliUpdateContract(
        observed_at=datetime(2026, 11, 8, tzinfo=UTC),
        compatibility_schema_sha256="c" * 64,
        api=ApiRuntimeObservation(source_sha=None, control_contract_sha256=served),
        expected_worker_contract_sha256=None,
        worker_count=0,
        worker_membership_sha256="d" * 64,
        worker_source_sha=None,
        worker_contract_sha256=None,
        worker_compatibility="unknown",
        worker_issue="worker-observation-unavailable",
    ).model_dump(mode="json")


def _controller(
    tmp_path: Path, compatibility: Callable[[], _Response]
) -> tuple[ControlClient, list[str]]:
    requested: list[str] = []

    def opener(request, *, timeout: float) -> _Response:
        path = urllib.parse.urlsplit(request.full_url).path
        requested.append(path)
        if path == "/api/recipe/library":
            return _Response(200, _newer_library())
        assert path == "/api/cli/contract"
        return compatibility()

    token = tmp_path / "token"
    token.write_text("private-token")
    token.chmod(0o600)
    return (
        ControlClient("https://forge.example.test", token, opener=opener),
        requested,
    )


def _library_failure(
    client: ControlClient, capsys: pytest.CaptureFixture[str]
) -> dict[str, object]:
    status = cli.main(("--json", "recipe", "library"), control_client=client)
    document = json.loads(capsys.readouterr().out)
    assert status == 2, document
    jsonschema.Draft202012Validator(json.loads(ERROR_SCHEMA.read_text())).validate(
        document
    )
    return document


@pytest.fixture
def shipped_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        client_module,
        "packaged_runtime_identity",
        lambda: RuntimeBuildIdentity(None, SHIPPED, "b" * 64),
    )


def test_a_controller_on_another_contract_answers_update_with_one_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], shipped_contract: None
) -> None:
    client, requested = _controller(
        tmp_path, lambda: _Response(200, _compatibility("e" * 64))
    )

    document = _library_failure(client, capsys)

    assert document["code"] == "client.contract_mismatch"
    assert document["recovery_actions"] == ["vonkctl update --apply"]
    assert document["decision"] == "exit"
    assert document["retryable"] is False
    # Re-reading cannot change the answer: one read, one compatibility check.
    assert requested == ["/api/recipe/library", "/api/cli/contract"]


@pytest.mark.parametrize(
    "compatibility",
    [
        pytest.param(lambda: _Response(200, _compatibility(SHIPPED)), id="same"),
        pytest.param(
            lambda: _Response(503, {"detail": "observation unavailable"}),
            id="unobservable",
        ),
    ],
)
def test_same_or_unknown_contract_keeps_protocol_invalid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    shipped_contract: None,
    compatibility: Callable[[], _Response],
) -> None:
    client, requested = _controller(tmp_path, compatibility)

    document = _library_failure(client, capsys)

    assert document["code"] == "controller.protocol_invalid"
    assert not document.get("recovery_actions")
    assert requested.count("/api/cli/contract") == 1
    assert requested.count("/api/recipe/library") > 1


def test_an_unstamped_cli_never_claims_a_mismatch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        client_module,
        "packaged_runtime_identity",
        lambda: RuntimeBuildIdentity(None, None, None),
    )
    client, requested = _controller(
        tmp_path, lambda: _Response(200, _compatibility("e" * 64))
    )

    document = _library_failure(client, capsys)

    assert document["code"] == "controller.protocol_invalid"
    assert "/api/cli/contract" not in requested
