"""The serving chat check is sent as the recipe's own API facts say it fits."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import cast

import pytest
from vonk_forge_contracts.api import ChatApi

from cluster_profiles.serving_execution import (
    ServingExecutionError,
    execute_http_check,
)
from cluster_profiles.serving_request import Json, bounded_chat_check

CHECK: dict[str, Json] = {
    "name": "hello",
    "kind": "openai.chat",
    "assertions": ["chat.nonempty", "chat.output-cap"],
    "request": {
        "transport": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "body": {
            "model": "$ALIAS",
            "messages": [{"role": "user", "content": "hello"}],
            "max_tokens": 64,
        },
    },
}
REMAINING_CONTEXT = 100_000
THINKING_BUDGET = 1000  # tokens an engine spends thinking before it answers


def _facts(
    reasoning: dict[str, object],
    *,
    default: dict[str, object] | None = None,
    maximum: dict[str, object] | None = None,
) -> ChatApi:
    return ChatApi.model_validate(
        {
            "mode": "chat",
            "max_output_tokens": maximum or {"setting": "context_tokens"},
            "default_output_tokens": default or {"remaining": "context"},
            "max_input_tokens": None,
            "input_modalities": {
                "text": True,
                "image": False,
                "video": False,
                "audio": False,
                "pdf": False,
            },
            "audio_output": False,
            "reasoning": reasoning,
            "tools": {"kind": "none"},
            "structured_output": False,
            "system_messages": True,
            "streaming": True,
            "endpoints": ["/v1/chat/completions"],
        }
    )


def _switch(via: str, keys: list[str], off: object = False) -> ChatApi:
    return _facts(
        {
            "kind": "switch",
            "via": via,
            "keys": keys,
            "on": True,
            "off": off,
            "default": "on",
            "effort": None,
        }
    )


ALWAYS_ON = {"kind": "always-on", "effort": None}


def _body(check: dict[str, Json]) -> dict[str, Json]:
    request = cast(dict[str, Json], check["request"])
    return cast(dict[str, Json], request["body"])


def test_switch_route_gets_its_off_value_at_its_declared_placement() -> None:
    nested = bounded_chat_check(
        CHECK, _switch("chat_template_kwargs", ["enable_thinking"])
    )
    assert _body(nested)["chat_template_kwargs"] == {"enable_thinking": False}
    assert _body(nested)["max_tokens"] == 64
    top = bounded_chat_check(
        CHECK, _switch("top_level", ["thinking", "think"], off="none")
    )
    assert _body(top)["thinking"] == "none" and _body(top)["think"] == "none"
    assert "chat_template_kwargs" not in _body(top)


def test_a_key_the_check_already_names_is_kept() -> None:
    named = json.loads(json.dumps(CHECK))
    _body(named)["chat_template_kwargs"] = {"enable_thinking": True}
    fitted = bounded_chat_check(
        named, _switch("chat_template_kwargs", ["enable_thinking"])
    )
    assert _body(fitted)["chat_template_kwargs"] == {"enable_thinking": True}


def test_always_on_route_follows_default_output_tokens_capped_by_its_maximum() -> None:
    value = _facts(ALWAYS_ON, default={"value": 8192}, maximum={"value": 32768})
    assert _body(bounded_chat_check(CHECK, value))["max_tokens"] == 8192
    capped = _facts(ALWAYS_ON, default={"value": 8192}, maximum={"value": 2048})
    assert _body(bounded_chat_check(CHECK, capped))["max_tokens"] == 2048


def test_always_on_argument_default_reads_the_recipes_runtime_argument() -> None:
    facts = _facts(ALWAYS_ON, default={"argument": "max-model-len"})
    fitted = bounded_chat_check(CHECK, facts, {"max-model-len": 6000}.get)
    assert _body(fitted)["max_tokens"] == 6000
    # No number the facts cannot give is invented: the check goes as declared.
    assert bounded_chat_check(CHECK, facts, {}.get) == CHECK
    assert bounded_chat_check(CHECK, facts) == CHECK


def test_always_on_remaining_context_sends_no_cap_and_asserts_none() -> None:
    fitted = bounded_chat_check(CHECK, _facts(ALWAYS_ON))
    assert "max_tokens" not in _body(fitted)
    assert fitted["assertions"] == ["chat.nonempty"]


def test_no_facts_or_no_reasoning_is_unchanged_and_the_declared_check_is_never_edited() -> (
    None
):
    before = json.dumps(CHECK, sort_keys=True)
    assert bounded_chat_check(CHECK, None) == CHECK
    assert bounded_chat_check(CHECK, _facts({"kind": "none"})) == CHECK
    bounded_chat_check(CHECK, _switch("top_level", ["thinking"]))
    assert json.dumps(CHECK, sort_keys=True) == before


def test_only_chat_checks_are_fitted() -> None:
    health = {"kind": "openai.health", "request": {"method": "GET", "path": "/health"}}
    assert bounded_chat_check(health, _switch("top_level", ["thinking"])) == health


class _ThinkingEngine(BaseHTTPRequestHandler):
    """Spends its whole budget thinking unless the declared off switch arrives."""

    off_keys: tuple[tuple[str | None, str], ...] = (
        ("chat_template_kwargs", "enable_thinking"),
        (None, "think"),
    )

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"] or 0)))
        thinking = all(
            (request if where is None else request.get(where, {})).get(key, True)
            is not False
            for where, key in self.off_keys
        )
        budget = request.get("max_tokens", REMAINING_CONTEXT)
        if thinking and budget <= THINKING_BUDGET:
            message, used = {"content": None, "reasoning_content": "hmm"}, budget
        else:
            message, used = (
                {"content": "hello there"},
                3 + (THINKING_BUDGET if thinking else 0),
            )
        payload = json.dumps(
            {"choices": [{"message": message}], "usage": {"completion_tokens": used}}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format: str, *_args: object) -> None:
        return


@pytest.fixture()
def engine():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ThinkingEngine)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=30)


def test_declared_check_alone_sees_only_thinking(engine: str) -> None:
    with pytest.raises(ServingExecutionError, match="no non-empty content"):
        execute_http_check(engine, CHECK, model_alias="m")


def test_off_switch_gets_an_answer_inside_the_declared_cap(engine: str) -> None:
    facts = _switch("chat_template_kwargs", ["enable_thinking"])
    observed = execute_http_check(engine, CHECK, model_alias="m", chat_api=facts)
    assert observed["observed_features"] == ["text"]


def test_top_level_off_switch_is_honoured(engine: str) -> None:
    facts = _switch("top_level", ["think"])
    observed = execute_http_check(engine, CHECK, model_alias="m", chat_api=facts)
    assert observed["observed_features"] == ["text"]


def test_always_on_remaining_context_answers_without_a_cap(engine: str) -> None:
    observed = execute_http_check(
        engine, CHECK, model_alias="m", chat_api=_facts(ALWAYS_ON)
    )
    assert observed["observed_features"] == ["text"]


def test_always_on_route_is_judged_against_the_budget_it_was_sent(engine: str) -> None:
    facts = _facts(ALWAYS_ON, default={"value": 4096}, maximum={"value": 8192})
    observed = execute_http_check(engine, CHECK, model_alias="m", chat_api=facts)
    assert observed["observed_features"] == ["text"]
