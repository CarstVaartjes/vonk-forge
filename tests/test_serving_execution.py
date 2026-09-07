from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from cluster_profiles.serving_execution import (
    MAX_HTTP_RESPONSE_BYTES,
    HttpObservation,
    ServingExecutionError,
    evaluate_http_response,
    evaluate_job_result,
    execute_http_check,
)


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        size = int(self.headers["Content-Length"] or 0)
        request = json.loads(self.rfile.read(size))
        assert request["model"] == "fixture-model"
        payload = {
            "choices": [{"message": {"content": "fixture response"}}],
            "usage": {"completion_tokens": 1},
        }
        encoded = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture()
def fixture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_execute_http_check_uses_real_request_response_and_assertions(fixture_server: str) -> None:
    check = {
        "name": "chat-smoke",
        "kind": "openai.chat",
        "request": {
            "transport": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "body": {"model": "$ALIAS", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 4},
        },
        "assertions": ["chat.nonempty", "chat.output-cap"],
    }

    observed = execute_http_check(fixture_server, check, model_alias="fixture-model")

    assert observed["response_shape"] == "chat.completion"
    assert observed["choices"] == 1


def test_http_evaluator_rejects_output_cap() -> None:
    check = {
        "kind": "openai.completion",
        "request": {"body": {"prompt": "hi", "max_tokens": 2}},
        "assertions": ["completion.nonempty", "completion.output-cap"],
    }
    response = {"choices": [{"text": "done"}], "usage": {"completion_tokens": 3}}

    with pytest.raises(ServingExecutionError, match="output cap"):
        evaluate_http_response(HttpObservation(200, {}, json.dumps(response).encode()), check)


@pytest.mark.parametrize(
    "usage",
    [{}, {"completion_tokens": -1}, {"completion_tokens": "1"}],
)
def test_output_cap_requires_nonnegative_integer_usage(usage: dict[str, object]) -> None:
    check = {
        "kind": "openai.chat",
        "request": {"body": {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 2}},
        "assertions": ["chat.nonempty", "chat.output-cap"],
    }
    response = {"choices": [{"message": {"content": "done"}}], "usage": usage}

    with pytest.raises(ServingExecutionError, match="output cap"):
        evaluate_http_response(HttpObservation(200, {}, json.dumps(response).encode()), check)


def test_http_execution_rejects_oversized_content_length_before_read() -> None:
    class OversizedResponse:
        status = 200

        def __init__(self) -> None:
            self.headers = {"Content-Length": str(MAX_HTTP_RESPONSE_BYTES + 1)}

        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, *_args: object) -> bytes:
            raise AssertionError("oversized response must be rejected before reading")

    check = {
        "kind": "openai.health",
        "request": {"method": "GET", "path": "/health"},
        "assertions": ["endpoint.healthy"],
    }

    with pytest.raises(ServingExecutionError, match="maximum body size"):
        execute_http_check("http://fixture", check, opener=lambda *_args, **_kwargs: OversizedResponse())


def test_job_evaluator_requires_declared_output_slot() -> None:
    check = {
        "kind": "artifact-job.output",
        "request": {"output_slot": "result"},
        "assertions": ["inference.completed", "artifact.output"],
    }

    observed = evaluate_job_result({"state": "succeeded", "outputs": [{"slot": "result", "bytes": 1}]}, check)

    assert observed["response_shape"] == "job.result"
    assert observed["output_count"] == 1


@pytest.mark.parametrize("state", ["failed", "completed"])
def test_job_evaluator_rejects_non_succeeded_completion_with_outputs(state: str) -> None:
    check = {
        "kind": "artifact-job.output",
        "request": {"output_slot": "result"},
        "assertions": ["inference.completed", "artifact.output"],
    }

    with pytest.raises(ServingExecutionError, match="successful completion"):
        evaluate_job_result({"state": state, "outputs": [{"slot": "result", "bytes": 1}]}, check)
