from __future__ import annotations

import json
import re
import uuid
from contextlib import redirect_stdout
from io import StringIO

from cluster_profiles import cli
from cluster_profiles.control_client import (
    ControlNotFound,
    _operation,
    validate_control_document,
)
from cluster_profiles.generated_control.models.fleet_profile_endpoints_view import (
    FleetProfileEndpointsView,
)


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
        retry=True,
    ):
        self._validate_request(method, path, payload, query)
        if path == "/api/library/vocabulary" and (method, path) not in self.responses:
            # The release words are informative and fetched aside by the
            # library commands; a release without them answers 404.
            raise ControlNotFound(404, "the synced release publishes no vocabulary")
        self.calls.append((method, path, payload, query))
        response = self.responses.get((method, path), {})
        if method == "POST" and path.endswith("/preview") and not response:
            response = {"allowed": True, "plan_digest": "c" * 64}
        if isinstance(response, list):
            # The final entry is sticky so a caller can describe a dependency
            # that keeps answering the same way, including one that stays
            # unavailable for the whole bounded observation.
            response = response.pop(0) if len(response) > 1 else response[0]
        if isinstance(response, BaseException):
            raise response
        return response

    def metrics_catalogue(self):
        raise AssertionError("the metric catalogue is not read here")

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
            r"/api/profile/(\d+)(?:/(definition|preview|load|delete|progress|endpoints))?",
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
            r"/api/(model|recipe)/(operations|requests)/[^/]+(?:/(?:cancel|resume-access))?",
            path,
        )
        model_cancel_path = re.fullmatch(
            r"/api/model/operations/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/cancel",
            path,
        )
        installation_reconcile_path = re.fullmatch(
            r"/api/recipe/installations/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}/reconcile(?:/preview)?",
            path,
        )
        run_switch_operation_path = re.fullmatch(
            r"/api/run-switch/operations/[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            path,
        )
        known_get = {
            "/api/fleet",
            "/api/model/library",
            "/api/recipe/library",
            "/api/profile",
            "/api/operations",
        }
        job_path = re.fullmatch(r"/api/jobs/[^/]+", path)
        resume_path = re.fullmatch(r"/api/jobs/[^/]+/resume", path)
        job_request_path = re.fullmatch(r"/api/jobs/[^/]+/requests/[^/]+", path)
        known = (
            path in known_get
            or job_path is not None
            or resume_path is not None
            or job_request_path is not None
            or profile_path is not None
            or application_path is not None
            or application_cancel_path is not None
            or application_cancel_receipt_path is not None
            or request_path is not None
            or selector_path is not None
            or operation_path is not None
            or model_cancel_path is not None
            or installation_reconcile_path is not None
            or run_switch_operation_path is not None
        )
        known = (
            known or re.fullmatch(r"/api/fleet/[^/]+(?:/loginfo)?", path) is not None
        )
        known = known or path in {
            "/api/fleet/enroll",
            "/api/fleet/upgrade",
            "/api/recipe/update",
            "/api/recipe/operations/op-cancel/cancel",
            "/api/library/vocabulary",
        }
        known = known or re.fullmatch(r"/api/jobs/[^/]+", path) is not None
        known = (
            known
            or re.fullmatch(r"/api/operations/[^/]+(?:/evidence)?", path) is not None
        )
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
            if path.endswith("/library") or path == "/api/profile":
                parameters = _operation(path, method)["parameters"]
                assert isinstance(parameters, list)
                assert query is None or set(query) <= {
                    item["name"] for item in parameters if item["in"] == "query"
                }
            elif path.startswith("/api/fleet/") and not path.endswith("/loginfo"):
                assert query is None or set(query) <= {"technical"}
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
                    assert query in (None, {"fleet": True})
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
            elif path.endswith("/evidence"):
                assert query == {"attempt": 2}
            elif run_switch_operation_path is not None:
                assert query is None
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
            if payload is not None:
                validate_control_document("FleetProfilePreviewRequest", payload)
            assert installation_reconcile_path is not None or profile_path is not None
        elif path.endswith("/delete"):
            assert profile_path is not None
            validate_control_document("FleetProfileDeleteRequest", payload)
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
        elif installation_reconcile_path is not None and path.endswith("/reconcile"):
            assert isinstance(payload, dict) and set(payload) == {"request_key"}
            uuid.UUID(payload["request_key"])
        elif selector_path is not None and selector_path.group(2) in {
            "download",
            "remove",
        }:
            assert isinstance(payload, dict)
            if selector_path.group(1) == "model" and selector_path.group(2) == "remove":
                assert set(payload) in (
                    {"request_key"},
                    {"request_key", "fleet", "review_digest"},
                )
                uuid.UUID(payload["request_key"])
            else:
                assert set(payload) <= {"request_key", "with_model"}
                uuid.UUID(payload["request_key"])
                if selector_path.group(2) == "remove":
                    assert set(payload) == {
                        "request_key",
                        "with_model",
                    }
            if selector_path.group(1) == "model":
                assert "with_model" not in payload
            if selector_path.group(2) == "download":
                assert "yes" not in payload and "with_model" not in payload
        elif path == "/api/recipe/update":
            assert isinstance(payload, dict)
            assert set(payload) == {"request_key", "selectors", "all"}
            uuid.UUID(payload["request_key"])
            assert isinstance(payload["selectors"], list)
            assert isinstance(payload["all"], bool)
        elif path.endswith("/resume-access"):
            validate_control_document("ModelCacheOperatorRequest", payload)
        elif model_cancel_path is not None:
            validate_control_document("ModelCacheCancellationRequest", payload)
        elif re.fullmatch(r"/api/recipe/operations/[^/]+/cancel", path):
            assert isinstance(payload, dict)
            validate_control_document("RecipeCancellationRequest", payload)
        elif path == "/api/fleet/upgrade":
            assert (
                {"all", "request_key"}
                <= set(payload)
                <= {
                    "selectors",
                    "all",
                    "request_key",
                }
            )
            uuid.UUID(payload["request_key"])
            if "selectors" in payload:
                assert isinstance(payload["selectors"], list) and payload["selectors"]
        elif path == "/api/fleet/enroll":
            assert set(payload) == {"name"}
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
    document = json.loads(output.getvalue())
    # The client-contract stamp is proven by test_vonkctl_json_contract; these
    # tests compare the Controller's content.
    document.pop("schema_version", None)
    if "result_kind" in document:
        document.pop("result_kind")
        document["result"].pop("schema_version", None)
    return status, document


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


def _accepted_profile_load(identity: str, state: str) -> dict[str, object]:
    return {
        "id": identity,
        "state": state,
        "request_key": "11111111-1111-4111-8111-111111111111",
        "progress": {"intended_profile": {"reviewed_plan_digest": "c" * 64}},
    }


_RUN_KEY = "11111111-1111-4111-8111-111111111111"


_RUN_DIGEST = "e" * 64


def _run_preview(plan_digest: str, effects_digest: str) -> dict[str, object]:
    return {
        "allowed": True,
        "profile_name": "Default",
        "plan_digest": plan_digest,
        "effects_digest": effects_digest,
        "scope": {"node_ids": [], "idle_node_ids": []},
        "summary": {},
        "assignments": [],
        "steps": [],
        "preparations": [],
        "preparation_decisions": [],
        "assessments": [],
        "admission_decisions": [],
        "effects": {"runs": [], "installations": [], "superseded": []},
        "reasons": [],
    }


class _Prompt:
    """A terminal that answers the confirmation questions it is asked."""

    def __init__(self, monkeypatch, *answers: str) -> None:
        import sys

        class Stderr(StringIO):
            def isatty(self) -> bool:
                return True

        class Stdin:
            def __init__(self, replies: list[str]) -> None:
                self.replies = replies

            def isatty(self) -> bool:
                return True

            def readline(self, _limit: int = -1) -> str:
                return self.replies.pop(0) + "\n" if self.replies else ""

        self.stderr = Stderr()
        monkeypatch.setattr(sys, "stdin", Stdin(list(answers)))
        monkeypatch.setattr(sys, "stderr", self.stderr)

    @property
    def transcript(self) -> str:
        return self.stderr.getvalue()


class _InjectedClock:
    """A clock the test owns: sleeping advances it, nothing waits in real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.now += max(delay, 0.01)
