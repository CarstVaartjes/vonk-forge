"""Controller CLI: profile load follow."""

from __future__ import annotations

import pytest
from controller_cli_support import FakeClient, _accepted_profile_cancellation, run
from library_route_fixtures import _recipe_projection

from cluster_profiles import cli
from cluster_profiles.cli_render.progress import progress_line
from cluster_profiles.control_client.errors import (
    ControlForbidden,
    ControlHTTPError,
    ControlNotFound,
    ControlTransportError,
    ControlUnavailable,
)


def _accepted_profile_load(identity: str, state: str) -> dict[str, object]:
    return {
        "id": identity,
        "state": state,
        "request_key": "11111111-1111-4111-8111-111111111111",
        "progress": {"intended_profile": {"reviewed_plan_digest": "c" * 64}},
    }


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
        assert isinstance(observation, dict)
        assert observation["status"] == "timed_out"
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
    assert status == 0
    assert result["id"] == application_id


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

    assert status == 0
    assert payload["state"] == "succeeded"
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
    assert isinstance(observation, dict)
    assert observation["status"] == "timed_out"
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
    assert status == 0
    assert result["request_key"] == fresh_key


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
    assert status == 0
    assert payload["state"] == "succeeded"
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

    assert status == 0
    assert payload["id"] == application_id
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

    assert status == 0
    assert payload["id"] == application_id
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

    assert status == 0
    assert payload["id"] == application_id


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

    assert status == 0
    assert payload["id"] == selected_application
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

    assert status == 0
    assert payload["id"] == requested_application
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

    assert status == 0
    assert payload["state"] == "succeeded"
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
    assert payload["id"] == second
    assert payload["state"] == "succeeded"
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
    assert status == 0
    assert result["request_key"] == fresh_key


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


def test_accepted_load_recovers_after_missing_observation() -> None:
    key = "11111111-1111-4111-8111-111111111111"
    operation = "33333333-3333-4333-8333-333333333333"
    client = FakeClient(
        {
            ("POST", "/api/profile/1/load"): _accepted_profile_load(
                operation, "queued"
            ),
            (
                "GET",
                f"/api/profile/applications/{operation}",
            ): [
                ControlUnavailable(503, "application observation is unavailable"),
                {"id": operation, "state": "succeeded"},
            ],
        }
    )

    status, payload = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--yes",
            "--request-key",
            key,
            "--timeout-seconds",
            "1",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert payload["id"] == operation
    assert payload["state"] == "succeeded"
    assert [call[:2] for call in client.calls] == [
        ("POST", "/api/profile/1/load"),
        ("GET", f"/api/profile/applications/{operation}"),
        ("GET", f"/api/profile/applications/{operation}"),
    ]


def test_lost_load_response_retries_only_with_original_request_key() -> None:
    key = "11111111-1111-4111-8111-111111111111"
    operation = "33333333-3333-4333-8333-333333333333"
    client = FakeClient(
        {
            ("POST", "/api/profile/1/load"): [
                ControlTransportError("connection reset"),
                _accepted_profile_load(operation, "succeeded"),
            ],
            ("GET", f"/api/profile/1/requests/{key}"): ControlNotFound(
                404, "not committed"
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
    post_keys = [
        call[2]["request_key"]
        for call in client.calls
        if call[0] == "POST" and call[2] is not None
    ]
    assert post_keys == [key, key]


def test_progress_does_not_invent_percentage_for_unknown_total() -> None:
    line = progress_line(
        {
            "state": "running",
            "progress": {"phase": "building", "completed_items": 4, "total_items": 7},
        }
    )
    assert "items 4 / 7" in line
    assert "%" not in line
    unknown_total = progress_line(
        {
            "state": "running",
            "progress": {
                "phase": "copying",
                "completed_bytes": 20,
                "total_bytes_known": False,
            },
        }
    )
    assert "20 B" in unknown_total
    assert "total unknown" in unknown_total


def test_removal_flags_are_scoped_to_recipe_model_dependency() -> None:
    parser = cli._parser()
    for argv in (
        ("fleet", "remove", "Atlas", "--with-model"),
        ("model", "remove", "qwen", "--with-model"),
    ):
        try:
            parser.parse_args(argv)
        except cli._UsageError:
            pass
        else:
            raise AssertionError(f"invalid dependency flag accepted: {argv}")
    assert (
        parser.parse_args(("recipe", "remove", "qwen-code", "--keep-model")).keep_model
        is True
    )
    for argv in (
        ("model", "download", "qwen", "--force"),
        ("recipe", "download", "qwen-code", "--force"),
    ):
        try:
            parser.parse_args(argv)
        except cli._UsageError:
            pass
        else:
            raise AssertionError(f"redundant refresh flag accepted: {argv}")


def test_explicit_request_key_is_forwarded_for_retry_reconciliation() -> None:
    client = FakeClient(
        {
            ("POST", "/api/model/qwen/download"): {
                "state": "running",
                "operation_id": "download",
                "request_key": "22222222-2222-4222-8222-222222222222",
                "action": "download",
                "selector": "qwen",
            }
        }
    )
    status, _ = run(
        (
            "model",
            "download",
            "qwen",
            "--request-key",
            "22222222-2222-4222-8222-222222222222",
            "--detach",
            "--json",
        ),
        client,
    )
    assert status == 0
    download_request = client.calls[0][2]
    assert download_request is not None
    assert download_request["request_key"] == "22222222-2222-4222-8222-222222222222"


def test_profile_revision_conflict_is_reported_without_a_second_write() -> None:
    class ConflictClient(FakeClient):
        def request(
            self,
            method,
            path,
            payload=None,
            *,
            extra_headers=None,
            query=None,
            timeout_seconds=None,
            retry=True,
        ):
            self._validate_request(method, path, payload, query)
            self.calls.append((method, path, payload, query))
            if method == "PUT":
                raise ValueError("profile revision conflict")
            if path == "/api/recipe/library":
                return {
                    "recipes": [
                        _recipe_projection("vonk-forge/qwen-code", "Qwen Code")
                    ],
                    "next_cursor": None,
                }
            if path == "/api/fleet":
                return {
                    "nodes": [
                        {
                            "id": "spk_" + "a" * 32,
                            "display_name": "Atlas",
                            "hostname": "atlas",
                        }
                    ]
                }
            return {
                "id": "11111111-1111-4111-8111-111111111111",
                "number": 1,
                "revision": 4,
                "definition": {"assignments": []},
            }

    client = ConflictClient({})
    status, _payload = run(
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
    assert [call[0] for call in client.calls] == ["GET", "GET", "PUT"]


def test_ambiguous_mutation_error_is_not_retried_or_fuzzily_resolved() -> None:
    class AmbiguousClient(FakeClient):
        def request(
            self,
            method,
            path,
            payload=None,
            *,
            extra_headers=None,
            query=None,
            timeout_seconds=None,
            retry=True,
        ):
            self._validate_request(method, path, payload, query)
            self.calls.append((method, path, payload, query))
            raise ControlHTTPError(
                422, "ambiguous model selector; choose an exact selector"
            )

    client = AmbiguousClient({})
    status, payload = run(("model", "download", "qwen", "--json"), client)
    assert status == 2
    ambiguous_model_error = payload["error"]
    assert isinstance(ambiguous_model_error, str)
    assert "ambiguous model selector" in ambiguous_model_error
    assert len(client.calls) == 1


def test_profile_selection_and_progress_use_numbered_current_routes() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile"): {"profiles": [{"number": 1, "name": "Default"}]},
            ("GET", "/api/profile/3"): {
                "number": 3,
                "state": "running",
                "operation_id": "load-3",
            },
            ("GET", "/api/profile/3/progress"): {
                "id": "33333333-3333-4333-8333-333333333333",
                "profile_id": "11111111-1111-4111-8111-111111111111",
                "state": "succeeded",
            },
        }
    )
    assert run(("--profile", "3", "profile", "list", "--json"), client)[0] == 0
    assert run(("--profile", "3", "profile", "progress", "--json"), client)[0] == 0
    assert [call[1] for call in client.calls] == [
        "/api/profile",
        "/api/profile/3/progress",
    ]


def test_profile_endpoint_uses_scoped_current_controller_projection() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile/3/endpoints"): {
                "number": 3,
                "profile_id": "11111111-1111-4111-8111-111111111111",
                "application_id": "22222222-2222-4222-8222-222222222222",
                "application_state": "succeeded",
                "observed_at": "2026-09-23T12:59:31Z",
                "assignments": [
                    {
                        "assignment_id": "33333333-3333-4333-8333-333333333333",
                        "recipe_title": "Example Model",
                        "desired_state": "running",
                        "alias": "example-model",
                        "state": "published",
                        "endpoint": {
                            "alias": "example-model",
                            "api_base": "https://vonk-forge.example.ts.net/v1",
                            "backend_api_base": "http://10.0.0.10:8000/v1",
                            "generation": 8,
                            "node_id": "spk_" + "a" * 32,
                            "observed_at": "2026-09-23T12:59:30Z",
                            "plan_digest": "a" * 64,
                        },
                    },
                    {
                        "assignment_id": "44444444-4444-4444-8444-444444444444",
                        "recipe_title": "Installed Tool",
                        "desired_state": "installed",
                        "alias": None,
                        "state": "installed-only",
                        "endpoint": None,
                    },
                ],
            }
        }
    )

    assert (
        cli.main(("--profile", "3", "profile", "endpoint"), control_client=client) == 0
    )
    assert client.calls == [("GET", "/api/profile/3/endpoints", None, None)]


def test_profile_endpoint_without_a_profile_answers_for_the_loaded_one() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile"): {
                "profiles": [
                    {"number": 1, "name": "Default", "loaded_revision": None},
                    {"number": 42, "name": "Video", "loaded_revision": 3},
                ]
            },
            ("GET", "/api/profile/42/endpoints"): {
                "number": 42,
                "observed_at": "2026-09-23T12:59:31Z",
                "assignments": [],
            },
        }
    )

    status, payload = run(("profile", "endpoint", "--json"), client)

    assert status == 0
    assert payload["number"] == 42
    assert client.calls[-1][1] == "/api/profile/42/endpoints"


def test_profile_endpoint_survives_one_transient_failure_listing_profiles() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile"): [
                ControlHTTPError(503, "unavailable"),
                {"profiles": [{"number": 42, "name": "Video", "loaded_revision": 3}]},
            ],
            ("GET", "/api/profile/42/endpoints"): {
                "number": 42,
                "observed_at": "2026-09-23T12:59:31Z",
                "assignments": [],
            },
        }
    )

    status, payload = run(("profile", "endpoint", "--json"), client)

    assert status == 0
    assert payload["number"] == 42


def test_profile_endpoint_alias_uses_controller_membership_query_and_json() -> None:
    client = FakeClient(
        {
            ("GET", "/api/profile/3/endpoints"): {
                "number": 3,
                "profile_id": "11111111-1111-4111-8111-111111111111",
                "application_id": "22222222-2222-4222-8222-222222222222",
                "application_state": "succeeded",
                "observed_at": "2026-09-23T12:59:31Z",
                "assignments": [],
            }
        }
    )

    status, payload = run(
        ("--profile", "3", "profile", "endpoint", "missing", "--json"), client
    )

    assert status == 0
    assert payload["assignments"] == []
    assert client.calls == [
        ("GET", "/api/profile/3/endpoints", None, {"alias": "missing"})
    ]
