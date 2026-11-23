"""Controller CLI: remove and cancel."""

from __future__ import annotations

from io import StringIO

import pytest
from controller_cli_support import (
    _RUN_DIGEST,
    _RUN_KEY,
    FakeClient,
    _accepted_profile_cancellation,
    _model_detail,
    _Prompt,
    _recipe_removal_receipt,
    _removal_review,
    _run_preview,
    run,
)
from library_route_fixtures import _recipe_projection

from cluster_profiles import cli, controller_cli
from cluster_profiles.control_client import (
    ControlConflict,
    ControlNotFound,
    ControlTransportError,
    ControlUnavailable,
)


def test_model_remove_reconciles_after_lost_acceptance() -> None:
    selector = "qwen"
    request_key = "11111111-1111-4111-8111-111111111111"
    operation_id = "11111111-1111-4111-8111-111111111121"
    _detail, digest = _model_detail(selector)
    receipt = {
        "action": "remove",
        "selector": selector,
        "request_key": request_key,
        "model_content_sha256": digest,
        "operation_id": operation_id,
        "state": "queued",
        "phase": "queued",
        "progress": {"phase": "queued"},
        "transferred_bytes": 0,
    }
    lookup = f"/api/model/requests/{request_key}"
    remove = f"/api/model/{selector}/remove"
    client = FakeClient(
        {
            ("GET", lookup): [
                ControlNotFound(404, "request was not accepted"),
                receipt,
            ],
            (
                "GET",
                f"/api/model/{selector}/remove-review",
            ): _removal_review("model", selector, model_digest=digest),
            ("POST", remove): ControlTransportError("accepted response was lost"),
        }
    )

    status, result = run(
        (
            "model",
            "remove",
            selector,
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0 and result == receipt
    assert [call[:2] for call in client.calls] == [
        ("GET", lookup),
        ("GET", f"/api/model/{selector}/remove-review"),
        ("POST", remove),
        ("GET", lookup),
    ]
    body = client.calls[2][2]
    assert body == {
        "request_key": request_key,
    }


def test_model_remove_reconnects_to_existing_key_before_resolving_current_head() -> (
    None
):
    selector = "qwen"
    request_key = "11111111-1111-4111-8111-111111111111"
    receipt = {
        "action": "remove",
        "selector": selector,
        "request_key": request_key,
        "model_content_sha256": "a" * 64,
        "operation_id": "11111111-1111-4111-8111-111111111121",
        "state": "succeeded",
        "phase": "completed",
        "progress": {"phase": "completed"},
        "transferred_bytes": 0,
    }
    lookup = f"/api/model/requests/{request_key}"
    client = FakeClient({("GET", lookup): receipt})

    status, result = run(
        (
            "model",
            "remove",
            selector,
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0 and result == receipt
    assert [call[:2] for call in client.calls] == [("GET", lookup)]


def test_recipe_remove_reconciles_lost_acceptance_with_the_same_request_key() -> None:
    selector = "vision"
    request_key = "11111111-1111-4111-8111-111111111111"
    receipt = _recipe_removal_receipt(selector, request_key, with_model=False)
    lookup = f"/api/recipe/requests/{request_key}"
    remove = f"/api/recipe/{selector}/remove"
    client = FakeClient(
        {
            ("GET", lookup): [
                ControlNotFound(404, "request was not accepted"),
                receipt,
            ],
            (
                "GET",
                f"/api/recipe/{selector}/remove-review",
            ): _removal_review("recipe", selector, with_model=False),
            ("POST", remove): ControlTransportError("accepted response was lost"),
        }
    )

    status, result = run(
        (
            "recipe",
            "remove",
            selector,
            "--keep-model",
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0 and result == receipt
    assert [call[:2] for call in client.calls] == [
        ("GET", lookup),
        ("GET", f"/api/recipe/{selector}/remove-review"),
        ("POST", remove),
        ("GET", lookup),
    ]
    assert client.calls[2][2] == {
        "request_key": request_key,
        "with_model": False,
    }


def test_recipe_remove_reconnects_to_existing_key_without_posting() -> None:
    selector = "vision"
    request_key = "11111111-1111-4111-8111-111111111111"
    receipt = _recipe_removal_receipt(selector, request_key, with_model=True)
    lookup = f"/api/recipe/requests/{request_key}"
    client = FakeClient({("GET", lookup): receipt})

    status, result = run(
        (
            "recipe",
            "remove",
            selector,
            "--with-model",
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0 and result == receipt
    assert [call[:2] for call in client.calls] == [("GET", lookup)]


@pytest.mark.parametrize(
    ("bad_selector", "bad_with_model"),
    [("another-recipe", False), ("vision", True)],
)
def test_recipe_remove_reconnect_rejects_foreign_selector_or_retention(
    bad_selector: str, bad_with_model: bool
) -> None:
    selector = "vision"
    request_key = "11111111-1111-4111-8111-111111111111"
    receipt = _recipe_removal_receipt(
        bad_selector, request_key, with_model=bad_with_model
    )
    lookup = f"/api/recipe/requests/{request_key}"
    repaired = _recipe_removal_receipt(selector, request_key, with_model=False)
    client = FakeClient({("GET", lookup): [receipt, repaired]})

    status, payload = run(
        (
            "recipe",
            "remove",
            selector,
            "--keep-model",
            "--yes",
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0 and payload == repaired
    assert [call[:2] for call in client.calls] == [("GET", lookup), ("GET", lookup)]


@pytest.mark.parametrize(
    ("noun", "selector", "bad_field", "bad_value"),
    [
        ("model", "qwen", "action", "download"),
        ("model", "qwen", "selector", "other-model"),
        (
            "model",
            "qwen",
            "request_key",
            "22222222-2222-4222-8222-222222222222",
        ),
        ("recipe", "vision", "selector", "other-recipe"),
        (
            "recipe",
            "vision",
            "request_key",
            "22222222-2222-4222-8222-222222222222",
        ),
        ("recipe", "vision", "with_model", True),
    ],
)
def test_cache_remove_rejects_receipt_for_another_intent_before_follow(
    noun: str, selector: str, bad_field: str, bad_value: object
) -> None:
    request_key = "11111111-1111-4111-8111-111111111111"
    operation_id = "11111111-1111-4111-8111-111111111121"
    if noun == "model":
        model_detail, model_digest = _model_detail(selector)
        receipt: dict[str, object] = {
            "action": "remove",
            "selector": selector,
            "request_key": request_key,
            "model_content_sha256": model_digest,
            "operation_id": operation_id,
            "state": "succeeded",
            "phase": "completed",
            "progress": {"phase": "completed"},
            "transferred_bytes": 0,
        }
    else:
        receipt = {
            "action": "remove",
            "selector": selector,
            "request_key": request_key,
            "operation_id": operation_id,
            "recipe_revision_id": "revision-1",
            "with_model": False,
            "state": "succeeded",
            "progress": {"phase": "completed"},
            "reclaimed_bytes": 0,
        }
    receipt[bad_field] = bad_value
    path = f"/api/{noun}/{selector}/remove"
    model_receipt_only_reconnect = (
        noun == "model" and bad_field != "model_content_sha256"
    )
    if noun == "model":
        args = (
            (
                noun,
                "remove",
                selector,
                "--yes",
                "--request-key",
                request_key,
                "--json",
            )
            if model_receipt_only_reconnect
            else (
                noun,
                "remove",
                selector,
                "--yes",
                "--json",
            )
        )
    else:
        args = (
            noun,
            "remove",
            selector,
            "--keep-model",
            "--yes",
            "--json",
        )
    client = FakeClient({("POST", path): receipt})
    if noun == "model":
        client.responses[("GET", f"/api/model/{selector}")] = model_detail
        client.responses[("GET", f"/api/model/requests/{request_key}")] = receipt
        if not model_receipt_only_reconnect:
            client.responses[("GET", f"/api/model/{selector}/remove-review")] = (
                _removal_review("model", selector, model_digest=model_digest)
            )
    else:
        client.responses[("GET", f"/api/recipe/{selector}/remove-review")] = (
            _removal_review("recipe", selector, with_model=False)
        )
        client.responses[("GET", f"/api/recipe/requests/{request_key}")] = receipt

    status, payload = run(args, client)

    assert status == 2
    observation = payload["observation"]
    assert isinstance(observation, dict) and observation["status"] == "timed_out"
    assert payload["result"] == {}
    methods = [call[0] for call in client.calls]
    assert methods.count("POST") == 1
    if model_receipt_only_reconnect:
        # Damaged lookup bookkeeping replays the same keyed owner request.
        assert next(call[2] for call in client.calls if call[0] == "POST") == {
            "request_key": request_key
        }
    assert methods.count("GET") >= 4
    # A distinct request is admitted immediately after the bounded observer ends.
    repaired = receipt | {
        "action": "remove",
        "selector": selector,
        "request_key": "33333333-3333-4333-8333-333333333333",
    }
    if noun == "recipe":
        repaired["with_model"] = False
    client.responses[("POST", path)] = repaired
    client.responses[("GET", f"/api/{noun}/{selector}/remove-review")] = (
        _removal_review(noun, selector, model_digest=str(model_digest))
        if noun == "model"
        else _removal_review(noun, selector, with_model=False)
    )
    fresh_args = (
        noun,
        "remove",
        selector,
        *(("--keep-model",) if noun == "recipe" else ()),
        "--yes",
        "--detach",
        "--json",
    )
    client.responses[("GET", f"/api/{noun}/requests/{repaired['request_key']}")] = (
        ControlNotFound(404, "No accepted request")
    )
    status, result = run(
        (*fresh_args, "--request-key", str(repaired["request_key"])), client
    )
    assert status == 0 and result["operation_id"] == operation_id


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
