"""A schema rejection from a Controller on another contract is typed, not protocol_invalid.

Incident 2026-11-08: a Controller built with public contracts 4.7.0 answered
``/api/recipe/library`` with a reasoning effort that had become an object
tagged by ``kind``. Every older vonkctl rejected it against the OpenAPI it ships,
re-read it until its deadline and exited ``controller.protocol_invalid``; nothing
said that ``vonkctl update --apply`` was the way out.
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from typing import Self

import httpx2
import jsonschema
import pytest
from vonk_control.cli_update_contract import CliUpdateContract
from vonk_control.library_contract import (
    FreshnessPolicy,
    LibraryFacetValues,
    RecipeLibraryResponse,
)
from vonk_control.observation_transfer import (
    OBSERVATION_MEDIA_TYPE,
    observation_response,
)
from vonk_control.platform_observation import (
    ApiRuntimeObservation,
    PlatformObservation,
)

from cluster_profiles import cli
from cluster_profiles.control_client import (
    ControlClient,
    ControlContractMismatch,
    ControlSchemaRejected,
)
from cluster_profiles.control_client import client as client_module
from control.tests.observation_transfer_peer import ObservationHTTPPeer

SHIPPED = "a" * 64
SERVED = "e" * 64
ERROR_SCHEMA = (
    Path(__file__).resolve().parents[2] / "schemas/vonkctl/error-v2.schema.json"
)

type Answer = _Response | ObservationHTTPPeer


@pytest.fixture(autouse=True)
def observation_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))


@pytest.fixture(autouse=True)
def stamped(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | None], None]:
    def stamp(value: str | None) -> None:
        monkeypatch.setattr(
            client_module, "stamped_control_contract_sha256", lambda: value
        )

    stamp(SHIPPED)
    return stamp


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


def _newer_library() -> _Response:
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
    return _Response(200, library)


def _compatibility(served: str = SERVED) -> _Response:
    return _Response(
        200,
        CliUpdateContract(
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
        ).model_dump(mode="json"),
    )


def _unobservable() -> _Response:
    return _Response(503, {"detail": "observation unavailable"})


def _platform(*, complete: bool) -> ObservationHTTPPeer:
    """The Controller's own platform stream, optionally cut before its receipt."""
    response = observation_response(
        PlatformObservation(
            observed_at=datetime(2026, 11, 8, tzinfo=UTC),
            api=ApiRuntimeObservation(source_sha=None, control_contract_sha256=SERVED),
            workers=None,
            worker_issue="worker-observation-unavailable",
        ),
        resource="platform",
    )

    async def collect() -> bytes:
        parts: list[bytes] = []
        async for part in response.body_iterator:
            parts.append(part.encode() if isinstance(part, str) else bytes(part))
        return b"".join(parts)

    body = asyncio.run(collect())
    if not complete:
        body = b"".join(body.splitlines(keepends=True)[:-1])
    return ObservationHTTPPeer(
        httpx2.Response(
            200,
            content=body,
            headers={"Content-Type": OBSERVATION_MEDIA_TYPE},
            request=httpx2.Request("GET", "https://forge.example.test/api/platform"),
        )
    )


def _controller(
    tmp_path: Path, answers: dict[str, Iterator[Answer] | Callable[[], Answer]]
) -> tuple[ControlClient, list[str]]:
    requested: list[str] = []

    def opener(request, *, timeout: float) -> Answer:
        path = urllib.parse.urlsplit(request.full_url).path
        requested.append(path)
        answer = answers[path]
        return answer() if callable(answer) else next(answer)

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


def test_a_controller_on_another_contract_answers_update_with_one_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, requested = _controller(
        tmp_path,
        {"/api/recipe/library": _newer_library, "/api/cli/contract": _compatibility},
    )

    document = _library_failure(client, capsys)

    assert document["code"] == "client.contract_mismatch"
    assert document["recovery_actions"] == ["vonkctl update --apply"]
    assert document["decision"] == "exit"
    assert document["retryable"] is False
    # Re-reading cannot change the answer: one read, one compatibility check.
    assert requested == ["/api/recipe/library", "/api/cli/contract"]


def test_a_long_lived_client_keeps_the_answer_it_read(tmp_path: Path) -> None:
    client, requested = _controller(
        tmp_path,
        {"/api/recipe/library": _newer_library, "/api/cli/contract": _compatibility},
    )

    for _ in range(2):
        with pytest.raises(ControlContractMismatch):
            client.request("GET", "/api/recipe/library")

    assert requested.count("/api/cli/contract") == 1


def test_a_failed_compatibility_read_is_asked_again_within_its_bound(
    tmp_path: Path,
) -> None:
    client, requested = _controller(
        tmp_path,
        {
            "/api/recipe/library": _newer_library,
            "/api/cli/contract": iter([_unobservable(), _compatibility()]),
        },
    )

    with pytest.raises(ControlContractMismatch):
        client.request("GET", "/api/recipe/library")

    assert requested == [
        "/api/recipe/library",
        "/api/cli/contract",
        "/api/recipe/library",
        "/api/cli/contract",
    ]


def test_an_unanswered_compatibility_read_stops_at_its_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, requested = _controller(
        tmp_path,
        {"/api/recipe/library": _newer_library, "/api/cli/contract": _unobservable},
    )

    document = _library_failure(client, capsys)

    assert document["code"] == "controller.protocol_invalid"
    assert not document.get("recovery_actions")
    assert requested.count("/api/cli/contract") == client_module._CONTRACT_READS
    assert requested.count("/api/recipe/library") > client_module._CONTRACT_READS


def test_the_same_contract_keeps_protocol_invalid_and_re_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client, requested = _controller(
        tmp_path,
        {
            "/api/recipe/library": _newer_library,
            "/api/cli/contract": lambda: _compatibility(SHIPPED),
        },
    )

    document = _library_failure(client, capsys)

    assert document["code"] == "controller.protocol_invalid"
    assert not document.get("recovery_actions")
    assert requested.count("/api/cli/contract") == 1
    assert requested.count("/api/recipe/library") > 1


def test_an_unstamped_cli_never_claims_a_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    stamped: Callable[[str | None], None],
) -> None:
    stamped(None)
    client, requested = _controller(
        tmp_path,
        {"/api/recipe/library": _newer_library, "/api/cli/contract": _compatibility},
    )

    document = _library_failure(client, capsys)

    assert document["code"] == "controller.protocol_invalid"
    assert "/api/cli/contract" not in requested


def test_a_cut_transfer_stays_re_readable_on_another_contract(tmp_path: Path) -> None:
    client, requested = _controller(
        tmp_path,
        {
            "/api/platform": iter(
                [_platform(complete=False), _platform(complete=True)]
            ),
            "/api/cli/contract": _compatibility,
        },
    )

    platform = client.get("/api/platform")

    assert platform["api"] == {"source_sha": None, "control_contract_sha256": SERVED}
    # A transfer fault is not a schema rejection: nothing asks the contract.
    assert requested == ["/api/platform", "/api/platform"]


def test_a_generated_call_on_another_contract_is_typed(tmp_path: Path) -> None:
    job = "/api/jobs/12345678-1234-4123-8123-123456789abc"
    client, requested = _controller(
        tmp_path,
        {
            job: lambda: _Response(200, {"kind": "levels"}),
            "/api/cli/contract": _compatibility,
        },
    )

    with pytest.raises(ControlContractMismatch) as raised:
        client.job(job.rsplit("/", 1)[1])

    assert raised.value.context is not None
    assert raised.value.context.code == "client.contract_mismatch"
    assert requested == [job, "/api/cli/contract"]


def test_a_generated_schema_rejection_on_the_same_contract_stays_re_readable(
    tmp_path: Path,
) -> None:
    job = "/api/jobs/12345678-1234-4123-8123-123456789abc"
    client, requested = _controller(
        tmp_path,
        {
            job: lambda: _Response(200, {"kind": "levels"}),
            "/api/cli/contract": lambda: _compatibility(SHIPPED),
        },
    )

    with pytest.raises(ControlSchemaRejected):
        client.job(job.rsplit("/", 1)[1])

    assert requested.count(job) > 1
