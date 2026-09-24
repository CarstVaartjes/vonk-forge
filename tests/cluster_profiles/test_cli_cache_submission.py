"""Bounded recovery at the download acceptance/response boundary."""

from __future__ import annotations

import http.client
import json
from email.message import Message
from io import BytesIO, StringIO
from urllib.error import HTTPError

import pytest
from test_controller_cli import FakeClient, run

from cluster_profiles import cli, controller_cli
from cluster_profiles.control_client import (
    ControlClient,
    ControlForbidden,
    ControlMalformedResponse,
    ControlNotFound,
    ControlTransportError,
    ControlUnavailable,
)

KEY = "11111111-1111-4111-8111-111111111111"


def receipt(noun: str) -> dict[str, object]:
    if noun == "model":
        return {
            "action": "download",
            "selector": "chosen",
            "request_key": KEY,
            "operation_id": "original",
            "state": "queued",
        }
    return {
        "kind": "recipe.image.availability.v2",
        "id": "original",
        "request_id": KEY,
        "state": "queued",
        "request": {"kind": "selector", "selector": "chosen", "force": True},
    }


class SubmissionClient(FakeClient):
    request_timeout_seconds = 2.0


def acceptance(result: dict[str, object]) -> object:
    submission = result["submission"]
    assert isinstance(submission, dict)
    return submission["acceptance"]


@pytest.mark.parametrize("noun", ["model", "recipe"])
def test_lost_download_response_finds_original_without_second_submission(noun):
    accepted = receipt(noun)
    client = SubmissionClient(
        {
            ("POST", f"/api/{noun}/chosen/download"): ControlTransportError(),
            ("GET", f"/api/{noun}/requests/{KEY}"): accepted,
        }
    )
    status, result = run((noun, "download", "chosen", "--detach", "--json"), client)
    assert status == 0 and result == accepted
    assert [call[0] for call in client.calls] == ["POST", "GET"]


@pytest.mark.parametrize("second_lost", [False, True])
def test_only_authoritative_absence_allows_one_identical_replay(second_lost):
    post = "/api/model/chosen/download"
    client = SubmissionClient(
        {
            ("POST", post): [
                ControlUnavailable(503, "response unavailable"),
                ControlTransportError() if second_lost else receipt("model"),
            ],
            ("GET", f"/api/model/requests/{KEY}"): ControlNotFound(404, "not found"),
        }
    )
    status, result = run(("model", "download", "chosen", "--detach", "--json"), client)
    assert [call[0] for call in client.calls] == ["POST", "GET", "POST"]
    assert client.calls[0][1:3] == client.calls[2][1:3]
    if second_lost:
        assert status == 2
        assert acceptance(result) == "unknown"
        assert result["request_key"] == KEY
    else:
        assert status == 0 and result == receipt("model")


@pytest.mark.parametrize(
    "failure",
    [
        ControlForbidden(403, "no read access"),
        ControlUnavailable(503, "offline"),
        receipt("model") | {"request_key": "22222222-2222-4222-8222-222222222222"},
        receipt("model") | {"action": "remove"},
        receipt("model") | {"selector": "another-model"},
    ],
)
def test_failed_or_foreign_lookup_cannot_justify_replay(failure):
    client = SubmissionClient(
        {
            ("POST", "/api/model/chosen/download"): ControlTransportError(),
            ("GET", f"/api/model/requests/{KEY}"): failure,
        }
    )
    status, result = run(("model", "download", "chosen", "--detach", "--json"), client)
    assert status == 2 and acceptance(result) == "unknown"
    reconnect = result["reconcile"]
    assert isinstance(reconnect, dict)
    assert reconnect["operation"] == (
        f"vonkctl model progress --request-key {KEY} --follow"
    )
    assert [call[0] for call in client.calls] == ["POST", "GET"]


@pytest.mark.parametrize("found", [False, True])
def test_malformed_success_allows_only_read_only_diagnosis(found):
    client = SubmissionClient(
        {
            ("POST", "/api/recipe/chosen/download"): ControlMalformedResponse(
                "bad JSON"
            ),
            ("GET", f"/api/recipe/requests/{KEY}"): receipt("recipe")
            if found
            else ControlNotFound(404, "not found"),
        }
    )
    status, result = run(("recipe", "download", "chosen", "--detach", "--json"), client)
    assert [call[0] for call in client.calls] == ["POST", "GET"]
    assert status == (0 if found else 2)
    if not found:
        assert acceptance(result) == "unknown"


def test_definite_refusal_is_not_looked_up_or_retried():
    client = SubmissionClient(
        {
            ("POST", "/api/model/chosen/download"): ControlForbidden(
                403, "no mutation access"
            ),
        }
    )
    status, result = run(("model", "download", "chosen", "--json"), client)
    assert status == 2 and acceptance(result) == "refused"
    assert result["http_status"] == 403
    assert len(client.calls) == 1


@pytest.mark.parametrize("exhausted", [False, True])
def test_recovery_deadline_is_shared_across_all_three_network_calls(
    monkeypatch, exhausted
):
    elapsed = 0.0
    timeouts = []

    class DelayedClient(SubmissionClient):
        def request(self, *args, **kwargs):
            nonlocal elapsed
            timeouts.append(kwargs.get("timeout_seconds"))
            # Scheduling delay consumes the same budget as network time.
            elapsed += (3.5, 2.5 if exhausted else 2, 0)[len(timeouts) - 1]
            return super().request(*args, **kwargs)

    monkeypatch.setattr(controller_cli.time, "monotonic", lambda: elapsed)
    client = DelayedClient(
        {
            ("POST", "/api/model/chosen/download"): [
                ControlTransportError(),
                receipt("model"),
            ],
            ("GET", f"/api/model/requests/{KEY}"): ControlNotFound(404, "not found"),
        }
    )
    status, result = run(("model", "download", "chosen", "--detach", "--json"), client)
    assert status == (2 if exhausted else 0)
    assert timeouts == ([2.0, 2.0] if exhausted else [2.0, 2.0, 0.5])
    if exhausted:
        assert acceptance(result) == "unknown"
        assert result["code"] == "control.submission_timeout"


@pytest.mark.parametrize("delay", [1, 7])
def test_server_retry_delay_consumes_the_existing_submission_budget(monkeypatch, delay):
    elapsed = 0.0
    slept = []

    def sleep(seconds):
        nonlocal elapsed
        slept.append(seconds)
        elapsed += seconds

    monkeypatch.setattr(controller_cli.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(controller_cli.time, "sleep", sleep)
    client = SubmissionClient(
        {
            ("POST", "/api/model/chosen/download"): [
                ControlUnavailable(503, "try later", retry_after_seconds=delay),
                receipt("model"),
            ],
            ("GET", f"/api/model/requests/{KEY}"): ControlNotFound(404, "not found"),
        }
    )
    status, result = run(("model", "download", "chosen", "--detach", "--json"), client)
    if delay < 6:
        assert status == 0 and slept == [delay]
        assert [call[0] for call in client.calls] == ["POST", "GET", "POST"]
    else:
        assert status == 2 and acceptance(result) == "unknown"
        assert not slept and [call[0] for call in client.calls] == ["POST", "GET"]


def test_observation_does_not_shorten_a_server_delay_to_poll_early(monkeypatch):
    elapsed = 0.0

    def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds

    monkeypatch.setattr(controller_cli.time, "monotonic", lambda: elapsed)
    monkeypatch.setattr(controller_cli.time, "sleep", sleep)
    identity = {"operation_id": "original", "state": "running"}
    client = SubmissionClient(
        {
            ("GET", "/api/model/operations/original"): [
                identity,
                ControlUnavailable(503, "try later", retry_after_seconds=120),
                identity | {"state": "succeeded"},
            ],
        }
    )
    status, result = run(
        (
            "model",
            "progress",
            "original",
            "--follow",
            "--timeout-seconds",
            "60",
            "--json",
        ),
        client,
    )
    assert status == 2
    observation = result["observation"]
    assert isinstance(observation, dict) and observation["status"] == "timed_out"
    assert len(client.calls) == 2 and elapsed == 60


@pytest.mark.parametrize("failure", ["lost", "malformed", "normal"])
def test_received_403_remains_a_refusal_when_its_body_is_lost(
    tmp_path, capsys, failure
):
    token = tmp_path / "token"
    token.write_text("fixture-token")
    token.chmod(0o600)
    calls = []

    class BrokenBody(BytesIO):
        def read(self, size=-1):
            raise http.client.IncompleteRead(b"Bearer partial-secret", 100)

    def opener(request, timeout):
        calls.append(request.get_method())
        headers = Message()
        headers["Content-Type"] = "application/json"
        headers["X-Request-ID"] = "refused-original"
        body = (
            BrokenBody()
            if failure == "lost"
            else BytesIO(
                b'{"partial":' if failure == "malformed" else b'{"detail":"Forbidden"}'
            )
        )
        raise HTTPError(request.full_url, 403, "Forbidden", headers, body)

    control = ControlClient(
        "https://forge.example.test", token, opener=opener, timeout_seconds=2
    )
    assert (
        cli.main(
            ("model", "download", "model-" + "m" * 175, "--request-key", KEY, "--json"),
            control_client=control,
        )
        == 2
    )
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert acceptance(result) == "refused"
    assert result["http_status"] == 403
    assert result["request_id"] == "refused-original"
    assert calls == ["POST"]
    assert "partial-secret" not in output.out + output.err


def test_lost_server_error_body_preserves_its_retry_delay(
    tmp_path, capsys, monkeypatch
):
    token = tmp_path / "token"
    token.write_text("fixture-token")
    token.chmod(0o600)
    calls = []

    class BrokenBody(BytesIO):
        def read(self, size=-1):
            raise http.client.IncompleteRead(b"partial", 100)

    def opener(request, timeout):
        calls.append(request.get_method())
        assert len(calls) <= 2, "retried before the server's Retry-After"
        headers = Message()
        headers["Content-Type"] = "application/json"
        if len(calls) == 1:
            headers["Retry-After"] = "120"
            raise HTTPError(request.full_url, 503, "Unavailable", headers, BrokenBody())
        raise HTTPError(
            request.full_url,
            404,
            "Not Found",
            headers,
            BytesIO(b'{"detail":"not found"}'),
        )

    def unexpected_sleep(seconds):
        raise AssertionError("server delay exceeds the whole submission budget")

    monkeypatch.setattr(controller_cli.time, "sleep", unexpected_sleep)
    control = ControlClient("https://forge.example.test", token, opener=opener)
    assert (
        cli.main(
            ("model", "download", "chosen", "--request-key", KEY, "--json"),
            control_client=control,
        )
        == 2
    )
    result = json.loads(capsys.readouterr().out)
    assert acceptance(result) == "unknown" and result["retry_after_seconds"] == 120
    assert calls == ["POST", "GET"]


def test_human_key_is_flushed_before_the_post_and_interrupt_keeps_unknown(
    monkeypatch, capsys
):
    class Stderr(StringIO):
        flushed = False

        def flush(self):
            self.flushed = True
            return super().flush()

    output = Stderr()
    monkeypatch.setattr(controller_cli.sys, "stderr", output)

    class InterruptedClient(SubmissionClient):
        def request(self, *args, **kwargs):
            assert output.flushed and KEY in output.getvalue()
            assert (
                f"vonkctl model progress --request-key {KEY} --follow"
                in output.getvalue()
            )
            raise KeyboardInterrupt()

    assert (
        cli.main(
            ("model", "download", "chosen", "--request-key", KEY),
            control_client=InterruptedClient({}),
        )
        == 130
    )
    assert "acceptance is unknown" in output.getvalue().lower()
    assert not capsys.readouterr().out

    # JSON emits no preamble and still records uncertainty on interruption.
    monkeypatch.setattr(controller_cli.sys, "stderr", StringIO())
    client = SubmissionClient(
        {("POST", "/api/model/chosen/download"): KeyboardInterrupt()}
    )
    assert (
        cli.main(
            ("model", "download", "chosen", "--request-key", KEY, "--json"),
            control_client=client,
        )
        == 130
    )
    document = json.loads(capsys.readouterr().out)
    assert document["submission"]["acceptance"] == "unknown"
    assert document["request_key"] == KEY


@pytest.mark.parametrize("noun", ["model", "recipe"])
def test_uncertain_cancellation_reconnect_preserves_cancellation_identity(noun):
    operation_id = "00000000-0000-4000-8000-000000000015"
    path = f"/api/{noun}/operations/{operation_id}"
    client = SubmissionClient(
        {
            ("POST", path + "/cancel"): ControlTransportError("answer lost"),
            ("GET", path): ControlUnavailable(503, "observation unavailable"),
        }
    )
    status, result = run(
        (
            noun,
            "cancel",
            operation_id,
            "--yes",
            "--detach",
            "--request-key",
            KEY,
            "--reason",
            "Stop this request",
            "--json",
        ),
        client,
    )
    assert status == 2 and acceptance(result) == "unknown"
    reconcile = result["reconcile"]
    assert isinstance(reconcile, dict)
    assert reconcile["operation"] == (
        f"vonkctl {noun} cancel {operation_id} --yes --request-key {KEY} "
        "--reason 'Stop this request'"
    )
    assert [call[0] for call in client.calls] == ["POST", "GET"]
