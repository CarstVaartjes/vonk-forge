"""Controller CLI: recipe reconcile cache."""

from __future__ import annotations

import argparse
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import pytest
from controller_cli_support import FakeClient, _model_detail, _removal_review, run

from cluster_profiles import cli
from cluster_profiles.cli_render.dispatch import render_payload
from cluster_profiles.control_client import schema as schema_module
from cluster_profiles.control_client.errors import (
    ControlConflict,
    ControlHTTPError,
    ControlNotFound,
    ControlTransportError,
    ControlUnavailable,
)
from cluster_profiles.controller_cli import installation as installation_module
from cluster_profiles.controller_cli import observation as observation_module
from cluster_profiles.generated_control.models.job_operator_receipt import (
    JobOperatorReceipt,
)
from cluster_profiles.generated_control.models.job_operator_receipt_action import (
    check_job_operator_receipt_action,
)
from control.tests.consumer_outcomes import not_adopted
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, OperatorActionName

_REVIEW_DIGEST = "b" * 64


def _subparser_choices(
    parser: argparse.ArgumentParser,
) -> dict[str, argparse.ArgumentParser]:
    """Return the parser's subcommands through the argparse group it created."""

    group = parser._subparsers
    assert group is not None, "parser has no subcommands"
    action = group._group_actions[0]
    assert isinstance(action, argparse._SubParsersAction), (
        "parser subcommands are unavailable"
    )
    return action.choices


def _reconciliation_plan(
    installation_id: str = "22222222-2222-4222-8222-222222222222",
    digest: str = _REVIEW_DIGEST,
    *,
    allowed: bool = True,
) -> dict[str, object]:
    return {
        "action": "cleanup",
        "allowed": allowed,
        "blockers": [],
        "cleanup_mode": "reconcile",
        "installation_id": installation_id,
        "plan_digest": digest,
    }


def _reconciliation_operation(
    request_key: str,
    installation_id: str = "22222222-2222-4222-8222-222222222222",
    digest: str = _REVIEW_DIGEST,
) -> dict[str, object]:
    node_id = "spk_" + "2" * 32
    return {
        "operation_id": "33333333-3333-4333-8333-333333333333",
        "kind": "recipe.cleanup.v2",
        "action": "cleanup",
        "state": "queued",
        "plan_digest": digest,
        "request_key": request_key,
        "cleanup_mode": "reconcile",
        "installation_id": installation_id,
        "node_ids": [node_id],
        "current_phase": None,
        "completed_phases": [],
        "progress": {
            "operation": None,
            "startup_budget_seconds": None,
            "start_deadline": None,
            "phase_index": 0,
            "phase_count": 2,
            "phase": None,
            "state": "queued",
            "completed_bytes": 0,
            "total_bytes": None,
            "total_bytes_known": False,
            "subphase": None,
            "members": [
                {
                    "node_id": node_id,
                    "phase": None,
                    "state": "pending",
                    "completed_bytes": 0,
                    "total_bytes": None,
                    "error": None,
                }
            ],
        },
        "status_reason": None,
        "result": None,
    }


def _operation_owner_page(request_key: str) -> dict[str, object]:
    operation_id = "33333333-3333-4333-8333-333333333333"
    node_id = "spk_" + "2" * 32
    owner = {"kind": "job", "id": operation_id, "request_id": request_key}
    return {
        "operations": [
            {
                "id": operation_id,
                "node_ids": [node_id],
                "kind": "recipe.cleanup.v2",
                "state": "queued",
                "summary": "Recipe cleanup queued",
                "attempt": 1,
                "created_at": "2026-09-25T09:00:00Z",
                "owner": owner,
            },
            {
                "id": "44444444-4444-4444-8444-444444444444",
                "parent_id": operation_id,
                "node_ids": [node_id],
                "kind": "recipe.reconcile",
                "state": "running",
                "summary": "Recipe check running",
                "attempt": 1,
                "created_at": "2026-09-25T09:00:01Z",
                "owner": owner,
            },
        ],
        "next_cursor": None,
        "total": 2,
    }


def _allow_minimal_reconciliation_plan(monkeypatch) -> None:
    original = schema_module.validate_control_document

    def validate(contract: str, value: object) -> dict[str, object]:
        if contract == "RunSwitchPlan":
            assert isinstance(value, dict)
            return value
        return original(contract, value)

    monkeypatch.setattr(installation_module, "validate_control_document", validate)


def test_recipe_installation_reconcile_review_uses_typed_preview_route(
    monkeypatch,
) -> None:
    installation_id = "22222222-2222-4222-8222-222222222222"
    path = f"/api/recipe/installations/{installation_id}/reconcile/preview"
    plan = _reconciliation_plan(installation_id)
    client = FakeClient({("POST", path): plan})

    _allow_minimal_reconciliation_plan(monkeypatch)
    status, result = run(
        (
            "recipe",
            "installation",
            "reconcile",
            installation_id,
            "--review",
            "--json",
        ),
        client,
    )

    assert status == 0 and result == plan
    assert client.calls == [("POST", path, None, None)]


def test_recipe_installation_reconcile_submits_without_a_preview() -> None:
    installation_id = "22222222-2222-4222-8222-222222222222"
    key = "11111111-1111-4111-8111-111111111111"
    apply = f"/api/recipe/installations/{installation_id}/reconcile"
    empty = {"operations": [], "next_cursor": None, "total": 0}
    client = FakeClient(
        {
            ("GET", "/api/operations"): empty,
            ("POST", apply): ControlConflict(409, "installation is busy"),
        }
    )

    status, _result = run(
        (
            "recipe",
            "installation",
            "reconcile",
            installation_id,
            "--request-key",
            key,
            "--yes",
            "--json",
        ),
        client,
    )

    assert status == 2
    assert isinstance(_result["observation"], dict)
    assert sum(call[0] == "POST" for call in client.calls) == 1
    assert all(call[0] == "GET" for call in client.calls[2:])
    assert client.calls[1][2] == {"request_key": key}

    client.responses[("POST", apply)] = _reconciliation_operation(key, installation_id)
    assert (
        run(
            (
                "recipe",
                "installation",
                "reconcile",
                installation_id,
                "--request-key",
                key,
                "--yes",
                "--detach",
                "--json",
            ),
            client,
        )[0]
        == 0
    )


def test_recipe_installation_reconcile_reconnects_parent_from_realistic_activity_rows(
    monkeypatch,
) -> None:
    installation_id = "22222222-2222-4222-8222-222222222222"
    key = "11111111-1111-4111-8111-111111111111"
    operation_id = "33333333-3333-4333-8333-333333333333"
    operation_path = f"/api/run-switch/operations/{operation_id}"
    operation = _reconciliation_operation(key, installation_id)
    client = FakeClient(
        {
            ("GET", "/api/operations"): _operation_owner_page(key),
            ("GET", operation_path): operation,
        }
    )

    status, result = run(
        (
            "recipe",
            "installation",
            "reconcile",
            installation_id,
            "--request-key",
            key,
            "--yes",
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0 and result["operation_id"] == operation_id
    assert [(call[0], call[1]) for call in client.calls] == [
        ("GET", "/api/operations"),
        ("GET", operation_path),
    ]
    assert client.calls[0][3] == {"request_id": key, "limit": 100}
    assert all(call[0] != "POST" for call in client.calls)


def test_recipe_installation_reconcile_bounds_request_lookup_under_submission_deadline(
    monkeypatch,
) -> None:
    installation_id = "22222222-2222-4222-8222-222222222222"
    key = "11111111-1111-4111-8111-111111111111"
    operation_id = "33333333-3333-4333-8333-333333333333"

    clock = [100.0]

    class BoundedClient(FakeClient):
        def __init__(self):
            super().__init__(
                {
                    ("GET", "/api/operations"): _operation_owner_page(key),
                    (
                        "GET",
                        f"/api/run-switch/operations/{operation_id}",
                    ): _reconciliation_operation(key, installation_id),
                }
            )
            self.timeouts: list[float | None] = []

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
            self.timeouts.append(timeout_seconds)
            if len(self.timeouts) == 1:
                clock[0] += 35
            return super().request(
                method,
                path,
                payload,
                extra_headers=extra_headers,
                query=query,
                timeout_seconds=timeout_seconds,
            )

    monkeypatch.setattr(observation_module.time, "monotonic", lambda: clock[0])
    client = BoundedClient()

    status, result = run(
        (
            "recipe",
            "installation",
            "reconcile",
            installation_id,
            "--request-key",
            key,
            "--yes",
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0 and result["operation_id"] == operation_id
    assert client.timeouts == [15.0, 10.0]


def test_recipe_installation_reconcile_owner_accepts_after_bounded_unknown_lookup() -> (
    None
):
    installation_id = "22222222-2222-4222-8222-222222222222"
    key = "11111111-1111-4111-8111-111111111111"
    client = FakeClient(
        {
            ("GET", "/api/operations"): ControlUnavailable(503, "temporary outage"),
            (
                "POST",
                f"/api/recipe/installations/{installation_id}/reconcile",
            ): _reconciliation_operation(key, installation_id),
        }
    )

    status, _result = run(
        (
            "recipe",
            "installation",
            "reconcile",
            installation_id,
            "--request-key",
            key,
            "--yes",
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert [call[0] for call in client.calls] == ["GET", "GET", "GET", "POST"]
    client.responses[("GET", "/api/operations")] = {
        "operations": [],
        "next_cursor": None,
        "total": 0,
    }
    client.responses[
        ("POST", f"/api/recipe/installations/{installation_id}/reconcile")
    ] = _reconciliation_operation(key, installation_id)
    assert (
        run(
            (
                "recipe",
                "installation",
                "reconcile",
                installation_id,
                "--request-key",
                key,
                "--yes",
                "--detach",
                "--json",
            ),
            client,
        )[0]
        == 0
    )


def test_recipe_installation_reconcile_replays_lost_acceptance_with_same_identity() -> (
    None
):
    installation_id = "22222222-2222-4222-8222-222222222222"
    key = "11111111-1111-4111-8111-111111111111"
    apply = f"/api/recipe/installations/{installation_id}/reconcile"
    empty = {"operations": [], "next_cursor": None, "total": 0}
    operation = _reconciliation_operation(key, installation_id)
    client = FakeClient(
        {
            ("GET", "/api/operations"): [empty, empty],
            ("POST", apply): [
                ControlTransportError("acceptance response was lost"),
                operation,
            ],
        }
    )

    status, result = run(
        (
            "recipe",
            "installation",
            "reconcile",
            installation_id,
            "--request-key",
            key,
            "--yes",
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 0 and result["operation_id"] == operation["operation_id"]
    posts = [call[2] for call in client.calls if call[0] == "POST" and call[1] == apply]
    assert len(posts) == 2 and posts[0] == posts[1]
    assert posts[0] == {"request_key": key}


def test_parser_exposes_current_singular_operator_roots_and_update() -> None:
    parser = cli._parser()
    assert set(_subparser_choices(parser)) == {
        "fleet",
        "job",
        "model",
        "recipe",
        "profile",
        "platform",
        "metrics",
        "key",
        "run",
        "update",
        "auth",
        "completion",
    }
    for argv in (
        ("models", "list"),
        ("recipes", "list"),
        ("profiles", "list"),
        ("admin", "fleet"),
        ("fleet", "profile"),
    ):
        try:
            parser.parse_args(argv)
        except cli._UsageError:
            pass
        else:
            raise AssertionError(f"retired command accepted: {argv}")


def test_fleet_activity_passes_all_filters_to_one_canonical_page() -> None:
    request_id = "33333333-3333-4333-8333-333333333333"
    target = "spk_" + "1" * 32
    cursor = "opaque-continuation"
    response = {"operations": [], "next_cursor": None, "total": 0}
    client = FakeClient({("GET", "/api/operations"): response})

    status, payload = run(
        (
            "fleet",
            "activity",
            "--limit",
            "7",
            "--cursor",
            cursor,
            "--state",
            "backoff",
            "--target",
            target,
            "--request-id",
            request_id,
            "--json",
        ),
        client,
    )

    assert status == 0 and payload == response
    assert client.calls == [
        (
            "GET",
            "/api/operations",
            None,
            {
                "cursor": cursor,
                "limit": 7,
                "state": "backoff",
                "node_id": target,
                "request_id": request_id,
            },
        )
    ]


def test_fleet_locks_reads_the_admission_lock_report() -> None:
    response = {
        "held": [
            {
                "node_id": "spk_" + "1" * 32,
                "namespace": "node",
                "holder": "run-admission",
                "state": "idle in transaction",
                "transaction_age_seconds": 12.5,
                "query": "SELECT 1",
            }
        ],
        "open_transactions": [],
    }
    client = FakeClient({("GET", "/api/fleet/locks"): response})

    status, payload = run(("fleet", "locks", "--json"), client)

    assert status == 0 and payload == response
    assert client.calls == [("GET", "/api/fleet/locks", None, None)]


def test_fleet_evidence_downloads_the_current_attempt_to_a_private_file(
    tmp_path: Path,
) -> None:
    bundle = {"schema": "evidence"}
    client = FakeClient(
        {
            ("GET", "/api/operations/op-1"): {"attempt": 2},
            ("GET", "/api/operations/op-1/evidence"): bundle,
        }
    )
    output = tmp_path / "evidence.json"

    status, payload = run(
        ("fleet", "evidence", "op-1", "--output", str(output), "--json"), client
    )

    assert status == 0 and payload["attempt"] == 2
    assert json.loads(output.read_text()) == bundle
    assert output.stat().st_mode & 0o077 == 0
    assert client.calls[-1][3] == {"attempt": 2}


def test_fleet_evidence_falls_back_to_the_job_attempt_for_an_unprojected_job(
    tmp_path: Path,
) -> None:
    """A job Activity does not project still has its evidence downloaded.

    Wrong implementation caught: the default attempt came only from the
    operation projection, so a recipe image build or run/switch parent answered
    "operation not found" and its stored failure evidence was unreachable.
    """
    bundle = {"schema": "evidence"}
    job_id = "11111111-1111-4111-8111-111111111111"
    client = FakeClient(
        {
            ("GET", f"/api/operations/{job_id}"): ControlNotFound(
                404, "operation not found"
            ),
            ("GET", f"/api/jobs/{job_id}"): {"current_attempt": 2},
            ("GET", f"/api/operations/{job_id}/evidence"): bundle,
        }
    )
    output = tmp_path / "evidence.json"

    status, payload = run(
        ("fleet", "evidence", job_id, "--output", str(output), "--json"), client
    )

    assert status == 0 and payload["attempt"] == 2
    assert json.loads(output.read_text()) == bundle


def test_recipe_sync_status_treats_a_missing_sync_as_never_run() -> None:
    class NeverSynced(FakeClient):
        def request(self, method, path, *args, **kwargs):
            raise ControlHTTPError(404, "not found")

    status, payload = run(("recipe", "sync-status", "--json"), NeverSynced({}))

    assert status == 0 and payload == {"state": "never-run"}


def test_fleet_resume_forwards_explicit_intent_without_projection_gate() -> None:
    job_id = "11111111-1111-4111-8111-111111111111"
    detail = {
        "id": job_id,
        "state": "needs-operator",
        "recovery": {"actions": ["inspect", "resume"]},
    }
    accepted = {"id": job_id, "state": "queued"}
    client = FakeClient(
        {
            ("GET", f"/api/jobs/{job_id}"): detail,
            ("POST", f"/api/jobs/{job_id}/resume"): accepted,
        }
    )

    status, payload = run(("fleet", "resume", job_id, "--yes", "--json"), client)

    assert status == 0 and payload == accepted
    assert client.calls == [
        ("POST", f"/api/jobs/{job_id}/resume", {"disposition": "resume"}, None),
    ]


def test_fleet_resume_asks_in_a_terminal_and_refuses_without_input(monkeypatch) -> None:
    class Terminal(StringIO):
        def isatty(self) -> bool:
            return True

    job_id = "11111111-1111-4111-8111-111111111111"
    monkeypatch.setattr(cli.sys, "stderr", Terminal())
    monkeypatch.setattr(cli.sys, "stdin", Terminal("n\n"))
    client = FakeClient({})
    assert cli.main(("fleet", "resume", job_id), control_client=client) != 0
    assert client.calls == []

    monkeypatch.setattr(cli.sys, "stdin", Terminal("y\n"))
    client = FakeClient(
        {("POST", f"/api/jobs/{job_id}/resume"): {"id": job_id, "state": "queued"}}
    )
    assert cli.main(("fleet", "resume", job_id), control_client=client) == 0
    assert [call[:2] for call in client.calls[:1]] == [
        ("POST", f"/api/jobs/{job_id}/resume")
    ]

    status, _ = run(("--no-input", "fleet", "resume", job_id, "--json"), client)
    assert status == 2 and len(client.calls) == 1


def test_a_confirmed_fleet_resume_posts_the_resume_and_exits_zero() -> None:
    job_id = "11111111-1111-4111-8111-111111111111"
    client = FakeClient(
        {
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": "needs-operator",
                "recovery": {"actions": ["inspect", "resume"]},
            },
            ("POST", f"/api/jobs/{job_id}/resume"): {"id": job_id, "state": "queued"},
        }
    )

    status = cli.main(("fleet", "resume", job_id, "--yes"), control_client=client)

    assert status == 0
    assert [(call[0], call[1]) for call in client.calls][-1] == (
        "POST",
        f"/api/jobs/{job_id}/resume",
    )


@pytest.mark.parametrize("receipt_matches", [True, False])
def test_fleet_resume_rejects_a_receipt_for_another_job(receipt_matches: bool) -> None:
    job_id = "11111111-1111-4111-8111-111111111111"
    other_job_id = "22222222-2222-4222-8222-222222222222"
    lookup = f"/api/jobs/{job_id}/requests/{job_id}"
    client = FakeClient(
        {
            ("GET", lookup): JobOperatorReceipt(
                action=check_job_operator_receipt_action(OperatorActionName.RESUME),
                actor="operator",
                content_digest="a" * 64,
                job_id=job_id if receipt_matches else other_job_id,
                request_id=job_id,
            ).to_dict(),
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": LifecycleState.RUNNING.value,
                "recovery": {"actions": ["resume"]},
            },
            ("POST", f"/api/jobs/{job_id}/resume"): {
                "id": other_job_id,
                "state": "queued",
            },
        }
    )

    status, payload = run(("fleet", "resume", job_id, "--yes", "--json"), client)

    if receipt_matches:
        assert status == 0, payload
        assert payload["id"] == job_id
        assert other_job_id not in json.dumps(payload)
        assert [(method, path) for method, path, *_ in client.calls] == [
            ("POST", f"/api/jobs/{job_id}/resume"),
            ("GET", lookup),
            ("GET", f"/api/jobs/{job_id}"),
        ]
    else:
        assert status != 0
        assert not any(path == f"/api/jobs/{job_id}" for _, path, *_ in client.calls)
    client.responses[("POST", f"/api/jobs/{job_id}/resume")] = {
        "id": job_id,
        "state": "queued",
    }
    assert run(("fleet", "resume", job_id, "--yes", "--json"), client)[0] == 0


def test_fleet_resume_submits_even_after_advertised_action_disappears() -> None:
    job_id = "11111111-1111-4111-8111-111111111111"
    client = FakeClient(
        {
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": "needs-operator",
                "recovery": {"actions": ["inspect"]},
            },
            ("POST", f"/api/jobs/{job_id}/resume"): {"id": job_id, "state": "queued"},
        }
    )
    status, payload = run(("fleet", "resume", job_id, "--yes", "--json"), client)
    assert status == 0 and payload["id"] == job_id
    assert [call[0] for call in client.calls] == ["POST"]
    assert run(("fleet", "resume", job_id, "--yes", "--json"), client)[0] == 0


def test_activity_presentation_keeps_exact_reconnect_and_complete_continuation() -> (
    None
):
    request_id = "33333333-3333-4333-8333-333333333333"
    target = "spk_" + "1" * 32
    operation = {
        "id": "operation-1",
        "kind": "recipe.stop",
        "state": "needs-operator",
        "attempt": 3,
        "created_at": "2026-08-05T12:00:00Z",
        "node_ids": [target],
        "owner": {
            "kind": "job",
            "id": "job-owner-1",
            "request_id": request_id,
        },
        "recovery": {"actions": ["inspect"]},
    }
    output = StringIO()
    with redirect_stdout(output):
        render_payload(
            {
                "operations": [operation],
                "total": 3,
                "next_cursor": "opaque+token",
            },
            "fleet",
            action="activity",
            activity_filters={
                "limit": 1,
                "state": "needs-operator",
                "target": target,
                "request_id": request_id,
            },
        )

    rendered = output.getvalue()
    assert "vonkctl fleet progress job-owner-1 --follow" in rendered
    assert (
        "vonkctl fleet activity --limit 1 --cursor opaque+token --state "
        f"needs-operator --target {target} --request-id {request_id}"
    ) in rendered


def test_fleet_loginfo_line_count_is_bounded_without_enumerating_choices() -> None:
    parser = cli._parser()
    fleet = _subparser_choices(parser)["fleet"]
    loginfo = _subparser_choices(fleet)["loginfo"]

    for accepted in ("1", "1000"):
        assert loginfo.parse_args(["Atlas", "--lines", accepted]).lines == int(accepted)
    for rejected in ("0", "1001", "not-a-number"):
        with not_adopted():
            loginfo.parse_args(["Atlas", "--lines", rejected])
        status = cli.main(("fleet", "loginfo", "Atlas", "--lines", rejected, "--json"))
        assert status == 2

    help_text = loginfo.format_help()
    assert "{1,2," not in help_text


def test_model_and_recipe_library_use_final_singular_routes_and_facets() -> None:
    model = FakeClient({("GET", "/api/model/library"): {"models": []}})
    status, payload = run(
        ("model", "library", "--usage", "code", "--family", "Qwen", "--json"),
        model,
    )
    assert status == 0 and payload == {"models": []}
    assert model.calls == [
        (
            "GET",
            "/api/model/library",
            None,
            {"usage": ["code"], "family": ["Qwen"], "sort": "updated", "limit": 100},
        )
    ]

    recipe = FakeClient({("GET", "/api/recipe/library"): {"recipes": []}})
    status, _ = run(
        ("recipe", "library", "--cached", "--model", "vision", "--json"),
        recipe,
    )
    assert status == 0
    assert recipe.calls[0][1] == "/api/recipe/library"
    assert recipe.calls[0][3] == {
        "sort": "updated",
        "limit": 100,
        "model": ["vision"],
        "cached": True,
    }


def test_download_is_one_step_and_repeated_calls_keep_server_operation_states() -> None:
    client = FakeClient(
        {
            ("POST", "/api/model/qwen/download"): {
                "state": "accepted",
                "action": "download",
                "operation_id": "model-download",
                "request_key": "11111111-1111-4111-8111-111111111111",
                "selector": "qwen",
            },
            ("POST", "/api/recipe/qwen-code/download"): {
                "state": "running",
                "kind": "recipe.image.availability.v2",
                "id": "recipe-download",
                "request_id": "11111111-1111-4111-8111-111111111111",
                "request": {
                    "kind": "selector",
                    "selector": "qwen-code",
                    "force": False,
                },
            },
        }
    )
    assert (
        run(("model", "download", "qwen", "--detach", "--json"), client)[1]["action"]
        == "download"
    )
    assert run(("model", "download", "qwen", "--detach", "--json"), client)[0] == 0
    assert (
        run(("recipe", "download", "qwen-code", "--detach", "--json"), client)[0] == 0
    )
    assert (
        client.calls[0][2]
        == client.calls[1][2]
        == {
            "request_key": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert client.calls[2][2] == {
        "request_key": "11111111-1111-4111-8111-111111111111",
    }


def test_recipe_download_rejects_a_forced_selector_intent_receipt() -> None:
    selector = "qwen-code"
    request_key = "11111111-1111-4111-8111-111111111111"
    path = f"/api/recipe/{selector}/download"
    lookup = f"/api/recipe/requests/{request_key}"
    receipt = {
        "id": "recipe-download",
        "request_id": request_key,
        "kind": "recipe.image.availability.v2",
        "request": {"kind": "selector", "selector": selector, "force": True},
        "state": "running",
    }
    client = FakeClient({("POST", path): receipt, ("GET", lookup): receipt})

    status, payload = run(
        (
            "recipe",
            "download",
            selector,
            "--request-key",
            request_key,
            "--detach",
            "--json",
        ),
        client,
    )

    assert status == 2
    observation = payload["observation"]
    assert isinstance(observation, dict) and observation["status"] == "timed_out"
    assert payload["result"] == {}
    assert [call[:2] for call in client.calls] == [
        ("POST", path),
        *[("GET", lookup)] * 4,
    ]
    repaired = receipt | {
        "request_id": "22222222-2222-4222-8222-222222222222",
        "request": {"kind": "selector", "selector": selector, "force": False},
    }
    client.responses[("POST", path)] = repaired
    status, result = run(
        (
            "recipe",
            "download",
            selector,
            "--request-key",
            repaired["request_id"],
            "--detach",
            "--json",
        ),
        client,
    )
    assert status == 0 and result["id"] == repaired["id"]
    assert [call[0] for call in client.calls].count("POST") == 2


def test_recipe_detail_reads_the_vocabulary_only_for_the_human_detail() -> None:
    recipes = json.loads(
        (Path(__file__).parents[1] / "fixtures/library-list.json").read_text()
    )["recipes"]
    vocabulary = json.loads(
        (
            Path(__file__).parents[1] / "fixtures/library-vocabulary-wording.json"
        ).read_text()
    )["vocabulary"]
    client = FakeClient(
        {
            ("GET", "/api/recipe/qwen"): recipes[0] | {"usage": ["chat"]},
            ("GET", "/api/library/vocabulary"): vocabulary,
        }
    )
    assert cli.main(("recipe", "detail", "qwen"), control_client=client) == 0
    assert "/api/library/vocabulary" in [call[1] for call in client.calls]

    client.calls.clear()
    assert run(("recipe", "detail", "qwen", "--json"), client)[0] == 0
    assert [call[1] for call in client.calls] == ["/api/recipe/qwen"]

    # The plain table never needs the words.
    client.calls.clear()
    cli.main(("recipe", "library"), control_client=client)
    assert "/api/library/vocabulary" not in [call[1] for call in client.calls]


def test_detail_supports_technical_query_and_nested_typed_table_fields() -> None:
    detail = FakeClient(
        {
            ("GET", "/api/model/qwen"): {
                "selector": "qwen",
                "identity": {"slug": "Qwen 3.8", "content_sha256": "digest"},
                "local": {"controller": "not_cached", "running_on": []},
                "resources": {},
                "document": {},
            },
            ("GET", "/api/model/library"): {
                "models": [
                    {
                        "selector": "qwen",
                        "identity": {"slug": "Qwen 3.8"},
                        "local": {"controller": "cached", "running_on": ["Atlas"]},
                        "resources": {"disk_bytes": 42},
                    }
                ],
            },
        }
    )
    status, payload = run(("model", "detail", "qwen", "--technical", "--json"), detail)
    technical = payload["identity"]
    assert isinstance(technical, dict)
    assert status == 0 and technical["content_sha256"] == "digest"
    assert detail.calls[0][3] == {"technical": True}


def test_cache_actions_bind_schema_two_request_and_remove_semantics() -> None:
    model_operation_id = "11111111-1111-4111-8111-111111111121"
    recipe_operation_id = "11111111-1111-4111-8111-111111111122"
    request_key = "11111111-1111-4111-8111-111111111111"
    model_detail, model_digest = _model_detail()
    client = FakeClient(
        {
            ("GET", "/api/model/qwen"): model_detail,
            ("GET", "/api/model/qwen/remove-review"): _removal_review(
                "model", "qwen", model_digest=model_digest
            ),
            ("POST", "/api/model/qwen/remove"): {
                "action": "remove",
                "selector": "qwen",
                "request_key": request_key,
                "model_content_sha256": model_digest,
                "state": "cancelled",
                "operation_id": model_operation_id,
                "phase": "completed",
                "progress": {"phase": "completed"},
                "transferred_bytes": 0,
            },
            ("GET", "/api/recipe/vision/remove-review"): _removal_review(
                "recipe", "vision", with_model=True
            ),
            ("POST", "/api/recipe/vision/remove"): {
                "state": "accepted",
                "action": "remove",
                "selector": "vision",
                "request_key": request_key,
                "operation_id": recipe_operation_id,
                "recipe_revision_id": "revision-1",
                "with_model": True,
                "progress": {"phase": "completed"},
                "reclaimed_bytes": 0,
            },
            ("GET", f"/api/recipe/operations/{recipe_operation_id}"): {
                "state": "succeeded",
                "action": "remove",
                "selector": "vision",
                "request_key": request_key,
                "operation_id": recipe_operation_id,
                "recipe_revision_id": "revision-1",
                "with_model": True,
                "progress": {"phase": "completed"},
                "reclaimed_bytes": 0,
            },
        }
    )
    model_status, model_receipt = run(
        (
            "model",
            "remove",
            "qwen",
            "--yes",
            "--detach",
            "--json",
        ),
        client,
    )
    assert model_status == 2 and model_receipt["model_content_sha256"] == model_digest
    assert [call[:2] for call in client.calls[:2]] == [
        ("GET", "/api/model/qwen/remove-review"),
        ("POST", "/api/model/qwen/remove"),
    ]
    assert client.calls[1][2] == {
        "request_key": "11111111-1111-4111-8111-111111111111",
    }
    assert (
        run(
            (
                "recipe",
                "remove",
                "vision",
                "--with-model",
                "--yes",
                "--json",
            ),
            client,
        )[0]
        == 0
    )
    assert client.calls[2][1] == "/api/recipe/vision/remove-review"
    assert client.calls[3][1] == "/api/recipe/vision/remove"
    recipe_remove = client.calls[3][2]
    assert recipe_remove is not None
    assert recipe_remove["with_model"] is True
