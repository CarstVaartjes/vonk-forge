"""Operator intent reaches its owner; projections only inform observation."""

from __future__ import annotations

import argparse
import json
from dataclasses import replace

import pytest
from test_controller_cli import FakeClient, run

from cluster_profiles import cli, controller_cli
from cluster_profiles.cli_states_generated import (
    HTTP_REFUSAL,
    OBSERVING,
    QUEUED,
    SUCCEEDED,
)
from cluster_profiles.control_client import (
    ControlForbidden,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlTransportError,
    validate_control_document,
)
from cluster_profiles.error_reporting import protocol_context

KEY = "11111111-1111-4111-8111-111111111111"
FRESH = "22222222-2222-4222-8222-222222222222"
NODE = "spk_" + "a" * 32


@pytest.fixture(autouse=True)
def bounded_clock(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(controller_cli.observation.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        controller_cli.observation.time,
        "sleep",
        lambda delay: clock.__setitem__(0, clock[0] + delay),
    )
    return clock


def test_damaged_definition_repairs_before_edit_and_owner_decides_revision(capsys):
    path = "/api/profile/1/definition"
    definition = {"name": "original", "assignments": []}
    readable = {"id": KEY, "number": 1, "revision": 3, "definition": definition}
    client = FakeClient(
        {
            ("GET", path): [readable | {"definition": None}, readable],
            ("PUT", "/api/profile/1"): {
                "revision": 4,
                "definition": definition | {"name": "edited"},
            },
        }
    )
    assert (
        cli.main(
            (
                "--profile",
                "1",
                "profile",
                "configure",
                "--name",
                "edited",
                "--expected-revision",
                "2",
                "--json",
            ),
            control_client=client,
        )
        == 0
    )
    assert [call[0] for call in client.calls] == ["GET", "GET", "PUT"]
    assert client.calls[-1][2] == validate_control_document(
        "FleetProfileInput",
        definition
        | {
            "name": "edited",
            "expected_revision": 2,
            "labels": {},
        },
    )
    capsys.readouterr()
    assert (
        cli.main(
            ("--profile", "1", "profile", "configure", "--name", "fresh", "--json"),
            control_client=client,
        )
        == 0
    )
    assert client.calls[-1][2] == validate_control_document(
        "FleetProfileInput",
        definition | {"name": "fresh", "expected_revision": 3, "labels": {}},
    )


def test_damaged_definition_ends_without_save_and_fresh_edit_is_admitted(capsys):
    path = "/api/profile/1/definition"
    client = FakeClient(
        {("GET", path): ControlMalformedResponse("projection unavailable")}
    )
    argv = ("--profile", "1", "profile", "configure", "--name", "edited", "--json")
    assert cli.main(argv, control_client=client) == 2
    assert len(client.calls) <= 31
    assert all(call[0] == "GET" for call in client.calls)
    capsys.readouterr()
    definition = {"name": "original", "assignments": []}
    client.responses[("GET", path)] = {
        "id": KEY,
        "number": 1,
        "revision": 3,
        "definition": definition,
    }
    client.responses[("PUT", "/api/profile/1")] = {
        "revision": 4,
        "definition": definition,
    }
    assert cli.main(argv, control_client=client) == 0
    assert client.calls[-1][0] == "PUT"


def test_exact_spark_id_does_not_scan_unrelated_damaged_roster():
    client = FakeClient(
        {("GET", "/api/fleet"): ControlMalformedResponse("unrelated node")}
    )
    assert controller_cli._resolve_spark_selectors(client, [NODE]) == [NODE]
    assert client.calls == []


def test_first_fleet_read_recovers_and_exhaustion_does_not_block_fresh_read():
    path = "/api/fleet"
    readable = {"nodes": []}
    client = FakeClient(
        {("GET", path): [ControlMalformedResponse("partial projection"), readable]}
    )
    assert run(("fleet", "--json"), client) == (0, readable)
    assert [call[0] for call in client.calls] == ["GET", "GET"]
    client.responses[("GET", path)] = ControlMalformedResponse("projection unavailable")
    before = len(client.calls)
    assert run(("fleet", "--json"), client)[0] == 2
    assert len(client.calls) - before <= 31
    client.responses[("GET", path)] = readable
    assert run(("fleet", "--json"), client) == (0, readable)


def test_delivery_io_repairs_with_bounded_attempts_then_fresh_io_succeeds(tmp_path):
    from cluster_profiles.cli_files import PrivateOutput

    attempts = []
    destination = tmp_path / "grant.json"

    def repairable():
        attempts.append(None)
        if len(attempts) < 3:
            raise OSError("temporary storage loss")
        return PrivateOutput(destination)

    with controller_cli.fleet._delivery_io(repairable) as output:
        output.write({"grant_id": KEY})
    assert json.loads(destination.read_text()) == {"grant_id": KEY}
    assert len(attempts) == 3

    def unavailable():
        attempts.append(None)
        raise OSError("storage unavailable")

    before = len(attempts)
    try:
        controller_cli.fleet._delivery_io(unavailable)
    except OSError:
        pass
    assert len(attempts) - before == 3
    fresh = tmp_path / "fresh.json"
    with controller_cli.fleet._delivery_io(lambda: PrivateOutput(fresh)) as output:
        output.write({"grant_id": FRESH})
    assert json.loads(fresh.read_text()) == {"grant_id": FRESH}
    assert json.loads(destination.read_text()) == {"grant_id": KEY}


def test_malformed_domain_http_evidence_reobserves_exact_key_then_fresh_request():
    post = "/api/model/chosen/download"
    lookup = f"/api/model/requests/{KEY}"
    accepted = {
        "action": "download",
        "selector": "chosen",
        "request_key": KEY,
        "operation_id": "original",
        "state": QUEUED,
    }
    client = FakeClient(
        {
            ("POST", post): ControlMalformedResponse(
                "unreadable domain reply",
                context=replace(
                    protocol_context(operation="download", endpoint=post),
                    http_status=400,
                ),
            ),
            ("GET", lookup): [
                ControlMalformedResponse("projection unavailable"),
                accepted,
            ],
        }
    )
    status, result = run(("model", "download", "chosen", "--detach", "--json"), client)
    assert status == 0 and result == accepted
    assert [call[0] for call in client.calls] == ["POST", "GET", "GET"]
    fresh = accepted | {"request_key": FRESH, "operation_id": "fresh"}
    client.responses[("POST", post)] = fresh
    assert run(
        ("model", "download", "chosen", "--request-key", FRESH, "--detach", "--json"),
        client,
    ) == (0, fresh)


@pytest.mark.parametrize("denied", [False, True])
def test_read_answer_reobserves_except_actual_authorization_denial(denied):
    post = "/api/model/chosen/download"
    lookup = f"/api/model/requests/{KEY}"
    accepted = {
        "action": "download",
        "selector": "chosen",
        "request_key": KEY,
        "operation_id": "original",
        "state": QUEUED,
    }
    answer = (
        ControlForbidden(403, "authority denied", failure_family=HTTP_REFUSAL)
        if denied
        else ControlHTTPError(400, "projection temporarily unreadable")
    )
    client = FakeClient(
        {
            ("POST", post): ControlTransportError(),
            ("GET", lookup): [answer, accepted],
        }
    )
    status, result = run(("model", "download", "chosen", "--detach", "--json"), client)
    assert status == (2 if denied else 0)
    assert [call[0] for call in client.calls] == (
        ["POST", "GET"] if denied else ["POST", "GET", "GET"]
    )
    if not denied:
        assert result == accepted
    fresh = accepted | {"request_key": FRESH, "operation_id": "fresh"}
    client.responses[("POST", post)] = fresh
    assert run(
        ("model", "download", "chosen", "--request-key", FRESH, "--detach", "--json"),
        client,
    ) == (0, fresh)


def test_run_does_not_load_an_unconfirmed_draft_and_fresh_save_is_admitted():
    definition = {"name": "original", "assignments": []}
    path = "/api/profile/1/definition"
    client = FakeClient(
        {
            ("GET", path): {
                "id": KEY,
                "number": 1,
                "revision": 3,
                "definition": definition,
            },
            ("PUT", "/api/profile/1"): ControlTransportError(),
        }
    )
    argv = (
        "--profile",
        "1",
        "run",
        "publisher/recipe",
        "--spark",
        NODE,
        "--yes",
        "--json",
    )
    status, _ = run(argv, client)
    assert status == 2
    assert not any(call[1].endswith("/load") for call in client.calls)
    assert [call[0] for call in client.calls].count("PUT") == 1
    client.responses[("PUT", "/api/profile/1")] = {
        "revision": 4,
        "definition": definition,
    }
    client.responses[("POST", "/api/profile/1/preview")] = {"effects_digest": "a" * 64}
    client.responses[("POST", "/api/profile/1/load")] = {
        "id": FRESH,
        "request_key": KEY,
        "state": SUCCEEDED,
    }
    client.responses[("GET", "/api/profile/1/endpoints")] = {
        "assignments": [],
        "number": 1,
        "observed_at": "2026-10-09T00:00:00Z",
        "application_id": FRESH,
        "application_state": SUCCEEDED,
    }
    assert run(argv, client)[0] == 0
    assert sum(call[1].endswith("/load") for call in client.calls) == 1


def test_cancellation_lookup_reobserves_old_metadata_without_local_conflict():
    operation = KEY
    path = f"/api/model/operations/{operation}"
    cancellation = {"request_key": KEY, "reason": "changed intent"}
    accepted = {
        "action": "download",
        "selector": "chosen",
        "request_key": FRESH,
        "operation_id": operation,
        "state": OBSERVING,
        "cancellation": cancellation,
    }
    client = FakeClient(
        {
            ("POST", path + "/cancel"): ControlTransportError(),
            ("GET", path): [
                accepted
                | {"cancellation": {"request_key": FRESH, "reason": "old intent"}},
                accepted,
            ],
        }
    )
    # The accepted owner cancellation can still be running; detach observes its receipt.
    argv = (
        "model",
        "cancel",
        operation,
        "--reason",
        "changed intent",
        "--request-key",
        KEY,
        "--yes",
        "--detach",
        "--json",
    )
    status, result = run(argv, client)
    assert status == 0 and result == accepted
    assert [call[0] for call in client.calls] == ["POST", "GET", "GET"]
    client.responses[("POST", path + "/cancel")] = accepted | {
        "cancellation": cancellation | {"request_key": FRESH}
    }
    fresh_args = tuple(
        FRESH if item == KEY and index > 3 else item for index, item in enumerate(argv)
    )
    assert run(fresh_args, client)[0] == 0


def test_missing_attempt_reobserves_before_writing_evidence(tmp_path, capsys):
    path = f"/api/operations/{KEY}"
    bundle = {"operation_id": KEY, "attempt": 2}

    class EvidenceClient(FakeClient):
        def _validate_request(self, method, target, payload, query):
            if target.startswith(path):
                return
            super()._validate_request(method, target, payload, query)

    client = EvidenceClient(
        {
            ("GET", path): [{"id": KEY}, {"id": KEY, "attempt": 2}],
            ("GET", path + "/evidence"): bundle,
        }
    )
    destination = tmp_path / "evidence.json"
    assert (
        cli.main(
            ("fleet", "evidence", KEY, "--output", str(destination), "--json"),
            control_client=client,
        )
        == 0
    )
    assert json.loads(destination.read_text()) == bundle
    assert [call[1] for call in client.calls] == [path, path, path + "/evidence"]
    capsys.readouterr()
    fresh = tmp_path / "fresh.json"
    assert (
        cli.main(
            ("fleet", "evidence", KEY, "--output", str(fresh), "--json"),
            control_client=client,
        )
        == 0
    )
    assert json.loads(fresh.read_text()) == bundle


def test_malformed_upgrade_replays_same_owner_request_without_new_effect():
    args = argparse.Namespace(
        all=True, selector=None, request_key=KEY, global_json=True, json=True
    )
    receipt = {
        "action": "upgrade",
        "request_key": KEY,
        "operation_id": FRESH,
        "plan_digest": "a" * 64,
        "targets": [],
        "state": SUCCEEDED,
    }
    client = FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): [
                ControlMalformedResponse("lost receipt"),
                receipt,
            ]
        }
    )
    result = controller_cli.profile_load._submit_fleet_upgrade(
        args, client, lambda: KEY
    )
    assert result == receipt
    assert len(client.calls) == 2
    assert client.calls[0][2] == client.calls[1][2]
    assert (
        controller_cli.profile_load._submit_fleet_upgrade(args, client, lambda: KEY)
        == receipt
    )


def test_unique_profile_successors_share_one_budget_then_fresh_follow_succeeds(
    bounded_clock,
):
    from uuid import UUID

    from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState

    class Successors(FakeClient):
        def __init__(self):
            super().__init__({})
            self.fresh = False
            self.timeouts = []

        def request(self, method, path, payload=None, **kwargs):
            self.calls.append((method, path, payload, None))
            self.timeouts.append(kwargs["timeout_seconds"])
            bounded_clock[0] += 1
            if self.fresh:
                return {"id": FRESH, "state": SUCCEEDED}
            index = len(self.calls)
            identity = str(UUID(int=index, version=4))
            return {
                "id": identity,
                "state": LifecycleState.SUPERSEDED.value,
                "retry_of_application_id": str(UUID(int=index - 1, version=4)),
                "superseded_by": str(UUID(int=index + 1, version=4)),
            }

    client = Successors()
    argv = ("profile", "progress", "--follow", "--timeout-seconds", "4", "--json")
    status, _result = run(argv, client)
    assert status == 2
    assert bounded_clock[0] == 4
    assert client.timeouts == [4, 3, 2, 1]
    assert all(call[0] == "GET" for call in client.calls)
    client.fresh = True
    assert run(argv, client) == (0, {"id": FRESH, "state": SUCCEEDED})


@pytest.mark.parametrize("server_error", [False, True])
def test_lost_profile_save_observes_without_overwriting_then_fresh_edit_works(
    capsys, server_error
):
    original = {"name": "original", "assignments": []}
    changed = original | {"name": "changed"}
    view = {"id": KEY, "number": 1, "revision": 3, "definition": original}
    client = FakeClient(
        {
            ("GET", "/api/profile/1/definition"): [
                view,
                view | {"revision": 4, "definition": changed},
            ],
            ("PUT", "/api/profile/1"): (
                ControlHTTPError(500, "save outcome unavailable")
                if server_error
                else ControlTransportError()
            ),
        }
    )
    assert (
        cli.main(
            ("--profile", "1", "profile", "configure", "--name", "changed", "--json"),
            control_client=client,
        )
        == 2
    )
    assert sum(method == "PUT" for method, *_ in client.calls) == 1
    assert client.calls[-1][0] == "GET"
    capsys.readouterr()
    client.responses[("PUT", "/api/profile/1")] = {
        "revision": 5,
        "definition": changed | {"name": "fresh"},
    }
    assert (
        cli.main(
            ("--profile", "1", "profile", "configure", "--name", "fresh", "--json"),
            control_client=client,
        )
        == 0
    )
    assert client.calls[-1][2] == validate_control_document(
        "FleetProfileInput",
        changed | {"name": "fresh", "expected_revision": 4, "labels": {}},
    )
