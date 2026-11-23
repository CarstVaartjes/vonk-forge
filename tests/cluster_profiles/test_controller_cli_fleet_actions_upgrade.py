"""Controller CLI: fleet actions upgrade."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from io import StringIO

import pytest
from controller_cli_support import FakeClient, _InjectedClock, run

from cluster_profiles import cli
from cluster_profiles.control_client.errors import (
    ControlMalformedResponse,
    ControlTransportError,
)
from cluster_profiles.controller_cli import observation as observation_module


def test_fleet_actions_use_readable_selectors_and_avoid_legacy_agent_routes() -> None:
    node_id = "spk_" + "1" * 32
    log_path = f"/api/fleet/{node_id}/loginfo"
    client = FakeClient(
        {
            ("GET", "/api/fleet/Atlas"): {"display_name": "Atlas", "state": "live"},
            ("POST", "/api/fleet/Atlas/rename"): {"display_name": "Studio"},
            ("GET", "/api/fleet"): {
                "nodes": [{"id": node_id, "display_name": "Atlas"}]
            },
            ("GET", log_path): _fleet_log_response(node_id, follow=False),
        }
    )
    assert run(("fleet", "detail", "Atlas", "--json"), client)[0] == 0
    assert run(("fleet", "rename", "Atlas", "Studio", "--json"), client)[0] == 0
    assert (
        run(
            (
                "fleet",
                "loginfo",
                "Atlas",
                "--since",
                "1h",
                "--source",
                "runtime",
                "--json",
            ),
            client,
        )[0]
        == 0
    )
    assert [call[1] for call in client.calls] == [
        "/api/fleet/Atlas",
        "/api/fleet/Atlas/rename",
        "/api/fleet",
        log_path,
    ]
    assert all("/agent" not in call[1] for call in client.calls)


def test_error_output_redacts_request_secrets_and_keeps_json_clean() -> None:
    class FailingClient(FakeClient):
        def request(
            self, method, path, payload=None, *, extra_headers=None, query=None
        ):
            raise ValueError("authorization: Bearer secret-value")

    status, payload = run(("model", "detail", "qwen", "--json"), FailingClient({}))
    assert status == 2
    assert "secret-value" not in json.dumps(payload)


def test_async_mutations_follow_the_noun_operation_until_terminal() -> None:
    client = FakeClient(
        {
            ("POST", "/api/model/qwen/download"): {
                "state": "accepted",
                "operation_id": "download-1",
                "request_key": "11111111-1111-4111-8111-111111111111",
                "action": "download",
                "selector": "qwen",
            },
            ("GET", "/api/model/operations/download-1"): [
                {"state": "running", "operation_id": "download-1"},
                {"state": "succeeded", "operation_id": "download-1"},
            ],
        }
    )
    status, payload = run(
        ("model", "download", "qwen", "--interval-seconds", "0.01", "--json"),
        client,
    )
    assert status == 0
    assert payload["state"] == "succeeded"
    assert [call[1] for call in client.calls] == [
        "/api/model/qwen/download",
        "/api/model/operations/download-1",
        "/api/model/operations/download-1",
    ]


@pytest.mark.parametrize("noun", ["model", "recipe"])
def test_cache_request_lookup_pins_following_to_its_original_operation(
    noun: str,
) -> None:
    key = "11111111-1111-4111-8111-111111111111"
    identity = (
        {"operation_id": "original", "request_key": key, "action": "download"}
        if noun == "model"
        else {
            "id": "original",
            "request_id": key,
            "kind": "recipe.image.availability.v2",
        }
    )
    lookup = f"/api/{noun}/requests/{key}"
    target = f"/api/{noun}/operations/original"
    client = FakeClient(
        {
            ("GET", lookup): identity | {"state": "running"},
            ("GET", target): identity | {"state": "succeeded"},
        }
    )
    status, result = run(
        (
            noun,
            "progress",
            "--request-key",
            key,
            "--follow",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert result["state"] == "succeeded"
    assert [(call[0], call[1]) for call in client.calls] == [
        ("GET", lookup),
        ("GET", target),
    ]


def test_recipe_download_follows_its_canonical_receipt_id() -> None:
    receipt = {
        "id": "recipe-download",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "kind": "recipe.image.availability.v2",
        "state": "queued",
        "request": {"kind": "selector", "selector": "qwen-code", "force": False},
    }
    client = FakeClient(
        {
            ("POST", "/api/recipe/qwen-code/download"): receipt,
            ("GET", "/api/recipe/operations/recipe-download"): receipt
            | {"state": "succeeded"},
        }
    )
    status, result = run(
        (
            "recipe",
            "download",
            "qwen-code",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert result["state"] == "succeeded"
    assert [call[1] for call in client.calls] == [
        "/api/recipe/qwen-code/download",
        "/api/recipe/operations/recipe-download",
    ]


def test_recipe_cancel_recovers_accepted_request_after_lost_response() -> None:
    key = "11111111-1111-4111-8111-111111111111"
    receipt = {
        "id": "recipe-operation",
        "request_id": "22222222-2222-4222-8222-222222222222",
        "kind": "recipe.image.availability.v2",
        "state": "observing",
        "request": {"kind": "selector", "selector": "qwen-code", "force": False},
        "cancellation": {
            "cancel_requested": True,
            "cancel_requested_at": "2026-09-23T12:00:00+00:00",
            "cancel_request_id": key,
            "cancel_actor": "operator",
            "reason": "stop preparation",
        },
    }
    client = FakeClient(
        {
            (
                "POST",
                "/api/recipe/operations/recipe-operation/cancel",
            ): ControlTransportError("response was lost"),
            ("GET", "/api/recipe/operations/recipe-operation"): receipt,
        }
    )
    status, result = run(
        (
            "recipe",
            "cancel",
            "recipe-operation",
            "--yes",
            "--detach",
            "--request-key",
            key,
            "--reason",
            "stop preparation",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert result["state"] == "observing"
    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("POST", "/api/recipe/operations/recipe-operation/cancel"),
        ("GET", "/api/recipe/operations/recipe-operation"),
    ]
    assert client.calls[0][2] == {
        "request_key": key,
        "reason": "stop preparation",
    }


def test_recipe_cancel_requires_explicit_yes_before_mutation() -> None:
    client = FakeClient({})
    status, result = run(("recipe", "cancel", "operation", "--json"), client)
    assert status == 2
    assert "requires --yes" in str(result["error"])
    assert client.calls == []


def test_interrupted_submission_retains_reconnect_without_claiming_acceptance() -> None:
    client = FakeClient({("POST", "/api/model/qwen/download"): KeyboardInterrupt()})
    status, result = run(("model", "download", "qwen", "--json"), client)
    assert status == 130
    assert isinstance(result["error"], str)
    assert isinstance(result["reconcile"], dict)
    assert "accepted work continues" not in result["error"]
    assert result["reconcile"]["operation"] == (
        "vonkctl model progress --request-key 11111111-1111-4111-8111-111111111111 --follow"
    )
    assert len(client.calls) == 1


@pytest.mark.parametrize("changed_binding", ["request", "operation"])
def test_cache_reconnection_discards_wrong_binding_until_exact_owner_repairs(
    changed_binding: str,
) -> None:
    key = "11111111-1111-4111-8111-111111111111"
    initial = {"operation_id": "original", "request_key": key, "state": "running"}
    if changed_binding == "request":
        initial["request_key"] = "22222222-2222-4222-8222-222222222222"
    client = FakeClient(
        {
            ("GET", f"/api/model/requests/{key}"): [
                initial,
                initial | {"request_key": key},
            ],
            ("GET", "/api/model/operations/original"): [
                initial | {"operation_id": "different", "state": "succeeded"},
                initial | {"request_key": key, "state": "succeeded"},
            ],
        }
    )
    status, result = run(
        (
            "model",
            "progress",
            "--request-key",
            key,
            "--follow",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert result["operation_id"] == "original"
    assert result["request_key"] == key
    assert all(call[0] == "GET" for call in client.calls)


def test_detach_returns_acceptance_without_observing_operation() -> None:
    client = FakeClient(
        {
            ("POST", "/api/recipe/qwen-code/download"): {
                "state": "queued",
                "id": "download-2",
                "kind": "recipe.image.availability.v2",
                "request_id": "11111111-1111-4111-8111-111111111111",
                "request": {
                    "kind": "selector",
                    "selector": "qwen-code",
                    "force": False,
                },
            }
        }
    )
    status, payload = run(
        ("recipe", "download", "qwen-code", "--detach", "--json"), client
    )
    assert status == 0
    assert payload["state"] == "queued"
    assert [call[1] for call in client.calls] == ["/api/recipe/qwen-code/download"]


def test_watch_shows_updated_resource_and_retains_final_snapshot(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The watch must retain the last observed snapshot at its deadline; renderer
    # and scheduler latency must not decide whether the second snapshot is read.
    class WatchClock:
        now = 0.0

        def monotonic(self) -> float:
            return self.now

        def sleep(self, delay: float) -> None:
            self.now += delay

    clock = WatchClock()
    monkeypatch.setattr(observation_module, "time", clock)
    common = {
        "identity": {"slug": "Qwen"},
        "selector": "qwen",
        "usage": [],
        "resources": {"disk_bytes": 42},
    }
    preparing = {**common, "local": {"controller": "preparing", "running_on": []}}
    cached = {**common, "local": {"controller": "cached", "running_on": []}}
    client = FakeClient({("GET", "/api/model/qwen"): [preparing, cached]})
    status = cli.main(
        (
            "model",
            "detail",
            "qwen",
            "--watch",
            "--interval-seconds",
            "0.01",
            "--timeout-seconds",
            "0.04",
        ),
        control_client=client,
    )
    captured = capsys.readouterr()
    assert status == 2  # A resource watch has a deadline, not an execution outcome.
    assert clock.now == pytest.approx(0.04)
    assert len(client.calls) >= 2
    # Each observed snapshot is shown as progress; only the final one is the result.
    shown: list[str] = []
    for document in (preparing, cached):
        detail = FakeClient({("GET", "/api/model/qwen"): document})
        assert cli.main(("model", "detail", "qwen"), control_client=detail) == 0
        shown.append(capsys.readouterr().out)
    preparing_text, cached_text = shown
    assert preparing_text != cached_text
    assert preparing_text.rstrip() in captured.err
    assert cached_text.rstrip() in captured.err
    assert captured.out == cached_text
    assert "accepted work" not in captured.err


def test_noninteractive_removals_fail_closed_and_recipe_requires_model_choice() -> None:
    model = FakeClient({})
    status, payload = run(("model", "remove", "qwen", "--json"), model)
    removal_error = payload["error"]
    assert isinstance(removal_error, str)
    assert status == 2
    assert "--yes" in removal_error
    assert model.calls == []

    recipe = FakeClient({})
    status, payload = run(("recipe", "remove", "qwen-code", "--yes", "--json"), recipe)
    model_choice_error = payload["error"]
    assert isinstance(model_choice_error, str)
    assert status == 2
    assert "--with-model or --keep-model" in model_choice_error
    assert recipe.calls == []


def test_a_terminal_failure_state_has_a_nonzero_exit_code() -> None:
    failed = FakeClient(
        {
            ("POST", "/api/model/qwen/download"): {
                "state": "failed",
                "operation_id": "download",
                "request_key": "11111111-1111-4111-8111-111111111111",
                "action": "download",
                "selector": "qwen",
            }
        }
    )
    assert run(("model", "download", "qwen", "--json"), failed)[0] == 2


def test_default_log_window_is_an_api_timestamp_and_all_upgrade_omits_selectors() -> (
    None
):
    job_id = "upgrade-job"
    request_key = "11111111-1111-4111-8111-111111111111"
    node_id = "spk_" + "1" * 32
    path = f"/api/fleet/{node_id}/loginfo"
    client = FakeClient(
        {
            ("GET", "/api/fleet"): {
                "nodes": [{"id": node_id, "display_name": "Atlas"}]
            },
            ("GET", path): _fleet_log_response(node_id, follow=False),
            ("POST", "/api/fleet/upgrade"): {
                "action": "upgrade",
                "state": "queued",
                "operation_id": job_id,
                "plan_digest": "a" * 64,
                "request_key": request_key,
                "targets": [],
            },
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": "succeeded",
                "kind": "agent-upgrade",
                "targets": [],
                "operations": [],
                "progress": {"running": 0, "completed": 0, "failed": 0, "total": 0},
            },
        }
    )
    before = datetime.now(UTC) - timedelta(minutes=15)
    assert run(("fleet", "loginfo", "Atlas", "--json"), client)[0] == 0
    after = datetime.now(UTC) - timedelta(minutes=15)
    query = client.calls[-1][3]
    assert isinstance(query, dict)
    since = datetime.fromisoformat(query["since"])
    assert before <= since <= after
    assert (
        run(
            (
                "fleet",
                "upgrade",
                "--all",
                "--yes",
                "--request-key",
                request_key,
                "--json",
                "--interval-seconds",
                "0.01",
            ),
            client,
        )[0]
        == 0
    )
    post = next(
        call for call in client.calls if call[0:2] == ("POST", "/api/fleet/upgrade")
    )
    payload = post[2]
    assert isinstance(payload, dict)
    assert "selectors" not in payload


def test_fleet_upgrade_refuses_noninteractive_submission_without_consent() -> None:
    client = FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): {
                "action": "upgrade",
                "state": "queued",
                "operation_id": "upgrade-job",
                "plan_digest": "a" * 64,
                "request_key": "11111111-1111-4111-8111-111111111111",
                "targets": [],
            }
        }
    )

    status, result = run(("--no-input", "fleet", "upgrade", "--all", "--json"), client)

    assert status == 2
    assert not any(call[0:2] == ("POST", "/api/fleet/upgrade") for call in client.calls)
    assert "--yes" in str(result.get("error"))


def test_fleet_upgrade_resolves_friendly_scope_before_explicit_consent() -> None:
    node_id = "spk_" + "a" * 32
    request_key = "33333333-3333-4333-8333-333333333333"
    client = FakeClient(
        {
            ("GET", "/api/fleet"): {
                "nodes": [{"id": node_id, "display_name": "Atlas"}]
            },
            ("POST", "/api/fleet/upgrade"): {
                "action": "upgrade",
                "state": "queued",
                "operation_id": "upgrade-job",
                "plan_digest": "b" * 64,
                "request_key": request_key,
                "targets": [node_id],
            },
        }
    )

    status, _result = run(
        (
            "--no-input",
            "fleet",
            "upgrade",
            "Atlas",
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert [call[0:2] for call in client.calls] == [
        ("GET", "/api/fleet"),
        ("POST", "/api/fleet/upgrade"),
    ]
    body = client.calls[-1][2]
    assert body == {
        "all": False,
        "request_key": request_key,
        "selectors": [node_id],
    }


def test_fleet_upgrade_prompts_for_resolved_scope_in_a_terminal(monkeypatch) -> None:
    class TerminalInput(StringIO):
        def isatty(self) -> bool:
            return True

    class TerminalOutput(StringIO):
        def isatty(self) -> bool:
            return True

    node_id = "spk_" + "c" * 32
    terminal_input = TerminalInput("y\n")
    terminal_output = TerminalOutput()
    monkeypatch.setattr(cli.sys, "stdin", terminal_input)
    monkeypatch.setattr(cli.sys, "stderr", terminal_output)
    client = FakeClient(
        {
            ("GET", "/api/fleet"): {
                "nodes": [{"id": node_id, "display_name": "Atlas"}]
            },
            ("POST", "/api/fleet/upgrade"): {
                "action": "upgrade",
                "state": "queued",
                "operation_id": "upgrade-job",
                "plan_digest": "c" * 64,
                "request_key": "44444444-4444-4444-8444-444444444444",
                "targets": [node_id],
            },
        }
    )

    status = cli.main(
        ("fleet", "upgrade", "Atlas", "--detach"),
        control_client=client,
        request_id_factory=lambda: "44444444-4444-4444-8444-444444444444",
    )

    assert status == 0
    assert client.calls[-1][0:2] == ("POST", "/api/fleet/upgrade")
    assert terminal_input.read() == ""


def _fleet_log_response(node_id: str, *, follow: bool) -> dict[str, object]:
    return {
        "node_id": node_id,
        "since": "2026-09-24T10:00:00Z",
        "lines": 100,
        "entries": [],
        "retained": True,
        "follow": follow,
    }


def test_fleet_log_follow_resolves_alias_once_and_pins_every_poll_to_node_id() -> None:
    node_id = "spk_" + "1" * 32
    other_node_id = "spk_" + "2" * 32
    path = f"/api/fleet/{node_id}/loginfo"
    client = FakeClient(
        {
            ("GET", "/api/fleet"): {
                "nodes": [
                    {
                        "id": node_id,
                        "display_name": "Atlas",
                        "hostname": "spark-1",
                    }
                ]
            },
            ("GET", path): [
                _fleet_log_response(node_id, follow=True),
                _fleet_log_response(other_node_id, follow=True),
                _fleet_log_response(node_id, follow=False),
            ],
        }
    )

    status, payload = run(
        (
            "fleet",
            "loginfo",
            "Atlas",
            "--recipe",
            "vllm",
            "--source",
            "job",
            "--follow",
            "--timeout-seconds",
            "0.2",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert payload["node_id"] == node_id
    assert [call[1] for call in client.calls] == ["/api/fleet", path, path, path]
    first_query = client.calls[-2][3]
    second_query = client.calls[-1][3]
    assert isinstance(first_query, dict)
    assert isinstance(second_query, dict)
    assert second_query == first_query
    assert second_query["recipe"] == "vllm"
    assert second_query["source"] == "job"


def test_fleet_loginfo_validates_identity_even_for_a_canonical_selector() -> None:
    node_id = "spk_" + "1" * 32
    other_node_id = "spk_" + "2" * 32
    path = f"/api/fleet/{node_id}/loginfo"
    client = FakeClient(
        {
            ("GET", path): [
                _fleet_log_response(other_node_id, follow=False),
                _fleet_log_response(node_id, follow=False),
            ]
        }
    )

    status, payload = run(("fleet", "loginfo", node_id, "--json"), client)

    assert status == 0
    assert payload["node_id"] == node_id
    assert [call[1] for call in client.calls] == [path, path]


def test_fleet_log_follow_false_returns_the_retained_snapshot_without_timeout() -> None:
    node_id = "spk_" + "1" * 32
    path = f"/api/fleet/{node_id}/loginfo"
    response = _fleet_log_response(node_id, follow=False)
    client = FakeClient({("GET", path): response})

    status, payload = run(
        (
            "fleet",
            "loginfo",
            node_id,
            "--follow",
            "--timeout-seconds",
            "0.05",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert payload == response
    assert [call[1] for call in client.calls] == [path]


def test_fleet_log_follow_stops_when_owner_changes_to_retained_snapshot() -> None:
    node_id = "spk_" + "1" * 32
    path = f"/api/fleet/{node_id}/loginfo"
    retained = _fleet_log_response(node_id, follow=False)
    client = FakeClient(
        {
            ("GET", path): [
                _fleet_log_response(node_id, follow=True),
                retained,
            ]
        }
    )

    status, payload = run(
        (
            "fleet",
            "loginfo",
            node_id,
            "--follow",
            "--timeout-seconds",
            "0.5",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert payload == retained
    assert [call[1] for call in client.calls] == [path, path]


def test_fleet_log_reconnect_uses_resolved_node_id_and_keeps_log_scope() -> None:
    node_id = "spk_" + "1" * 32
    path = f"/api/fleet/{node_id}/loginfo"
    client = FakeClient(
        {
            ("GET", "/api/fleet"): {
                "nodes": [{"id": node_id, "display_name": "Atlas"}]
            },
            ("GET", path): _fleet_log_response(node_id, follow=True),
        }
    )

    status, payload = run(
        (
            "fleet",
            "loginfo",
            "Atlas",
            "--since",
            "1h",
            "--recipe",
            "vllm",
            "--source",
            "job",
            "--follow",
            "--timeout-seconds",
            "0",
            "--json",
        ),
        client,
    )

    assert status == 2
    observation = payload["observation"]
    assert isinstance(observation, dict)
    assert observation["path"] == path
    reconnect = str(observation["reconnect_command"])
    assert f"fleet loginfo {node_id}" in reconnect
    assert "Atlas" not in reconnect
    assert "--since 1h" in reconnect
    assert "--recipe vllm" in reconnect
    assert "--source job" in reconnect
    assert observation["timeout_seconds"] == 0


def _fleet_log_with_entries(node_id: str) -> dict[str, object]:
    response = _fleet_log_response(node_id, follow=False)
    response["entries"] = [
        {
            "observed_at": "2026-09-24T10:00:01Z",
            "level": "info",
            "source": "agent",
            "message": "first retained line",
        },
        {
            "observed_at": "2026-09-24T10:00:02Z",
            "level": "warning",
            "source": "job",
            "message": "second retained line",
        },
    ]
    return response


def test_fleet_loginfo_without_follow_prints_each_line_once_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    node_id = "spk_" + "1" * 32
    path = f"/api/fleet/{node_id}/loginfo"
    client = FakeClient({("GET", path): _fleet_log_with_entries(node_id)})

    status = cli.main(("fleet", "loginfo", node_id), control_client=client)

    captured = capsys.readouterr()
    assert status == 0
    for line in ("first retained line", "second retained line"):
        assert captured.out.count(line) == 1
        assert line not in captured.err


def test_fleet_upgrade_follows_exact_job_and_stops_at_operator_blocker() -> None:
    job_id = "upgrade-job"
    request_key = "11111111-1111-4111-8111-111111111111"
    node_a = "spk_" + "a" * 32
    node_b = "spk_" + "b" * 32
    client = FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): {
                "action": "upgrade",
                "state": "queued",
                "operation_id": job_id,
                "plan_digest": "a" * 64,
                "request_key": request_key,
                "targets": [node_a, node_b],
            },
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": "needs-operator",
                "kind": "agent-upgrade",
                "targets": [node_a, node_b],
                "status_reason": f"Spark {node_a} requires operator review",
                "operations": [
                    {
                        "id": "operation-a",
                        "node_id": node_a,
                        "state": "needs-operator",
                    }
                ],
                "progress": {"running": 0, "completed": 0, "failed": 1, "total": 1},
            },
        }
    )

    status, result = run(
        (
            "fleet",
            "upgrade",
            "--all",
            "--yes",
            "--request-key",
            request_key,
            "--json",
            "--interval-seconds",
            "0.01",
        ),
        client,
    )

    assert status == 2
    assert result["status_reason"] == f"Spark {node_a} requires operator review"
    assert [call[1] for call in client.calls] == [
        "/api/fleet/upgrade",
        f"/api/jobs/{job_id}",
    ]
    assert client.calls[-1][3] == {"limit": 100}
    assert isinstance(result["operations"], list)
    assert len(result["operations"]) == 1


def _sticky_upgrade_follow(
    snapshot_overrides: dict[str, object], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(observation_module, "time", _InjectedClock())
    job_id = "upgrade-job"
    request_key = "44444444-4444-4444-8444-444444444444"
    nodes = ["spk_" + letter * 32 for letter in "abcd"]
    states = ("running", "queued", "queued", "needs-operator")
    client = FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): {
                "action": "upgrade",
                "state": "queued",
                "operation_id": job_id,
                "plan_digest": "a" * 64,
                "request_key": request_key,
                "targets": nodes,
            },
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": "queued",
                "kind": "agent-upgrade",
                "targets": nodes,
                "operations": [
                    {"id": f"operation-{node[4]}", "node_id": node, "state": state}
                    for node, state in zip(nodes, states, strict=True)
                ],
                # The Controller counts queued and running work as running.
                "progress": {"running": 3, "completed": 0, "failed": 0, "total": 4},
                **snapshot_overrides,
            },
        }
    )
    return run(
        (
            "fleet",
            "upgrade",
            "--all",
            "--yes",
            "--request-key",
            request_key,
            "--json",
            "--interval-seconds",
            "0.01",
            "--timeout-seconds",
            "1",
        ),
        client,
    )


def test_fleet_upgrade_follows_parallel_sparks_without_a_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a follow validator that counts operations: every Spark upgrades
    at once, so 2 queued and 1 running operation is the normal mid-rollout view.
    It must be adopted; rejecting it left the receipt and an observation error."""
    _status, result = _sticky_upgrade_follow({}, monkeypatch)

    observation = result["observation"]
    assert isinstance(observation, dict)
    assert observation["status"] == "timed_out"
    assert "error" not in observation
    inner = result["result"]
    assert isinstance(inner, dict)
    assert len(inner["operations"]) == 4


def test_fleet_upgrade_timeout_reports_the_job_as_still_running(
    vonkctl_json, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a timed-out watch that shows a finished Spark as the outcome:
    one Spark succeeded, one is still queued, so the job is not done."""
    monkeypatch.setattr(observation_module, "time", _InjectedClock())
    job_id = "upgrade-job"
    request_key = "55555555-5555-4555-8555-555555555555"
    nodes = ["spk_" + letter * 32 for letter in "ab"]
    client = FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): {
                "action": "upgrade",
                "state": "queued",
                "operation_id": job_id,
                "plan_digest": "a" * 64,
                "request_key": request_key,
                "targets": nodes,
            },
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": "queued",
                "kind": "agent-upgrade",
                "targets": nodes,
                "operations": [
                    {"id": "operation-a", "node_id": nodes[0], "state": "queued"},
                    {"id": "operation-b", "node_id": nodes[1], "state": "succeeded"},
                ],
                "progress": {"running": 1, "completed": 1, "failed": 0, "total": 2},
            },
        }
    )

    status, document = vonkctl_json(
        "fleet",
        "upgrade",
        "--all",
        "--yes",
        "--request-key",
        request_key,
        "--interval-seconds",
        "0.01",
        "--timeout-seconds",
        "1",
        control_client=client,
    )

    assert status == 2
    assert document["observation"]["status"] == "timed_out"
    assert document["result"]["state"] == "queued"


def test_fleet_upgrade_follow_still_refuses_another_jobs_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The job, kind and target identity checks stay."""
    _status, result = _sticky_upgrade_follow(
        {"targets": ["spk_" + "f" * 32]}, monkeypatch
    )

    observation = result["observation"]
    assert isinstance(observation, dict)
    assert observation["error"]
    inner = result["result"]
    assert isinstance(inner, dict)
    assert "operations" not in inner


def test_fleet_upgrade_replays_lost_post_with_same_request_key() -> None:
    request_key = "22222222-2222-4222-8222-222222222222"
    client = FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): [
                OSError("response lost after acceptance"),
                {
                    "action": "upgrade",
                    "state": "queued",
                    "operation_id": "upgrade-job",
                    "plan_digest": "a" * 64,
                    "request_key": request_key,
                    "targets": [],
                },
            ]
        }
    )

    status, _result = run(
        (
            "fleet",
            "upgrade",
            "--all",
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert [call[0:2] for call in client.calls] == [
        ("POST", "/api/fleet/upgrade"),
        ("POST", "/api/fleet/upgrade"),
    ]
    assert client.calls[0][2] == client.calls[1][2]
    request_body = client.calls[0][2]
    assert request_body is not None
    assert request_body["request_key"] == request_key


@pytest.mark.parametrize("unreadable", [False, True])
def test_fleet_upgrade_unknown_acceptance_keeps_exact_request_for_fresh_observer(
    unreadable: bool,
) -> None:
    request_key = "22222222-2222-4222-8222-222222222222"
    client = FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): (
                ControlMalformedResponse("unreadable receipt")
                if unreadable
                else [
                    OSError("connection reset after acceptance"),
                    OSError("connection reset after replay"),
                ]
            )
        }
    )

    status, result = run(
        (
            "fleet",
            "upgrade",
            "--all",
            "--yes",
            "--request-key",
            request_key,
            "--json",
        ),
        client,
    )

    assert status == 2
    assert result["result"] == {}
    observation = result["observation"]
    assert isinstance(observation, dict)
    assert request_key in observation["reconnect_command"]
    assert len(client.calls) == 2
    assert all(call[2] == client.calls[0][2] for call in client.calls)

    client.responses[("POST", "/api/fleet/upgrade")] = {
        "action": "upgrade",
        "operation_id": "original-job",
        "plan_digest": "a" * 64,
        "request_key": request_key,
        "targets": [],
    }
    status, fresh = run(
        (
            "fleet",
            "upgrade",
            "--all",
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert fresh["operation_id"] == "original-job"
