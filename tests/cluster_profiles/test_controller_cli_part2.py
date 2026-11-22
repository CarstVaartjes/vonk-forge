from __future__ import annotations

from io import StringIO

import pytest
from controller_cli_support import (
    _RUN_DIGEST,
    _RUN_KEY,
    FakeClient,
    _accepted_profile_load,
    _Prompt,
    _run_preview,
    run,
)
from library_route_fixtures import _recipe_projection

from cluster_profiles import cli, controller_cli
from cluster_profiles.control_client import (
    ControlConflict,
    ControlForbidden,
    ControlNotFound,
    ControlTransportError,
    ControlUnavailable,
)


def test_fleet_remove_resolves_and_confirms_stable_node_before_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node_id = "spk_" + "a" * 32
    client = FakeClient(
        {
            ("GET", "/api/fleet"): {
                "nodes": [{"id": node_id, "display_name": "Atlas"}]
            },
            ("GET", f"/api/fleet/{node_id}"): {
                "id": node_id,
                "display_name": "Atlas",
            },
            ("POST", f"/api/fleet/{node_id}/remove"): {
                "action": "remove",
                "state": "accepted",
                "node_id": node_id,
            },
        }
    )
    error_stream = StringIO()

    class TTYInput(StringIO):
        def isatty(self) -> bool:
            return True

        def readline(self, size: int = -1) -> str:
            assert [call[:2] for call in client.calls] == [
                ("GET", "/api/fleet"),
                ("GET", f"/api/fleet/{node_id}"),
            ]
            assert node_id in error_stream.getvalue()
            return super().readline(size)

    class TTYOutput(StringIO):
        def isatty(self) -> bool:
            return True

    input_stream = TTYInput("yes\n")
    error_stream = TTYOutput()
    monkeypatch.setattr(controller_cli.confirmation.sys, "stdin", input_stream)
    monkeypatch.setattr(controller_cli.confirmation.sys, "stderr", error_stream)
    monkeypatch.setattr(cli, "begin_interactive_update_check", lambda: None)

    status = cli.main(
        ("fleet", "remove", "Atlas"),
        control_client=client,
        request_id_factory=lambda: "11111111-1111-4111-8111-111111111111",
    )

    assert status == 0
    assert [call[:2] for call in client.calls] == [
        ("GET", "/api/fleet"),
        ("GET", f"/api/fleet/{node_id}"),
        ("POST", f"/api/fleet/{node_id}/remove"),
    ]


def test_fleet_remove_requires_yes_for_noninteractive_json() -> None:
    node_id = "spk_" + "b" * 32
    client = FakeClient(
        {
            ("GET", "/api/fleet"): {
                "nodes": [{"id": node_id, "display_name": "Studio Spark"}]
            },
            ("GET", f"/api/fleet/{node_id}"): {
                "id": node_id,
                "display_name": "Studio Spark",
            },
        }
    )

    status, payload = run(
        ("--no-input", "fleet", "remove", "Studio Spark", "--json"), client
    )

    error = payload.get("error")
    assert status == 2
    assert isinstance(error, str) and "Pass --yes" in error and node_id in error
    assert [call[:2] for call in client.calls] == [
        ("GET", "/api/fleet"),
        ("GET", f"/api/fleet/{node_id}"),
    ]


def test_fleet_remove_yes_posts_to_the_resolved_stable_identity() -> None:
    node_id = "spk_" + "c" * 32
    client = FakeClient(
        {
            ("GET", "/api/fleet"): {
                "nodes": [{"id": node_id, "display_name": "Lab Spark"}]
            },
            ("GET", f"/api/fleet/{node_id}"): {
                "id": node_id,
                "display_name": "Lab Spark",
            },
            ("POST", f"/api/fleet/{node_id}/remove"): {
                "action": "remove",
                "state": "accepted",
                "node_id": node_id,
            },
        }
    )

    status, payload = run(
        ("--no-input", "fleet", "remove", "Lab Spark", "--yes", "--json"),
        client,
    )

    assert status == 0 and payload["node_id"] == node_id
    assert [call[:2] for call in client.calls] == [
        ("GET", "/api/fleet"),
        ("GET", f"/api/fleet/{node_id}"),
        ("POST", f"/api/fleet/{node_id}/remove"),
    ]


def test_model_cancel_requires_consent_and_reuses_its_stable_identity() -> None:
    operation_id = "11111111-1111-4111-8111-111111111121"
    cancel_key = "11111111-1111-4111-8111-111111111122"
    path = f"/api/model/operations/{operation_id}/cancel"
    response = {
        "state": "observing",
        "action": "download",
        "operation_id": operation_id,
        "request_key": "11111111-1111-4111-8111-111111111123",
        "selector": "qwen",
        "cancellation": {
            "request_key": cancel_key,
            "actor": "operator",
            "reason": "switching to another model",
            "requested_at": "2026-09-10T10:00:00+00:00",
        },
    }
    client = FakeClient({("POST", path): response})
    assert run(("model", "cancel", operation_id, "--json"), client)[0] == 2
    assert client.calls == []

    status, payload = run(
        (
            "model",
            "cancel",
            operation_id,
            "--yes",
            "--request-key",
            cancel_key,
            "--reason",
            "switching to another model",
            "--detach",
            "--json",
        ),
        client,
    )
    assert status == 0 and payload["state"] == "observing"
    assert client.calls == [
        (
            "POST",
            path,
            {
                "request_key": cancel_key,
                "reason": "switching to another model",
            },
            None,
        )
    ]


def test_model_cancel_reconciles_a_lost_acceptance_before_replay() -> None:
    operation_id = "11111111-1111-4111-8111-111111111131"
    cancel_key = "11111111-1111-4111-8111-111111111132"
    path = f"/api/model/operations/{operation_id}/cancel"
    lookup = f"/api/model/operations/{operation_id}"
    receipt = {
        "state": "cancelled",
        "action": "download",
        "operation_id": operation_id,
        "request_key": "11111111-1111-4111-8111-111111111133",
        "selector": "qwen",
        "cancellation": {
            "request_key": cancel_key,
            "actor": "operator",
            "reason": "operator request",
            "requested_at": "2026-09-10T10:00:00+00:00",
        },
    }
    client = FakeClient(
        {
            ("POST", path): [
                ControlTransportError("response lost"),
                receipt,
            ],
            ("GET", lookup): receipt,
        }
    )
    status, payload = run(
        (
            "model",
            "cancel",
            operation_id,
            "--yes",
            "--request-key",
            cancel_key,
            "--reason",
            "operator request",
            "--detach",
            "--json",
        ),
        client,
    )
    assert status == 0 and payload["state"] == "cancelled", payload
    assert [call[0] for call in client.calls] == ["POST", "GET"]
    assert client.calls[0][2] == {
        "request_key": cancel_key,
        "reason": "operator request",
    }


def test_profile_add_autosaves_whole_fleet_authoring_shape_with_revision() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile/2/definition"): {
                "id": "11111111-1111-4111-8111-111111111111",
                "number": 2,
                "revision": 7,
                "definition": {"name": "Coding", "assignments": []},
            },
            ("GET", "/api/recipe/library"): {
                "recipes": [
                    _recipe_projection("vonk-forge/recipe-uuid", "Recipe UUID")
                ],
                "next_cursor": None,
            },
            ("GET", "/api/fleet"): {
                "nodes": [
                    {
                        "id": "spk_" + "a" * 32,
                        "display_name": "Atlas",
                        "hostname": "atlas",
                    },
                    {
                        "id": "spk_" + "b" * 32,
                        "display_name": "Boreal",
                        "hostname": "boreal",
                    },
                ]
            },
            ("PUT", "/api/profile/2"): {
                "number": 2,
                "revision": 8,
                "warnings": ["incomplete group"],
            },
        }
    )
    status, payload = run(
        (
            "--profile",
            "2",
            "profile",
            "add",
            "Recipe UUID",
            "--spark",
            "Boreal",
            "--spark",
            "Atlas",
            "--state",
            "running",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert payload["revision"] == 8
    # The definition, recipe and Spark reads are independent and run at once.
    reads, writes = client.calls[:3], client.calls[3:]
    assert sorted(reads, key=lambda call: call[1]) == sorted(
        [
            ("GET", "/api/profile/2/definition", None, None),
            (
                "GET",
                "/api/recipe/library",
                None,
                {"limit": 512, "sort": "name", "assess": False},
            ),
            ("GET", "/api/fleet", None, None),
        ],
        key=lambda call: call[1],
    )
    assert writes == [
        (
            "PUT",
            "/api/profile/2",
            {
                "assignments": [
                    {
                        "recipe_selector": "vonk-forge/recipe-uuid",
                        "spark_ids": ["spk_" + "a" * 32, "spk_" + "b" * 32],
                        "desired_state": "running",
                    }
                ],
                "name": "Coding",
                "expected_revision": 7,
            },
            None,
        ),
    ]


def test_profile_add_rejects_an_ambiguous_spark_name() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile/1/definition"): {
                "id": "11111111-1111-4111-8111-111111111111",
                "number": 1,
                "revision": 1,
                "definition": {"name": "Default", "assignments": []},
            },
            ("GET", "/api/recipe/library"): {
                "recipes": [_recipe_projection("vonk-forge/qwen-code", "Qwen Code")],
                "next_cursor": None,
            },
            ("GET", "/api/fleet"): {
                "nodes": [
                    {
                        "id": "spk_" + "a" * 32,
                        "display_name": "Atlas",
                        "hostname": "atlas-a",
                    },
                    {
                        "id": "spk_" + "b" * 32,
                        "display_name": "Atlas",
                        "hostname": "atlas-b",
                    },
                ]
            },
        }
    )

    status, payload = run(
        (
            "--profile",
            "1",
            "profile",
            "add",
            "vonk-forge/qwen-code",
            "--spark",
            "Atlas",
            "--json",
        ),
        client,
    )

    assert status == 2
    ambiguous_error = payload["error"]
    assert isinstance(ambiguous_error, str)
    assert "ambiguous spark selector" in ambiguous_error
    assert {call[1] for call in client.calls} == {
        "/api/profile/1/definition",
        "/api/fleet",
    }


def test_profile_remove_resolves_recipe_title_to_canonical_selector() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile/1/definition"): {
                "id": "11111111-1111-4111-8111-111111111111",
                "number": 1,
                "revision": 3,
                "definition": {
                    "name": "Default",
                    "assignments": [
                        {
                            "recipe_selector": "vonk-forge/recipe-uuid",
                            "assignment_name": None,
                            "spark_ids": ["spk_" + "a" * 32],
                        }
                    ],
                },
            },
            ("GET", "/api/recipe/library"): {
                "recipes": [
                    _recipe_projection("vonk-forge/recipe-uuid", "Recipe UUID")
                ],
                "next_cursor": None,
            },
            ("PUT", "/api/profile/1"): {"number": 1, "revision": 4},
        }
    )

    status, payload = run(
        ("--profile", "1", "profile", "remove", "Recipe UUID", "--json"), client
    )

    assert status == 0
    assert payload["revision"] == 4
    profile_write = client.calls[-1][2]
    assert profile_write is not None
    assert profile_write["assignments"] == []


def _accepted_profile_cancellation(
    identity: str, request_key: str
) -> dict[str, object]:
    return {
        "id": identity,
        "state": "running",
        "cancellation": {
            "request_key": request_key,
            "actor": "administrator",
            "cause": "operator",
            "state": "observing",
        },
    }


def test_profile_cancel_targets_one_application_with_replayable_request_identity() -> (
    None
):
    application_id = "11111111-1111-4111-8111-111111111111"
    request_key = "22222222-2222-4222-8222-222222222222"
    unconfirmed = FakeClient({})
    status, _ = run(
        (
            "--profile",
            "4",
            "profile",
            "cancel",
            application_id,
            "--request-key",
            request_key,
            "--json",
        ),
        unconfirmed,
    )
    assert status == 2
    assert unconfirmed.calls == []

    client = FakeClient(
        {
            (
                "POST",
                f"/api/profile/applications/{application_id}/cancel",
            ): {
                **_accepted_profile_cancellation(application_id, request_key),
            }
        }
    )

    status, result = run(
        (
            "--profile",
            "4",
            "profile",
            "cancel",
            application_id,
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert result["id"] == application_id
    assert client.calls == [
        (
            "POST",
            f"/api/profile/applications/{application_id}/cancel",
            {"profile_number": 4, "request_key": request_key},
            None,
        )
    ]


def test_profile_delete_confirms_then_repeats_while_the_cancel_finishes() -> None:
    path = "/api/profile/4/delete"
    refused = FakeClient({})
    status, _ = run(("--profile", "4", "profile", "delete", "--json"), refused)
    assert status == 2
    assert refused.calls == []

    client = FakeClient(
        {
            ("POST", path): [
                {"number": 4, "outcome": "cancelling"},
                {"number": 4, "outcome": "deleted"},
            ]
        }
    )
    status, result = run(
        (
            "--profile",
            "4",
            "profile",
            "delete",
            "--yes",
            "--expected-revision",
            "2",
            "--json",
        ),
        client,
    )
    assert status == 0
    assert result["outcome"] == "deleted"
    assert [call[:3] for call in client.calls] == [
        ("POST", path, {"expected_revision": 2})
    ] * 2


_CANCEL_ARGV = (
    "--profile",
    "4",
    "profile",
    "cancel",
    "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
    "--yes",
    "--request-key",
    "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
    "--detach",
    "--json",
)


@pytest.mark.parametrize(
    ("code", "refused"),
    [
        ("profile.request_invalid", True),
        # An older Controller answers every profile conflict 409 with its
        # generic code: acceptance stays unknown and is looked up.
        ("controller.conflict", False),
        (None, False),
    ],
)
def test_profile_cancel_fails_fast_only_on_a_typed_refusal(code, refused) -> None:
    application_id, request_key = _CANCEL_ARGV[4], _CANCEL_ARGV[7]
    path = f"/api/profile/applications/{application_id}/cancel"
    lookup = f"/api/profile/applications/{application_id}/cancellations/{request_key}"
    client = FakeClient(
        {
            ("POST", path): ControlConflict(
                409, "request key belongs to another actor", code=code
            ),
            ("GET", lookup): _accepted_profile_cancellation(
                application_id, request_key
            ),
        }
    )

    status, result = run(_CANCEL_ARGV, client)

    if refused:
        assert status == 2
        assert result["detail"] == "request key belongs to another actor"
        assert [call[:2] for call in client.calls] == [("POST", path)]
    else:
        assert status == 0
        assert ("GET", lookup) in [call[:2] for call in client.calls]


def test_profile_cancel_waits_out_a_busy_controller_and_replays() -> None:
    application_id, request_key = _CANCEL_ARGV[4], _CANCEL_ARGV[7]
    path = f"/api/profile/applications/{application_id}/cancel"
    lookup = f"/api/profile/applications/{application_id}/cancellations/{request_key}"
    client = FakeClient(
        {
            ("POST", path): [
                ControlUnavailable(503, "profile admission is busy", 0),
                _accepted_profile_cancellation(application_id, request_key),
            ],
            ("GET", lookup): ControlNotFound(404, "not committed"),
        }
    )

    status, result = run(_CANCEL_ARGV, client)

    assert status == 0
    assert result["id"] == application_id
    assert [call[:2] for call in client.calls] == [
        ("POST", path),
        ("GET", lookup),
        ("POST", path),
    ]


@pytest.mark.parametrize("review", [False, True])
def test_profile_load_fails_fast_on_a_typed_refusal(review, monkeypatch) -> None:
    refusal = ControlConflict(
        409, "Profile fleet scope changed during admission", code="profile.stale_plan"
    )
    client = FakeClient(
        {
            ("POST", "/api/profile/1/preview"): _run_preview("b" * 64, _RUN_DIGEST),
            ("POST", "/api/profile/1/load"): refusal,
        }
    )
    _Prompt(monkeypatch, "yes")
    argv = ("--profile", "1", "profile", "load", "--detach")

    status = cli.main(
        argv if review else (*argv, "--yes"),
        control_client=client,
        request_id_factory=lambda: _RUN_KEY,
    )

    assert status == 2
    assert not [call for call in client.calls if call[0] == "GET"]
    assert [call[1] for call in client.calls].count("/api/profile/1/load") == 1


def test_lost_profile_cancel_response_resolves_exact_owner_receipt() -> None:
    application_id = "55555555-5555-4555-8555-555555555555"
    request_key = "66666666-6666-4666-8666-666666666666"
    path = f"/api/profile/applications/{application_id}/cancel"
    lookup = f"/api/profile/applications/{application_id}/cancellations/{request_key}"
    client = FakeClient(
        {
            ("POST", path): ControlTransportError("connection reset after commit"),
            ("GET", lookup): _accepted_profile_cancellation(
                application_id, request_key
            ),
        }
    )

    status, result = run(
        (
            "--profile",
            "4",
            "profile",
            "cancel",
            application_id,
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0
    cancellation = result["cancellation"]
    assert isinstance(cancellation, dict)
    assert cancellation["request_key"] == request_key
    assert [call[:2] for call in client.calls] == [("POST", path), ("GET", lookup)]
    assert client.calls[0][2] == {"profile_number": 4, "request_key": request_key}


def test_lost_profile_cancel_replays_only_after_exact_owner_is_absent() -> None:
    application_id = "77777777-7777-4777-8777-777777777777"
    request_key = "88888888-8888-4888-8888-888888888888"
    path = f"/api/profile/applications/{application_id}/cancel"
    lookup = f"/api/profile/applications/{application_id}/cancellations/{request_key}"
    receipt = _accepted_profile_cancellation(application_id, request_key)
    client = FakeClient(
        {
            ("POST", path): [
                ControlTransportError("connection reset"),
                receipt,
            ],
            ("GET", lookup): ControlNotFound(404, "not committed"),
        }
    )

    status, result = run(
        (
            "--profile",
            "4",
            "profile",
            "cancel",
            application_id,
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert result["id"] == application_id
    posts = [call for call in client.calls if call[0] == "POST"]
    assert len(posts) == 2
    assert (
        posts[0][2]
        == posts[1][2]
        == {
            "profile_number": 4,
            "request_key": request_key,
        }
    )
    assert [call[:2] for call in client.calls] == [
        ("POST", path),
        ("GET", lookup),
        ("POST", path),
    ]


@pytest.mark.parametrize("repair", [True, False])
def test_lost_profile_cancel_reconciles_without_replaying_an_effect(repair) -> None:
    application_id = "99999999-9999-4999-8999-999999999999"
    request_key = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    path = f"/api/profile/applications/{application_id}/cancel"
    lookup = f"/api/profile/applications/{application_id}/cancellations/{request_key}"
    client = FakeClient(
        {
            ("POST", path): ControlTransportError("connection reset"),
            ("GET", lookup): [
                _accepted_profile_cancellation(
                    application_id, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                ),
                _accepted_profile_cancellation(
                    application_id,
                    request_key if repair else "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                ),
            ],
        }
    )

    status, result = run(
        (
            "--profile",
            "4",
            "profile",
            "cancel",
            application_id,
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == (0 if repair else 2)
    assert [call[0] for call in client.calls].count("POST") == 1
    assert all(call[:2] == ("GET", lookup) for call in client.calls[1:])
    if repair:
        assert result["id"] == application_id
    else:
        observation = result["observation"]
        assert isinstance(observation, dict) and observation["status"] == "timed_out"
    fresh_key = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    client.responses[("POST", path)] = _accepted_profile_cancellation(
        application_id, fresh_key
    )
    status, result = run(
        (
            "--profile",
            "4",
            "profile",
            "cancel",
            application_id,
            "--yes",
            "--request-key",
            fresh_key,
            "--detach",
            "--json",
        ),
        client,
    )
    assert status == 0 and result["id"] == application_id


def test_profile_load_follows_the_application_it_submitted() -> None:
    """A load is followed by its own durable application identity.

    The numbered progress route answers with whichever application is latest,
    so following it would report a later load's progress and could report a
    success this invocation never produced.
    """

    application_id = "33333333-3333-4333-8333-333333333333"
    other_application = "44444444-4444-4444-8444-444444444444"
    client = FakeClient(
        {
            ("POST", "/api/profile/1/load"): _accepted_profile_load(
                application_id, "queued"
            ),
            (
                "GET",
                f"/api/profile/applications/{application_id}",
            ): _accepted_profile_load(application_id, "succeeded"),
            ("GET", "/api/profile/1/progress"): _accepted_profile_load(
                other_application, "failed"
            ),
        }
    )
    status, payload = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--yes",
            "--json",
        ),
        client,
    )

    assert status == 0 and payload["state"] == "succeeded"
    assert [call[1] for call in client.calls] == [
        "/api/profile/1/load",
        f"/api/profile/applications/{application_id}",
    ]
    assert client.calls[0][2] == {
        "request_key": "11111111-1111-4111-8111-111111111111",
    }


def test_profile_load_without_durable_identity_does_not_follow_a_numbered_route() -> (
    None
):
    client = FakeClient(
        {
            ("POST", "/api/profile/1/load"): {"state": "queued"},
            ("GET", "/api/profile/1/progress"): {"state": "succeeded"},
        }
    )
    status, payload = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--yes",
            "--json",
        ),
        client,
    )

    assert status != 0
    assert [call[0] for call in client.calls] == ["POST", "GET", "GET", "GET", "GET"]
    observation = payload["observation"]
    assert isinstance(observation, dict) and observation["status"] == "timed_out"
    assert not any(call[1].endswith("/progress") for call in client.calls)

    fresh_key = "22222222-2222-4222-8222-222222222222"
    client.responses[("POST", "/api/profile/1/load")] = {
        "id": "33333333-3333-4333-8333-333333333333",
        "request_key": fresh_key,
        "state": "succeeded",
    }
    status, result = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--yes",
            "--request-key",
            fresh_key,
            "--detach",
            "--json",
        ),
        client,
    )
    assert status == 0 and result["request_key"] == fresh_key


def test_profile_progress_follow_stops_at_current_terminal_state() -> None:
    identity = "33333333-3333-4333-8333-333333333333"
    client = FakeClient(
        {
            ("GET", "/api/profile/1/progress"): {
                "id": identity,
                "state": "running",
                "progress": {"step": 1, "steps": 2},
            },
            ("GET", f"/api/profile/applications/{identity}"): {
                "id": identity,
                "state": "succeeded",
                "progress": {"step": 2, "steps": 2},
            },
        }
    )
    status, payload = run(
        ("profile", "progress", "--follow", "--interval-seconds", "0.1", "--json"),
        client,
    )
    assert status == 0 and payload["state"] == "succeeded"
    assert len(client.calls) == 2


def test_profile_application_selector_uses_owner_without_a_second_profile_gate() -> (
    None
):
    application_id = "33333333-3333-4333-8333-333333333333"
    selected_profile_id = "11111111-1111-4111-8111-111111111111"
    other_profile_id = "22222222-2222-4222-8222-222222222222"
    client = FakeClient(
        {
            ("GET", "/api/profile/4"): {
                "id": selected_profile_id,
                "number": 4,
            },
            ("GET", f"/api/profile/applications/{application_id}"): {
                "id": application_id,
                "profile_id": other_profile_id,
                "state": "running",
            },
        }
    )

    status, payload = run(
        (
            "--profile",
            "4",
            "profile",
            "progress",
            "--load",
            application_id,
            "--json",
        ),
        client,
    )

    assert status == 0 and payload["id"] == application_id
    assert payload["profile_id"] == other_profile_id
    assert [call[1] for call in client.calls] == [
        f"/api/profile/applications/{application_id}",
    ]


def test_global_profile_application_selector_needs_no_profile_selection() -> None:
    application_id = "33333333-3333-4333-8333-333333333333"
    client = FakeClient(
        {
            ("GET", f"/api/profile/applications/{application_id}"): {
                "id": application_id,
                "profile_id": "22222222-2222-4222-8222-222222222222",
                "state": "failed",
            }
        }
    )

    status, payload = run(
        ("profile", "progress", "--load", application_id, "--json"),
        client,
    )

    assert status == 0 and payload["id"] == application_id
    assert [call[1] for call in client.calls] == [
        f"/api/profile/applications/{application_id}"
    ]


def test_profile_application_selector_accepts_selected_profile_owner() -> None:
    application_id = "33333333-3333-4333-8333-333333333333"
    selected_profile_id = "11111111-1111-4111-8111-111111111111"
    client = FakeClient(
        {
            ("GET", "/api/profile/4"): {
                "id": selected_profile_id,
                "number": 4,
            },
            ("GET", f"/api/profile/applications/{application_id}"): {
                "id": application_id,
                "profile_id": selected_profile_id,
                "state": "failed",
            },
        }
    )

    status, payload = run(
        (
            "--profile",
            "4",
            "profile",
            "progress",
            "--load",
            application_id,
            "--json",
        ),
        client,
    )

    assert status == 0 and payload["id"] == application_id


def test_profile_progress_follow_discards_wrong_identity_until_original_repairs() -> (
    None
):
    selected_application = "33333333-3333-4333-8333-333333333333"
    replacement_application = "44444444-4444-4444-8444-444444444444"
    path = f"/api/profile/applications/{selected_application}"
    client = FakeClient(
        {
            ("GET", path): [
                {"id": selected_application, "state": "running"},
                {"id": replacement_application, "state": "succeeded"},
                {"id": selected_application, "state": "succeeded"},
            ]
        }
    )

    status, payload = run(
        (
            "profile",
            "progress",
            "--load",
            selected_application,
            "--follow",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )

    assert status == 0 and payload["id"] == selected_application
    assert [call[1] for call in client.calls] == [path, path, path]


def test_initial_application_identity_is_reobserved_until_original_repairs() -> None:
    requested_application = "33333333-3333-4333-8333-333333333333"
    returned_application = "44444444-4444-4444-8444-444444444444"
    client = FakeClient(
        {
            (
                "GET",
                f"/api/profile/applications/{requested_application}",
            ): [
                {"id": returned_application, "state": "succeeded"},
                {"id": requested_application, "state": "succeeded"},
            ]
        }
    )

    status, payload = run(
        (
            "profile",
            "progress",
            "--load",
            requested_application,
            "--json",
        ),
        client,
    )

    assert status == 0 and payload["id"] == requested_application
    assert [call[0] for call in client.calls] == ["GET", "GET"]


def test_follow_survives_lost_connections_and_reports_the_durable_outcome() -> None:
    """A dropped observation must not discard the operation's real result.

    The last confirmed snapshot stays authoritative across two consecutive
    transport failures, and the durable success is still the reported outcome.
    """

    identity = "33333333-3333-4333-8333-333333333333"
    client = FakeClient(
        {
            ("GET", "/api/profile/1/progress"): {"id": identity, "state": "running"},
            ("GET", f"/api/profile/applications/{identity}"): [
                ControlTransportError("connection reset"),
                ControlTransportError("connection reset"),
                {"id": identity, "state": "succeeded"},
            ],
        }
    )
    status, payload = run(
        ("profile", "progress", "--follow", "--interval-seconds", "0.01", "--json"),
        client,
    )

    assert status == 0 and payload["state"] == "succeeded"
    assert "reconnecting" not in payload
    assert len(client.calls) == 4


def test_follow_adopts_the_successor_of_a_superseded_application() -> None:
    """A load the Controller replaced (its own retry) is followed, not reported failed."""

    first = "33333333-3333-4333-8333-333333333333"
    second = "44444444-4444-4444-8444-444444444444"
    client = FakeClient(
        {
            ("GET", "/api/profile/1/progress"): {
                "id": first,
                "request_key": "same-request",
                "state": "running",
            },
            ("GET", f"/api/profile/applications/{first}"): {
                "id": first,
                "state": "superseded",
                "superseded_by": second,
                "request_key": "same-request",
            },
            ("GET", f"/api/profile/applications/{second}"): [
                {"id": second, "retry_of_application_id": first, "state": "running"},
                {"id": second, "retry_of_application_id": first, "state": "succeeded"},
            ],
        }
    )
    status, payload = run(
        ("profile", "progress", "--follow", "--interval-seconds", "0.01", "--json"),
        client,
    )

    assert status == 0
    assert payload["id"] == second and payload["state"] == "succeeded"
    assert payload["supersedes_chain"] == [first]


def test_observation_timeout_names_the_connection_it_lost() -> None:
    """An unreachable Controller is an observation failure, not a run failure."""

    identity = "33333333-3333-4333-8333-333333333333"
    client = FakeClient(
        {
            ("GET", "/api/profile/1/progress"): {"id": identity, "state": "running"},
            ("GET", f"/api/profile/applications/{identity}"): ControlUnavailable(
                503, "control API unavailable"
            ),
        }
    )
    status, payload = run(
        (
            "profile",
            "progress",
            "--follow",
            "--timeout-seconds",
            "1",
            "--interval-seconds",
            "0.05",
            "--json",
        ),
        client,
    )

    assert status == 2
    observation = payload["observation"]
    assert isinstance(observation, dict)
    assert observation["status"] == "timed_out"
    assert observation["reconnecting"] is True
    # The durable operation kept its last observed state; only the observation
    # is reported as incomplete.
    assert payload["result"] == {"id": identity, "state": "running"}


def test_authorization_failure_is_not_retried_as_an_observation() -> None:
    """A permission answer is actionable immediately, not a reconnect."""

    client = FakeClient(
        {
            ("GET", "/api/profile/1/progress"): ControlForbidden(
                403, "insufficient role"
            ),
        }
    )
    status, _payload = run(("profile", "progress", "--follow", "--json"), client)

    assert status != 0
    assert len(client.calls) == 1


def test_lost_mutation_response_still_names_the_request_key() -> None:
    """A submission accepted without a visible answer stays reconcilable.

    The key is created before the request is sent and retained, so the error
    output identifies the one durable operation the operator must inspect.
    """

    client = FakeClient(
        {
            ("POST", "/api/profile/1/load"): ControlTransportError("connection reset"),
        }
    )
    status, payload = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--yes",
            "--json",
        ),
        client,
    )

    assert status != 0
    observation = payload["observation"]
    assert isinstance(observation, dict)
    key = "11111111-1111-4111-8111-111111111111"
    assert observation["path"] == f"/api/profile/1/requests/{key}"
    assert (
        observation["reconnect_command"]
        == f"vonkctl --profile 1 profile progress --request-key {key} --follow"
    )
    assert [call[0] for call in client.calls].count("POST") == 1
    fresh_key = "22222222-2222-4222-8222-222222222222"
    client.responses[("POST", "/api/profile/1/load")] = {
        "id": "33333333-3333-4333-8333-333333333333",
        "request_key": fresh_key,
        "state": "succeeded",
    }
    status, result = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--yes",
            "--request-key",
            fresh_key,
            "--detach",
            "--json",
        ),
        client,
    )
    assert status == 0 and result["request_key"] == fresh_key


def test_accepted_load_with_lost_response_is_reconciled_by_request_key() -> None:
    key = "11111111-1111-4111-8111-111111111111"
    operation = "33333333-3333-4333-8333-333333333333"
    client = FakeClient(
        {
            ("POST", "/api/profile/1/load"): ControlTransportError("connection reset"),
            ("GET", f"/api/profile/1/requests/{key}"): _accepted_profile_load(
                operation, "succeeded"
            ),
        }
    )

    status, payload = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--yes",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert payload["id"] == operation
    assert [call[0] for call in client.calls] == ["POST", "GET"]
