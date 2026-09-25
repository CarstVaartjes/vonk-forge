from __future__ import annotations

import argparse
import json
import re
import uuid
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from io import StringIO

import pytest
from vonk_forge_contracts import ModelDefinition, content_sha256

from cluster_profiles import cli, controller_cli
from cluster_profiles.cli_render import progress_line, render_payload
from cluster_profiles.control_client import (
    ControlForbidden,
    ControlHTTPError,
    ControlNotFound,
    ControlTransportError,
    ControlUnavailable,
    _operation,
    validate_control_document,
)
from cluster_profiles.generated_control.models.fleet_profile_endpoints_view import (
    FleetProfileEndpointsView,
)

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


class FakeClient:
    """Strict in-process adapter for the current operator wire contracts."""

    request_timeout_seconds = 15.0

    def __init__(self, responses: dict[tuple[str, str], object]):
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object] | None, object]] = []

    def request(
        self,
        method,
        path,
        payload=None,
        *,
        extra_headers=None,
        query=None,
        timeout_seconds=None,
    ):
        self._validate_request(method, path, payload, query)
        self.calls.append((method, path, payload, query))
        response = self.responses.get((method, path), {})
        if isinstance(response, list):
            # The final entry is sticky so a caller can describe a dependency
            # that keeps answering the same way, including one that stays
            # unavailable for the whole bounded observation.
            response = response.pop(0) if len(response) > 1 else response[0]
        if isinstance(response, BaseException):
            raise response
        return response

    def profile_endpoints(self, number, alias=None):
        payload = self.request(
            "GET",
            f"/api/profile/{number}/endpoints",
            query={"alias": alias} if alias is not None else None,
        )
        if not isinstance(payload, dict):
            raise TypeError("profile endpoints response must be an object")
        return FleetProfileEndpointsView.from_dict(payload)

    def _validate_request(self, method, path, payload, query):
        profile_path = re.fullmatch(
            r"/api/profile/(\d+)(?:/(definition|preview|load|progress|endpoints))?",
            path,
        )
        application_path = re.fullmatch(
            r"/api/profile/applications/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            path,
        )
        application_cancel_path = re.fullmatch(
            r"/api/profile/applications/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/cancel",
            path,
        )
        application_cancel_receipt_path = re.fullmatch(
            r"/api/profile/applications/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/cancellations/[0-9a-f]{8}-"
            r"[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            path,
        )
        request_path = re.fullmatch(
            r"/api/profile/\d+/requests/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            path,
        )
        selector_path = re.fullmatch(
            r"/api/(model|recipe)/[^/]+(?:/(download|remove|remove-review))?", path
        )
        operation_path = re.fullmatch(
            r"/api/(model|recipe)/(operations|requests)/[^/]+(?:/cancel)?", path
        )
        model_cancel_path = re.fullmatch(
            r"/api/model/operations/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/cancel",
            path,
        )
        known_get = {
            "/api/fleet",
            "/api/model",
            "/api/model/library",
            "/api/recipe",
            "/api/recipe/library",
            "/api/profile",
            "/api/operations",
        }
        job_path = re.fullmatch(r"/api/jobs/[^/]+", path)
        resume_path = re.fullmatch(r"/api/jobs/[^/]+/resume", path)
        known = (
            path in known_get
            or job_path is not None
            or resume_path is not None
            or profile_path is not None
            or application_path is not None
            or application_cancel_path is not None
            or application_cancel_receipt_path is not None
            or request_path is not None
            or selector_path is not None
            or operation_path is not None
            or model_cancel_path is not None
        )
        known = (
            known or re.fullmatch(r"/api/fleet/[^/]+(?:/loginfo)?", path) is not None
        )
        known = known or path in {
            "/api/fleet/enroll",
            "/api/fleet/upgrade",
            "/api/recipe/update",
            "/api/recipe/operations/op-cancel/cancel",
        }
        known = known or re.fullmatch(r"/api/jobs/[^/]+", path) is not None
        known = (
            known
            or re.fullmatch(r"/api/fleet/[^/]+/(rename|re-enroll|remove)", path)
            is not None
        )
        if not known:
            raise AssertionError(
                f"CLI emitted retired or invented route: {method} {path}"
            )

        if method == "GET":
            if path.endswith("/library"):
                parameters = _operation(path, method)["parameters"]
                assert isinstance(parameters, list)
                assert query is None or set(query) <= {
                    item["name"] for item in parameters if item["in"] == "query"
                }
            elif path.startswith("/api/fleet/") and not path.endswith("/loginfo"):
                assert query is None or set(query) <= {
                    "metrics",
                    "range",
                    "device",
                    "interface",
                    "run",
                    "capabilities",
                    "technical",
                }
            elif path.endswith("/loginfo"):
                assert query is None or set(query) <= {
                    "since",
                    "lines",
                    "recipe",
                    "source",
                    "follow",
                }
            elif selector_path is not None and selector_path.group(2) is None:
                assert query is None or set(query) <= {"technical"}
            elif (
                selector_path is not None and selector_path.group(2) == "remove-review"
            ):
                if selector_path.group(1) == "recipe":
                    assert query is not None and set(query) == {"with_model"}
                    assert type(query["with_model"]) is bool
                else:
                    assert query is None
            elif operation_path is not None:
                assert query is None
            elif path.startswith("/api/jobs/"):
                assert query is None or set(query) <= {"limit"}
            elif path.endswith("/endpoints"):
                assert query is None or set(query) <= {"alias"}
            elif path == "/api/operations":
                assert query is None or set(query) <= {
                    "cursor",
                    "limit",
                    "state",
                    "node_id",
                    "request_id",
                }
            else:
                assert query is None
            return

        if method == "PUT":
            assert profile_path is not None and profile_path.group(2) is None
            validate_control_document("FleetProfileInput", payload)
            return

        if method != "POST":
            raise AssertionError(f"unexpected method: {method}")
        if resume_path is not None:
            assert payload == {"disposition": "resume"}
            return
        if path.endswith("/preview"):
            assert profile_path is not None and payload is None
        elif path.endswith("/load"):
            assert profile_path is not None
            validate_control_document("FleetProfileLoadRequest", payload)
        elif application_cancel_path is not None:
            assert isinstance(payload, dict)
            assert set(payload) == {"profile_number", "request_key"}
            assert (
                type(payload["profile_number"]) is int and payload["profile_number"] > 0
            )
            uuid.UUID(payload["request_key"])
        elif selector_path is not None and selector_path.group(2) in {
            "download",
            "remove",
        }:
            assert isinstance(payload, dict)
            if selector_path.group(1) == "model" and selector_path.group(2) == "remove":
                assert set(payload) == {
                    "schema_version",
                    "request_key",
                    "model_content_sha256",
                    "review_digest",
                }
                assert payload["schema_version"] == 2
                uuid.UUID(payload["request_key"])
                assert re.fullmatch(r"[0-9a-f]{64}", payload["model_content_sha256"])
                assert re.fullmatch(r"[0-9a-f]{64}", payload["review_digest"])
            else:
                assert set(payload) <= {
                    "schema_version",
                    "request_key",
                    "with_model",
                    "review_digest",
                }
                assert payload["schema_version"] == 2
                uuid.UUID(payload["request_key"])
                if selector_path.group(2) == "remove":
                    assert set(payload) == {
                        "schema_version",
                        "request_key",
                        "with_model",
                        "review_digest",
                    }
                    assert re.fullmatch(r"[0-9a-f]{64}", payload["review_digest"])
            if selector_path.group(1) == "model":
                assert "with_model" not in payload
            if selector_path.group(2) == "download":
                assert "yes" not in payload and "with_model" not in payload
        elif path == "/api/recipe/update":
            assert isinstance(payload, dict)
            assert set(payload) == {"schema_version", "request_key", "selectors", "all"}
            assert payload["schema_version"] == 2
            uuid.UUID(payload["request_key"])
            assert isinstance(payload["selectors"], list)
            assert isinstance(payload["all"], bool)
        elif model_cancel_path is not None:
            assert isinstance(payload, dict)
            assert set(payload) == {"schema_version", "request_key", "reason"}
            assert payload["schema_version"] == 2
            uuid.UUID(payload["request_key"])
            assert isinstance(payload["reason"], str) and payload["reason"].strip()
        elif re.fullmatch(r"/api/recipe/operations/[^/]+/cancel", path):
            assert isinstance(payload, dict)
            validate_control_document("RecipeCancellationRequest", payload)
        elif path == "/api/fleet/upgrade":
            assert (
                {"all", "request_key", "strategy"}
                <= set(payload)
                <= {
                    "selectors",
                    "all",
                    "request_key",
                    "strategy",
                }
            )
            assert payload["strategy"] == "one-at-a-time"
            uuid.UUID(payload["request_key"])
            if "selectors" in payload:
                assert isinstance(payload["selectors"], list) and payload["selectors"]
        elif path == "/api/fleet/enroll":
            assert set(payload) == {"name", "ttl_seconds"}
        elif path.endswith("/rename"):
            assert set(payload) == {"display_name"}
        elif path.endswith(("/re-enroll", "/remove")):
            assert payload is None
        else:
            raise AssertionError(f"invented mutation payload route: {path}")


def run(argv: tuple[str, ...], client: FakeClient) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        status = cli.main(
            argv,
            control_client=client,
            request_id_factory=lambda: "11111111-1111-4111-8111-111111111111",
        )
    return status, json.loads(output.getvalue())


def _model_detail(selector: str = "qwen") -> tuple[dict[str, object], str]:
    document_path = files("vonk_forge_contracts").joinpath(
        "examples", "model-definition.json"
    )
    model = ModelDefinition.model_validate(json.loads(document_path.read_text()))
    identity = model.identity
    digest = content_sha256(model)
    return (
        {
            "schema_version": 2,
            "selector": selector,
            "identity": {
                "kind": "model",
                "publisher": identity.publisher,
                "slug": identity.slug,
                "content_sha256": digest,
            },
            "document": model.model_dump(mode="json"),
            "family": f"{identity.family.publisher}/{identity.family.slug}",
            "version": identity.version,
            "variant": identity.variant,
            "quantization": model.format.quantization,
            "usage": [],
            "resources": {"disk_bytes": model.download_bytes},
            "local": {"controller": "not_cached", "running_on": []},
            "updated_at": "2026-09-24T00:00:00Z",
            "alignment": [],
        },
        digest,
    )


def _recipe_removal_receipt(
    selector: str, request_key: str, *, with_model: bool
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "action": "remove",
        "selector": selector,
        "request_key": request_key,
        "operation_id": "11111111-1111-4111-8111-111111111121",
        "recipe_revision_id": "revision-1",
        "with_model": with_model,
        "review_digest": "b" * 64,
        "progress": {"phase": "queued"},
        "reclaimed_bytes": 0,
        "state": "queued",
    }


def _removal_review(
    noun: str,
    selector: str,
    *,
    model_digest: str | None = None,
    with_model: bool | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "action": "remove",
        "resource_kind": noun,
        "selector": selector,
        "target_identity": model_digest or "revision-1",
        "with_model": with_model,
        "assets": [],
        "references": [],
        "active_work": [],
        "blockers": [],
        "observed_at": "2026-09-24T10:00:00Z",
        "review_digest": "b" * 64,
    }


def test_parser_exposes_current_singular_operator_roots_and_update() -> None:
    parser = cli._parser()
    assert set(_subparser_choices(parser)) == {
        "fleet",
        "model",
        "recipe",
        "profile",
        "update",
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
            "waiting-for-operator",
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
                "state": "waiting-for-operator",
                "node_id": target,
                "request_id": request_id,
            },
        )
    ]


def test_fleet_resume_requires_owner_advertised_action_and_posts_explicit_intent() -> (
    None
):
    job_id = "11111111-1111-4111-8111-111111111111"
    detail = {
        "id": job_id,
        "state": "waiting-for-operator",
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
        ("GET", f"/api/jobs/{job_id}", None, None),
        ("POST", f"/api/jobs/{job_id}/resume", {"disposition": "resume"}, None),
    ]


def test_fleet_resume_rejects_a_receipt_for_another_job() -> None:
    job_id = "11111111-1111-4111-8111-111111111111"
    other_job_id = "22222222-2222-4222-8222-222222222222"
    client = FakeClient(
        {
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": "waiting-for-operator",
                "recovery": {"actions": ["resume"]},
            },
            ("POST", f"/api/jobs/{job_id}/resume"): {
                "id": other_job_id,
                "state": "queued",
            },
        }
    )

    status, payload = run(("fleet", "resume", job_id, "--yes", "--json"), client)

    assert status == 2
    assert other_job_id not in json.dumps(payload)
    assert "another job" in str(payload["error"]).lower()


def test_fleet_resume_does_not_post_after_action_disappears() -> None:
    job_id = "11111111-1111-4111-8111-111111111111"
    client = FakeClient(
        {
            ("GET", f"/api/jobs/{job_id}"): {
                "id": job_id,
                "state": "waiting-for-operator",
                "recovery": {"actions": ["inspect"]},
            }
        }
    )

    status, payload = run(("fleet", "resume", job_id, "--yes", "--json"), client)

    assert status != 0
    assert payload["error_type"] == "control_api"
    error = payload["error"]
    assert isinstance(error, str)
    assert "no currently advertised authorized resume action" in error
    assert [call[0] for call in client.calls] == ["GET"]


def test_activity_presentation_keeps_exact_reconnect_and_complete_continuation() -> (
    None
):
    request_id = "33333333-3333-4333-8333-333333333333"
    target = "spk_" + "1" * 32
    operation = {
        "id": "operation-1",
        "kind": "recipe.stop",
        "state": "waiting-for-operator",
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
    audit = {
        "id": "audit:55555555-5555-4555-8555-555555555555",
        "kind": "audit.fleet.read",
        "state": "completed",
        "created_at": "2026-08-05T12:00:00Z",
        "node_ids": [target],
        "owner": {
            "kind": "audit-event",
            "id": "55555555-5555-4555-8555-555555555555",
            "request_id": request_id,
        },
    }
    output = StringIO()
    with redirect_stdout(output):
        render_payload(
            {
                "operations": [operation, audit],
                "total": 3,
                "next_cursor": "opaque+token",
            },
            "fleet",
            action="activity",
            activity_filters={
                "limit": 1,
                "state": "waiting-for-operator",
                "target": target,
                "request_id": request_id,
            },
        )

    rendered = output.getvalue()
    assert "2 of 3 references" in rendered
    assert "Owner: job job-owner-1" in rendered
    assert "vonkctl fleet progress job-owner-1 --follow" in rendered
    assert "vonkctl fleet activity --request-id " + request_id in rendered
    assert "More results are available" in rendered
    assert (
        "vonkctl fleet activity --limit 1 --cursor opaque+token --state "
        f"waiting-for-operator --target {target} --request-id {request_id}"
    ) in rendered


def test_fleet_loginfo_line_count_is_bounded_without_enumerating_choices() -> None:
    parser = cli._parser()
    fleet = _subparser_choices(parser)["fleet"]
    loginfo = _subparser_choices(fleet)["loginfo"]

    for accepted in ("1", "1000"):
        assert loginfo.parse_args(["Atlas", "--lines", accepted]).lines == int(accepted)
    for rejected in ("0", "1001", "not-a-number"):
        with pytest.raises(cli._UsageError):
            loginfo.parse_args(["Atlas", "--lines", rejected])
        output = StringIO()
        with redirect_stdout(output):
            status = cli.main(
                ("fleet", "loginfo", "Atlas", "--lines", rejected, "--json")
            )
        assert status == 2
        assert json.loads(output.getvalue())["error_type"] == "arguments"

    help_text = loginfo.format_help()
    assert "--lines 1-1000" in help_text
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
        ("recipe", "library", "--all-models", "--model", "vision", "--json"),
        recipe,
    )
    assert status == 0
    assert recipe.calls[0][1] == "/api/recipe/library"
    assert recipe.calls[0][3] == {
        "sort": "updated",
        "limit": 100,
        "model": ["vision"],
        "all_models": True,
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
            "schema_version": 2,
            "request_key": "11111111-1111-4111-8111-111111111111",
        }
    )
    assert client.calls[2][2] == {
        "schema_version": 2,
        "request_key": "11111111-1111-4111-8111-111111111111",
    }


def test_recipe_download_rejects_a_forced_selector_intent_receipt() -> None:
    selector = "qwen-code"
    request_key = "11111111-1111-4111-8111-111111111111"
    path = f"/api/recipe/{selector}/download"
    lookup = f"/api/recipe/requests/{request_key}"
    receipt = {
        "schema_version": 2,
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
    assert "acceptance is unknown" in str(payload.get("error")).lower()
    assert [call[:2] for call in client.calls] == [("POST", path), ("GET", lookup)]


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
            ("GET", "/api/model"): {
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
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(("model",), control_client=detail) == 0
    text = output.getvalue()
    assert "cached" in text and "Atlas" in text and "42 B" in text


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
                "schema_version": 2,
                "action": "remove",
                "selector": "qwen",
                "request_key": request_key,
                "model_content_sha256": model_digest,
                "review_digest": _REVIEW_DIGEST,
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
                "schema_version": 2,
                "state": "accepted",
                "action": "remove",
                "selector": "vision",
                "request_key": request_key,
                "operation_id": recipe_operation_id,
                "recipe_revision_id": "revision-1",
                "with_model": True,
                "review_digest": _REVIEW_DIGEST,
                "progress": {"phase": "completed"},
                "reclaimed_bytes": 0,
            },
            ("GET", f"/api/recipe/operations/{recipe_operation_id}"): {
                "schema_version": 2,
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
            "--review-digest",
            _REVIEW_DIGEST,
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
        "schema_version": 2,
        "request_key": "11111111-1111-4111-8111-111111111111",
        "model_content_sha256": model_digest,
        "review_digest": _REVIEW_DIGEST,
    }
    assert (
        run(
            (
                "recipe",
                "remove",
                "vision",
                "--with-model",
                "--review-digest",
                _REVIEW_DIGEST,
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


def test_model_remove_reconciles_the_exact_digest_after_lost_acceptance() -> None:
    selector = "qwen"
    request_key = "11111111-1111-4111-8111-111111111111"
    operation_id = "11111111-1111-4111-8111-111111111121"
    _detail, digest = _model_detail(selector)
    receipt = {
        "schema_version": 2,
        "action": "remove",
        "selector": selector,
        "request_key": request_key,
        "model_content_sha256": digest,
        "review_digest": _REVIEW_DIGEST,
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
            "--review-digest",
            _REVIEW_DIGEST,
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
        "schema_version": 2,
        "request_key": request_key,
        "model_content_sha256": digest,
        "review_digest": _REVIEW_DIGEST,
    }


def test_model_remove_reconnects_to_existing_key_before_resolving_current_head() -> (
    None
):
    selector = "qwen"
    request_key = "11111111-1111-4111-8111-111111111111"
    receipt = {
        "schema_version": 2,
        "action": "remove",
        "selector": selector,
        "request_key": request_key,
        "model_content_sha256": "a" * 64,
        "review_digest": _REVIEW_DIGEST,
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
            "--review-digest",
            _REVIEW_DIGEST,
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
        "schema_version": 2,
        "request_key": request_key,
        "with_model": False,
        "review_digest": _REVIEW_DIGEST,
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
    client = FakeClient({("GET", lookup): receipt})

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

    assert status == 2
    assert "receipt" in str(payload.get("error"))
    assert [call[:2] for call in client.calls] == [("GET", lookup)]


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
        ("model", "qwen", "model_content_sha256", "b" * 64),
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
            "schema_version": 2,
            "action": "remove",
            "selector": selector,
            "request_key": request_key,
            "model_content_sha256": model_digest,
            "review_digest": _REVIEW_DIGEST,
            "operation_id": operation_id,
            "state": "succeeded",
            "phase": "completed",
            "progress": {"phase": "completed"},
            "transferred_bytes": 0,
        }
    else:
        receipt = {
            "schema_version": 2,
            "action": "remove",
            "selector": selector,
            "request_key": request_key,
            "operation_id": operation_id,
            "recipe_revision_id": "revision-1",
            "with_model": False,
            "review_digest": _REVIEW_DIGEST,
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
                "--review-digest",
                _REVIEW_DIGEST,
                "--json",
            )
            if model_receipt_only_reconnect
            else (
                noun,
                "remove",
                selector,
                "--yes",
                "--review-digest",
                _REVIEW_DIGEST,
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
            "--review-digest",
            _REVIEW_DIGEST,
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

    error = payload.get("error")
    assert status == 2
    if bad_field == "model_content_sha256":
        assert isinstance(error, str) and "acceptance is unknown" in error
        expected_calls = [
            ("GET", f"/api/model/{selector}/remove-review"),
            ("POST", path),
            ("GET", f"/api/model/requests/{request_key}"),
        ]
    else:
        if noun == "recipe":
            assert isinstance(error, str) and "acceptance is unknown" in error
            expected_calls = [
                ("GET", f"/api/recipe/{selector}/remove-review"),
                ("POST", path),
                ("GET", f"/api/recipe/requests/{request_key}"),
            ]
        else:
            assert isinstance(error, str) and "receipt" in error
            expected_calls = [("GET", f"/api/model/requests/{request_key}")]
    assert [call[:2] for call in client.calls] == expected_calls
    if model_receipt_only_reconnect:
        submission = payload["submission"]
        assert isinstance(submission, dict)
        assert submission["acceptance"] == "not_submitted"
    if noun == "recipe":
        query = client.calls[0][3]
        assert query == {"with_model": False}


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
    monkeypatch.setattr(controller_cli.sys, "stdin", input_stream)
    monkeypatch.setattr(controller_cli.sys, "stderr", error_stream)
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
        "state": "cancelling",
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
    assert status == 0 and payload["state"] == "cancelling"
    assert client.calls == [
        (
            "POST",
            path,
            {
                "schema_version": 2,
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
        "schema_version": 2,
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
                    {
                        "selector": "vonk-forge/recipe-uuid",
                        "identity": {
                            "publisher": "vonk-forge",
                            "slug": "recipe-uuid",
                            "title": "Recipe UUID",
                        },
                    }
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
    assert client.calls == [
        ("GET", "/api/profile/2/definition", None, None),
        (
            "GET",
            "/api/recipe/library",
            None,
            {"all_models": True, "limit": 512, "sort": "name", "assess": False},
        ),
        ("GET", "/api/fleet", None, None),
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
                "recipes": [
                    {
                        "selector": "vonk-forge/qwen-code",
                        "identity": {
                            "publisher": "vonk-forge",
                            "slug": "qwen-code",
                            "title": "Qwen Code",
                        },
                    }
                ],
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
    assert [call[1] for call in client.calls] == [
        "/api/profile/1/definition",
        "/api/recipe/library",
        "/api/fleet",
    ]


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
                    {
                        "selector": "vonk-forge/recipe-uuid",
                        "identity": {"title": "Recipe UUID", "slug": "recipe-uuid"},
                    }
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


def _accepted_profile_load(identity: str, state: str) -> dict[str, object]:
    return {
        "id": identity,
        "state": state,
        "request_key": "11111111-1111-4111-8111-111111111111",
        "progress": {"intended_profile": {"reviewed_plan_digest": "c" * 64}},
    }


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
            "state": "cancelling",
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


def test_lost_profile_cancel_does_not_replay_a_mismatched_owner_receipt() -> None:
    application_id = "99999999-9999-4999-8999-999999999999"
    request_key = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    path = f"/api/profile/applications/{application_id}/cancel"
    lookup = f"/api/profile/applications/{application_id}/cancellations/{request_key}"
    client = FakeClient(
        {
            ("POST", path): ControlTransportError("connection reset"),
            ("GET", lookup): _accepted_profile_cancellation(
                application_id, "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            ),
        }
    )

    status, _result = run(
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

    assert status == 2
    assert [call[:2] for call in client.calls] == [("POST", path), ("GET", lookup)]


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
            "--expected-plan",
            "c" * 64,
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
        "plan_digest": "c" * 64,
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
    status, _payload = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--expected-plan",
            "c" * 64,
            "--yes",
            "--json",
        ),
        client,
    )

    assert status != 0
    assert [call[0] for call in client.calls] == ["POST", "GET"]
    assert not any(call[1].endswith("/progress") for call in client.calls)


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


def test_profile_application_selector_must_belong_to_explicit_profile() -> None:
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
            "--application",
            application_id,
            "--json",
        ),
        client,
    )

    assert status == 2
    assert "profile 4" in str(payload).lower()
    assert [call[1] for call in client.calls] == [
        "/api/profile/4",
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
        ("profile", "progress", "--application", application_id, "--json"),
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
            "--application",
            application_id,
            "--json",
        ),
        client,
    )

    assert status == 0 and payload["id"] == application_id


def test_profile_progress_follow_rejects_a_different_application_identity() -> None:
    selected_application = "33333333-3333-4333-8333-333333333333"
    replacement_application = "44444444-4444-4444-8444-444444444444"
    path = f"/api/profile/applications/{selected_application}"
    client = FakeClient(
        {
            ("GET", path): [
                {"id": selected_application, "state": "running"},
                {"id": replacement_application, "state": "succeeded"},
            ]
        }
    )

    status, payload = run(
        (
            "profile",
            "progress",
            "--application",
            selected_application,
            "--follow",
            "--interval-seconds",
            "0.01",
            "--json",
        ),
        client,
    )

    assert status == 2
    assert "application" in str(payload).lower()
    assert [call[1] for call in client.calls] == [path, path]


def test_profile_application_selector_rejects_a_different_returned_identity() -> None:
    requested_application = "33333333-3333-4333-8333-333333333333"
    returned_application = "44444444-4444-4444-8444-444444444444"
    client = FakeClient(
        {
            (
                "GET",
                f"/api/profile/applications/{requested_application}",
            ): {"id": returned_application, "state": "succeeded"}
        }
    )

    status, payload = run(
        (
            "profile",
            "progress",
            "--application",
            requested_application,
            "--json",
        ),
        client,
    )

    assert status == 2
    assert "another application" in str(payload).lower()


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
    assert observation["error"] == "control API reported unavailable"
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
    status, payload = run(("profile", "progress", "--follow", "--json"), client)

    assert status != 0
    assert payload["code"] == "http.403"
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
            "--expected-plan",
            "c" * 64,
            "--yes",
            "--json",
        ),
        client,
    )

    assert status != 0
    assert payload["request_key"] == "11111111-1111-4111-8111-111111111111"
    reconcile = payload["reconcile"]
    assert isinstance(reconcile, dict)
    assert reconcile["request_key"] == payload["request_key"]


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
            "--expected-plan",
            "c" * 64,
            "--yes",
            "--json",
        ),
        client,
    )

    assert status == 0
    assert payload["id"] == operation
    assert [call[0] for call in client.calls] == ["POST", "GET"]


def test_accepted_load_keeps_reconciliation_after_observation_not_found() -> None:
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
            ): ControlNotFound(404, "application observation is unavailable"),
        }
    )

    status, payload = run(
        (
            "--profile",
            "1",
            "profile",
            "load",
            "--expected-plan",
            "c" * 64,
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

    assert status == 2
    submission = payload.get("submission")
    assert isinstance(submission, dict)
    assert submission["acceptance"] == "accepted"
    reconcile = payload.get("reconcile")
    assert isinstance(reconcile, dict)
    assert reconcile == {
        "operation": (
            f"vonkctl --profile 1 profile progress --request-key {key} --follow"
        ),
        "request_key": key,
    }
    assert [call[:2] for call in client.calls] == [
        ("POST", "/api/profile/1/load"),
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
            "--expected-plan",
            "c" * 64,
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
    assert (
        progress_line(
            {
                "state": "running",
                "progress": {
                    "phase": "building",
                    "completed_items": 4,
                    "total_items": 7,
                },
            }
        )
        == "building | items 4 / 7"
    )
    assert (
        progress_line(
            {
                "state": "running",
                "progress": {
                    "phase": "copying",
                    "completed_bytes": 20,
                    "total_bytes_known": False,
                },
            }
        )
        == "copying | 20 B; total unknown"
    )


def test_profile_terminal_render_shows_effective_initial_budget_and_runtime_phase() -> (
    None
):
    output = StringIO()
    with redirect_stdout(output):
        render_payload(
            {
                "state": "running",
                "progress": {
                    "current_label": "start Mia",
                    "child_progress": {
                        "phase": "runtime-install",
                        "operation": {"phase": "jit"},
                        "startup_budget_seconds": 1800,
                        "start_deadline": "2026-09-14T20:00:00Z",
                    },
                },
            },
            "profile",
            action="progress",
        )
    rendered = output.getvalue()
    assert "start Mia" in rendered
    assert "load/JIT phase: jit" in rendered
    assert "initial start budget: 1800 seconds" in rendered
    assert "initial start deadline: 2026-09-14T20:00:00Z" in rendered


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
        ):
            self._validate_request(method, path, payload, query)
            self.calls.append((method, path, payload, query))
            if method == "PUT":
                raise ValueError("profile revision conflict")
            if path == "/api/recipe/library":
                return {
                    "recipes": [
                        {
                            "selector": "vonk-forge/qwen-code",
                            "identity": {
                                "publisher": "vonk-forge",
                                "slug": "qwen-code",
                                "title": "Qwen Code",
                            },
                        }
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
    assert payload["error"] == "profile revision conflict"
    assert [call[0] for call in client.calls] == ["GET", "GET", "GET", "PUT"]


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


def test_profile_endpoint_uses_scoped_current_controller_projection(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
                            "api_base": "http://10.0.0.10:8000/v1",
                            "expires_at": "2026-09-23T13:00:00Z",
                            "generation": 8,
                            "node_id": "spk_" + "a" * 32,
                            "observed_at": "2026-09-23T12:59:30Z",
                            "plan_digest": "a" * 64,
                            "state": "published",
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
    presentation = capsys.readouterr()
    assert "Client model identifier: example-model" in presentation.out
    assert "Route generation: 8" in presentation.out
    assert (
        "Freshness: observed 1 second before this Controller read" in presentation.out
    )
    assert "installed-only" in presentation.out
    assert "API base: http://10.0.0.10:8000/v1" in presentation.out
    assert "Authorization" not in presentation.out
    assert client.calls == [("GET", "/api/profile/3/endpoints", None, None)]


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

    assert status == 2
    assert "not part of profile 3" in str(payload["error"])
    assert client.calls == [
        ("GET", "/api/profile/3/endpoints", None, {"alias": "missing"})
    ]


def test_profile_endpoint_invalid_history_does_not_claim_alias_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "number": 3,
        "profile_id": "11111111-1111-4111-8111-111111111111",
        "application_id": "22222222-2222-4222-8222-222222222222",
        "application_state": "cancelled",
        "observed_at": "2026-09-23T12:59:31Z",
        "assignments": None,
        "projection_issue": {
            "code": "profile.application_intent.invalid",
            "detail": "stored document is invalid at assignments.0 (missing)",
        },
    }

    class UnavailableEndpointClient:
        def profile_endpoints(self, number: int, alias: str | None = None):
            assert number == 3
            assert alias == "qwen"
            return type("EndpointResponse", (), {"to_dict": lambda _self: payload})()

    monkeypatch.setattr(
        controller_cli,
        "validate_control_document",
        lambda _name, value: value,
    )
    status = cli.main(
        ("--profile", "3", "profile", "endpoint", "qwen"),
        control_client=UnavailableEndpointClient(),
    )

    output = capsys.readouterr().out
    assert status == 0
    assert "Endpoint assignments are unavailable" in output
    assert "stored document is invalid at assignments.0 (missing)" in output
    assert "not part of profile 3" not in output


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
    assert payload["error_type"] == "control_api"


def test_plain_output_is_adaptive_and_keeps_identity_before_optional_columns() -> None:
    client = FakeClient(
        {
            ("GET", "/api/model"): {
                "models": [
                    {
                        "selector": "qwen-3.8-nvfp4",
                        "usage": ["code"],
                        "identity": {"slug": "Qwen 3.8"},
                        "local": {"controller": "cached", "running_on": []},
                        "resources": {},
                    }
                ],
            }
        }
    )
    output = StringIO()
    with redirect_stdout(output):
        assert cli.main(("model",), control_client=client) == 0
    text = output.getvalue()
    assert "MODEL" in text and "USE qwen-3.8-nvfp4" in text
    assert "Qwen 3.8" in text


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
    assert status == 0 and payload["state"] == "succeeded"
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
    assert status == 0 and result["state"] == "succeeded"
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
    assert status == 0 and result["state"] == "succeeded"
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
        "state": "cancelling",
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
    assert status == 0 and result["state"] == "cancelling"
    assert [(method, path) for method, path, _, _ in client.calls] == [
        ("POST", "/api/recipe/operations/recipe-operation/cancel"),
        ("GET", "/api/recipe/operations/recipe-operation"),
    ]
    assert client.calls[0][2] == {
        "schema_version": 2,
        "request_key": key,
        "reason": "stop preparation",
    }


def test_recipe_cancel_requires_explicit_yes_before_mutation() -> None:
    client = FakeClient({})
    status, result = run(("recipe", "cancel", "operation", "--json"), client)
    assert status == 2 and "requires --yes" in str(result["error"])
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
def test_cache_reconnection_rejects_a_different_binding(changed_binding: str) -> None:
    key = "11111111-1111-4111-8111-111111111111"
    initial = {"operation_id": "original", "request_key": key, "state": "running"}
    if changed_binding == "request":
        initial["request_key"] = "22222222-2222-4222-8222-222222222222"
    client = FakeClient(
        {
            ("GET", f"/api/model/requests/{key}"): initial,
            ("GET", "/api/model/operations/original"): initial
            | {"operation_id": "different", "state": "succeeded"},
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
    assert status == 2
    assert isinstance(result["error"], str) and "another" in result["error"]
    assert len(client.calls) == (1 if changed_binding == "request" else 2)


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
    assert status == 0 and payload["state"] == "queued"
    assert [call[1] for call in client.calls] == ["/api/recipe/qwen-code/download"]


def test_watch_shows_updated_resource_and_retains_final_snapshot(capsys) -> None:
    common = {
        "identity": {"slug": "Qwen"},
        "selector": "qwen",
        "usage": [],
        "resources": {"disk_bytes": 42},
    }
    client = FakeClient(
        {
            ("GET", "/api/model/qwen"): [
                {**common, "local": {"controller": "preparing", "running_on": []}},
                {**common, "local": {"controller": "cached", "running_on": []}},
            ]
        }
    )
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
    assert len(client.calls) >= 2
    assert "preparing" in captured.err and "cached" in captured.err
    assert "preparing" not in captured.out and "cached" in captured.out
    assert "accepted work" not in captured.err


def test_noninteractive_removals_fail_closed_and_recipe_requires_model_choice() -> None:
    model = FakeClient({})
    status, payload = run(("model", "remove", "qwen", "--json"), model)
    removal_error = payload["error"]
    assert isinstance(removal_error, str)
    assert status == 2 and "--review-digest SHA256 --yes" in removal_error
    assert model.calls == []

    recipe = FakeClient({})
    status, payload = run(("recipe", "remove", "qwen-code", "--yes", "--json"), recipe)
    model_choice_error = payload["error"]
    assert isinstance(model_choice_error, str)
    assert status == 2 and "--with-model or --keep-model" in model_choice_error
    assert recipe.calls == []


def test_terminal_partial_and_failure_states_have_nonzero_exit_codes() -> None:
    partial = FakeClient(
        {
            ("POST", "/api/model/qwen/download"): {
                "state": "partial",
                "operation_id": "download",
                "request_key": "11111111-1111-4111-8111-111111111111",
                "action": "download",
                "selector": "qwen",
            }
        }
    )
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
    assert run(("model", "download", "qwen", "--json"), partial)[0] == 1
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
        "strategy": "one-at-a-time",
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
    assert f"Upgrade Spark {node_id} one at a time? [y/N]" in terminal_output.getvalue()
    assert client.calls[-1][0:2] == ("POST", "/api/fleet/upgrade")


def _fleet_log_response(node_id: str, *, follow: bool) -> dict[str, object]:
    return {
        "schema_version": 2,
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

    assert status == 2
    assert "another spark" in str(payload["error"]).lower()
    assert [call[1] for call in client.calls] == ["/api/fleet", path, path]
    first_query = client.calls[-2][3]
    second_query = client.calls[-1][3]
    assert isinstance(first_query, dict) and isinstance(second_query, dict)
    assert second_query == first_query
    assert second_query["recipe"] == "vllm"
    assert second_query["source"] == "job"


def test_fleet_loginfo_validates_identity_even_for_a_canonical_selector() -> None:
    node_id = "spk_" + "1" * 32
    other_node_id = "spk_" + "2" * 32
    path = f"/api/fleet/{node_id}/loginfo"
    client = FakeClient(
        {("GET", path): _fleet_log_response(other_node_id, follow=False)}
    )

    status, payload = run(("fleet", "loginfo", node_id, "--json"), client)

    assert status == 2
    assert "another spark" in str(payload["error"]).lower()
    assert [call[1] for call in client.calls] == [path]


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
                "state": "waiting-for-operator",
                "kind": "agent-upgrade",
                "targets": [node_a, node_b],
                "status_reason": f"Spark {node_a} requires operator review",
                "operations": [
                    {
                        "id": "operation-a",
                        "node_id": node_a,
                        "state": "waiting-for-operator",
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
    assert result["state"] == "waiting-for-operator"
    assert result["status_reason"] == f"Spark {node_a} requires operator review"
    assert [call[1] for call in client.calls] == [
        "/api/fleet/upgrade",
        f"/api/jobs/{job_id}",
    ]
    assert client.calls[-1][3] == {"limit": 100}
    assert isinstance(result["operations"], list)
    assert len(result["operations"]) == 1


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


def test_fleet_upgrade_unknown_acceptance_recommends_exact_request_replay() -> None:
    request_key = "22222222-2222-4222-8222-222222222222"
    client = FakeClient(
        {
            ("POST", "/api/fleet/upgrade"): [
                OSError("connection reset after acceptance"),
                OSError("connection reset after replay"),
            ]
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
    assert result["error"] == "Upgrade acceptance is unknown"
    assert result["reconcile"] == {
        "operation": (
            "vonkctl fleet upgrade --all --request-key "
            f"{request_key} --strategy one-at-a-time --yes"
        ),
        "request_key": request_key,
    }
    assert len(client.calls) == 2
    assert client.calls[0][2] == client.calls[1][2]


def test_cli_rejects_retired_fleet_upgrade_strategy_before_request() -> None:
    client = FakeClient({})

    status, _result = run(
        ("fleet", "upgrade", "--all", "--strategy", "all-at-once", "--json"),
        client,
    )

    assert status == 2
    assert not client.calls


@pytest.mark.parametrize("since", ["yesterday", "15", "-1m", "2026-09-13T08:00:00"])
def test_log_since_rejects_ambiguous_windows_before_request(since: str) -> None:
    client = FakeClient({})
    assert (
        run(("fleet", "loginfo", "Atlas", "--since", since, "--json"), client)[0] == 2
    )
    assert not client.calls


@pytest.mark.parametrize("follow", [False, True])
def test_fleet_progress_never_reports_another_job(follow: bool) -> None:
    expected = "33333333-3333-4333-8333-333333333333"
    other = "44444444-4444-4444-8444-444444444444"
    path = f"/api/jobs/{expected}"
    wrong = {"id": other, "state": "succeeded"}
    responses = [{"id": expected, "state": "running"}, wrong] if follow else wrong
    client = FakeClient({("GET", path): responses})
    arguments = ["fleet", "progress", expected, "--json"]
    if follow:
        arguments += ["--follow", "--interval-seconds", "0.01"]

    status, payload = run(tuple(arguments), client)

    assert status == 2
    assert "another job" in str(payload).lower()
    assert [call[1] for call in client.calls] == [path] * (2 if follow else 1)
