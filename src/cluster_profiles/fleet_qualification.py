"""Smoke adapters that exercise one qualified workload through the Controller.

An artifact-job recipe runs every reviewed fixture case through the durable
artifact-job lifecycle; an OpenAI-service recipe runs its reviewed HTTP cases
against the published endpoint. Both return a small result summary.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .cli_artifact_jobs import _create_with_reconcile, _job
from .cli_states import (
    CANCELLED,
    DRAFT,
    ENDED_STATES,
    FAILED,
    OBSERVING,
    READY,
    SUCCEEDED,
    preparation,
)
from .control_client import (
    ControlClientError,
)
from .fleet_qualification_observation import (
    QualificationError,
    QualificationObservationUnknown,
    QualificationQualityFailure,
    _BudgetClient,
    _durable_deadline,
    _durable_key,
    _forget_key,
    _observe,
)
from .qualification_fixtures import (
    FixtureError,
    FixtureRegistry,
    RecipeFixture,
    validate_outputs,
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()


def _strict_json_loads(content: bytes | str) -> object:
    def reject_constant(value: str) -> object:
        raise ValueError(f"non-finite JSON number: {value}")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(
        content,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise QualificationObservationUnknown(f"{label} must be an object")
    return value


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class ArtifactJobSmokeAdapter:
    """Run every digest-bound fixture case through the artifact-job lifecycle."""

    def __init__(
        self,
        fixtures: FixtureRegistry,
        *,
        request_directory: Path | None = None,
        request_scope: str = "",
    ) -> None:
        self.fixtures = fixtures
        self.request_directory = request_directory
        self.request_scope = request_scope

    def run(
        self,
        client: Any,
        run_id: str,
        *,
        recipe_key: str,
        recipe_content_sha256: str,
        interface: str,
        timeout_seconds: float,
        poll_interval_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> dict[str, object]:
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise QualificationError(
                "qualification budgets must be finite and positive"
            )
        fixture_deadline = clock() + timeout_seconds

        def resolve():
            recipe, _ = self.fixtures.resolve(
                recipe_key, recipe_content_sha256, interface
            )
            if recipe is None:
                raise QualificationObservationUnknown(
                    "qualification fixture is not yet observed"
                )
            return recipe

        recipe = _observe(
            resolve, fixture_deadline, clock, sleeper, poll_interval_seconds
        )
        results = []
        for case in recipe.all_cases:
            try:
                result = self._run_case(
                    client,
                    run_id,
                    case,
                    timeout_seconds=max(0, fixture_deadline - clock()),
                    poll_interval_seconds=poll_interval_seconds,
                    clock=clock,
                    sleeper=sleeper,
                )
            except (
                QualificationError,
                FixtureError,
                ControlClientError,
                OSError,
            ) as error:
                result = {
                    "case_id": case.case_id,
                    "state": OBSERVING,
                    "detail": str(error),
                }
            results.append(result)
        return {"cases": results}

    def _run_case(
        self,
        client: Any,
        run_id: str,
        recipe: RecipeFixture,
        *,
        timeout_seconds: float,
        poll_interval_seconds: float,
        clock: Callable[[], float],
        sleeper: Callable[[float], None],
    ) -> dict[str, object]:
        deadline = clock() + min(timeout_seconds, recipe.timeout_seconds + 300)
        bounded = _BudgetClient(client, deadline, clock)
        scope = f"{self.request_scope}/{run_id}/{recipe.key}/{recipe.case_id}"
        key = _observe(
            lambda: _durable_key(self.request_directory, scope),
            deadline,
            clock,
            sleeper,
            poll_interval_seconds,
        )
        try:
            deadline = _observe(
                lambda: _durable_deadline(
                    self.request_directory,
                    scope,
                    key,
                    min(timeout_seconds, recipe.timeout_seconds + 300),
                    clock,
                ),
                deadline,
                clock,
                sleeper,
                poll_interval_seconds,
            )
            expired = deadline <= clock()
            if expired:
                deadline = clock() + min(30, timeout_seconds)
            bounded = _BudgetClient(client, deadline, clock)
            args = argparse.Namespace(global_json=True, json=True)
            body = {
                "interface": recipe.interface,
                "parameters": recipe.parameters,
                "inputs": [
                    fixture.declaration(slot) for slot, fixture in recipe.inputs
                ],
                "output_limits": recipe.output_limits,
                "timeout_seconds": recipe.timeout_seconds,
            }
            created = _observe(
                (
                    lambda: bounded.request(
                        "GET", f"/api/artifact-jobs/requests/{_quote(key)}"
                    )
                )
                if expired
                else lambda: _create_with_reconcile(
                    args,
                    bounded,
                    f"/api/recipe/runs/{_quote(run_id)}/artifact-jobs",
                    body,
                    key,
                    _quote,
                    expected_run=run_id,
                ),
                deadline,
                clock,
                sleeper,
                poll_interval_seconds,
            )
            job_id = created.get("id")
            if not isinstance(job_id, str):
                raise QualificationObservationUnknown(
                    "artifact acceptance identity is not yet observed"
                )
            job_path = f"/api/artifact-jobs/{_quote(job_id)}"
            status = created

            def observe_job():
                return _job(
                    bounded.request("GET", job_path),
                    expected_id=job_id,
                    expected_run=run_id,
                )

            if not expired and preparation(status) == DRAFT:
                with recipe.materialize() as inputs:
                    for declaration, source in inputs:
                        _observe(
                            lambda declaration=declaration, source=source: (
                                bounded.upload_file(
                                    f"{job_path}/inputs/{_quote(str(declaration['name']))}",
                                    source,
                                    media_type=str(declaration["media_type"]),
                                    expected_sha256=str(declaration["sha256"]),
                                    expected_size=declaration["size_bytes"],
                                )
                            ),
                            deadline,
                            clock,
                            sleeper,
                            poll_interval_seconds,
                        )
                status = _observe(
                    lambda: bounded.request("POST", f"{job_path}/finalize"),
                    deadline,
                    clock,
                    sleeper,
                    poll_interval_seconds,
                )
            if not expired and preparation(status) == READY:
                # The owner reconciles this stable submit identity on exact replay.
                status = _observe(
                    lambda: bounded.request(
                        "POST",
                        f"{job_path}/submit",
                        extra_headers={"X-Request-ID": key},
                    ),
                    deadline,
                    clock,
                    sleeper,
                    poll_interval_seconds,
                )
            while status.get("state") not in ENDED_STATES and clock() < deadline:
                sleeper(min(poll_interval_seconds, max(0, deadline - clock())))
                try:
                    status = _observe(
                        observe_job,
                        deadline,
                        clock,
                        sleeper,
                        poll_interval_seconds,
                    )
                except QualificationError:
                    break
            if status.get("state") not in ENDED_STATES:
                # Cancellation is an intent, not evidence of cleanup. Reuse its
                # request identity and observe the same job through bounded settlement.
                cleanup_deadline = clock() + min(30, timeout_seconds)
                cleanup = _BudgetClient(client, cleanup_deadline, clock)
                cancel_key = str(uuid.uuid5(uuid.UUID(key), CANCELLED))
                try:

                    def cancel():
                        value = cleanup.request(
                            "POST",
                            f"{job_path}/cancel",
                            {"reason": "qualification observation deadline"},
                            extra_headers={"X-Request-ID": cancel_key},
                        )
                        value = _job(value, expected_id=job_id, expected_run=run_id)
                        evidence = value.get("result_evidence")
                        if (
                            not isinstance(evidence, Mapping)
                            or evidence.get("cancel_request_id") != cancel_key
                        ):
                            raise QualificationObservationUnknown(
                                "cancellation acceptance identity is not yet observed"
                            )
                        return value

                    _observe(
                        cancel, cleanup_deadline, clock, sleeper, poll_interval_seconds
                    )
                    while clock() < cleanup_deadline:
                        status = _observe(
                            lambda: _job(
                                cleanup.request("GET", job_path),
                                expected_id=job_id,
                                expected_run=run_id,
                            ),
                            cleanup_deadline,
                            clock,
                            sleeper,
                            poll_interval_seconds,
                        )
                        if (
                            status.get("id") == job_id
                            and status.get("state") in ENDED_STATES
                        ):
                            break
                        sleeper(
                            min(
                                poll_interval_seconds,
                                max(0, cleanup_deadline - clock()),
                            )
                        )
                except QualificationError:
                    pass
                terminal = (
                    status.get("id") == job_id and status.get("state") in ENDED_STATES
                )
                evidence = status.get("result_evidence")
                confirmed = (
                    terminal
                    and isinstance(evidence, Mapping)
                    and (
                        evidence.get("cancel_request_id") == cancel_key
                        and evidence.get("failure_kind") is None
                        and evidence.get("active_scope_may_remain") is not True
                    )
                )
                return {
                    "case_id": recipe.case_id,
                    "job_id": job_id,
                    "state": OBSERVING,
                    "cleanup_confirmed": confirmed,
                }
            if status.get("state") != SUCCEEDED:
                return {"case_id": recipe.case_id, "job_id": job_id, "state": FAILED}
            try:

                def outputs():
                    observed = observe_job()
                    if observed.get("id") != job_id:
                        raise QualificationObservationUnknown(
                            "artifact output projection identifies another job"
                        )
                    return validate_outputs(
                        recipe,
                        observed,
                        bounded,
                        timeout_seconds=max(0.001, deadline - clock()),
                    )

                assertions = _observe(
                    outputs, deadline, clock, sleeper, poll_interval_seconds
                )
            except FixtureError as error:
                return {
                    "case_id": recipe.case_id,
                    "job_id": job_id,
                    "state": FAILED,
                    "detail": str(error),
                }
            return {
                "case_id": recipe.case_id,
                "job_id": job_id,
                "state": SUCCEEDED,
                **assertions,
            }

        finally:
            _forget_key(self.request_directory, scope, key)


def _service_path(value: object, path: str) -> object:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                raise QualificationObservationUnknown(
                    f"service assertion path is missing: {path}"
                )
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                raise QualificationObservationUnknown(
                    f"service assertion path is missing: {path}"
                )
            current = current[index]
        else:
            raise QualificationObservationUnknown(
                f"service assertion path is missing: {path}"
            )
    return current


def _assert_service_response(
    response: Mapping[str, object], raw: bytes, assertions: list[object]
) -> None:
    raw_text = raw.decode("utf-8")
    for raw_assertion in assertions:
        assertion = _object(raw_assertion, "service assertion")
        kind = assertion.get("kind")
        if kind == "raw.not-contains":
            values = assertion.get("values")
            if not isinstance(values, list) or any(
                isinstance(item, str) and item in raw_text for item in values
            ):
                raise QualificationQualityFailure("service raw-token assertion failed")
            continue
        path = assertion.get("path")
        if not isinstance(path, str):
            raise QualificationObservationUnknown(
                "service assertion path is unavailable"
            )
        value = _service_path(response, path)
        expected = assertion.get("value")
        failed = False
        if kind == "path.equals":
            failed = value != expected
        elif kind == "path.regex":
            failed = (
                not isinstance(value, str) or re.fullmatch(str(expected), value) is None
            )
        elif kind == "path.nonempty":
            failed = not isinstance(value, (str, list, dict)) or len(value) == 0
        elif kind == "path.empty":
            failed = value not in (None, "", [], {})
        elif kind == "path.count":
            failed = not isinstance(value, (str, list, dict)) or len(value) != expected
        elif kind == "path.lte":
            failed = (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isinstance(expected, (int, float))
                or value > expected
            )
        elif kind == "path.json-equals":
            try:
                decoded = _strict_json_loads(value) if isinstance(value, str) else None
            except (json.JSONDecodeError, ValueError):
                decoded = None
            failed = decoded != expected
        elif kind == "array.path-count-equals":
            item_path = assertion.get("item_path")
            if not isinstance(value, list) or not isinstance(item_path, str):
                failed = True
            else:
                count = 0
                for item in value:
                    try:
                        if _service_path(item, item_path) == expected:
                            count += 1
                    except QualificationError:
                        continue
                failed = count != assertion.get("count")
        else:
            raise QualificationObservationUnknown(
                f"service assertion is unavailable: {kind}"
            )
        if failed:
            raise QualificationQualityFailure(
                f"service assertion failed: {kind} at {path}"
            )


class ServiceSmokeAdapter:
    """Run digest-bound, capability-aware OpenAI service acceptance cases."""

    def __init__(
        self,
        fixtures: FixtureRegistry,
        *,
        timeout_seconds: float = 180,
        opener: Any = None,
    ):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise QualificationError(
                "service observation budget must be finite and positive"
            )
        self.fixtures = fixtures
        self.timeout_seconds = timeout_seconds
        from .control_transport import open_https

        self.opener = opener or open_https

    def run(
        self,
        client: Any,
        number: int,
        alias: str,
        *,
        recipe_key: str,
        recipe_content_sha256: str,
    ) -> dict[str, object]:
        fixture_deadline = time.monotonic() + self.timeout_seconds

        def resolve():
            recipe, _ = self.fixtures.resolve_service(recipe_key, recipe_content_sha256)
            if recipe is None:
                raise QualificationObservationUnknown(
                    "service fixture is not yet observed"
                )
            return recipe

        recipe = _observe(resolve, fixture_deadline, time.monotonic, time.sleep, 0.5)
        deadline = fixture_deadline
        bounded = _BudgetClient(client, deadline, time.monotonic)

        def endpoint():
            view = bounded.request(
                "GET", f"/api/profile/{number}/endpoints", query={"alias": alias}
            )
            for item in view.get("assignments") or []:
                if item.get("alias") != alias or not item.get("endpoint"):
                    continue
                base = item["endpoint"].get("api_base")
                if not isinstance(base, str):
                    continue
                parsed = urllib.parse.urlsplit(base)
                if (
                    parsed.scheme == "https"
                    and parsed.hostname
                    and not parsed.username
                    and not parsed.password
                    and not parsed.fragment
                ):
                    return base
            raise QualificationObservationUnknown(
                "published endpoint is not yet observed"
            )

        base = _observe(endpoint, deadline, time.monotonic, time.sleep, 0.5)
        results = []
        for declared in recipe.cases:
            try:
                case = _observe(
                    lambda declared=declared: declared.render(
                        alias, self.fixtures.fixtures
                    ),
                    min(
                        deadline,
                        time.monotonic() + declared.timeout_seconds,
                    ),
                    time.monotonic,
                    time.sleep,
                    0.5,
                )
                result = self._run_service_case(base, case, until=deadline)
            except (
                FixtureError,
                QualificationError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
            ) as error:
                result = {
                    "case_id": declared.case_id,
                    "state": OBSERVING,
                    "detail": str(error),
                }
            results.append(result)
        return {"endpoint_alias": alias, "cases": results}

    def _run_service_case(self, base, case, *, until: float | None = None):
        method, path = case["method"], case["path"]
        body = _canonical(case.get("body")) if method == "POST" else None
        request = urllib.request.Request(
            base.rstrip("/") + path,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
        )
        limit = case["max_response_bytes"]
        budget = min(float(case["timeout_seconds"]), self.timeout_seconds)
        started = time.monotonic()
        deadline = (
            min(started + budget, until) if until is not None else started + budget
        )

        def observe():
            try:
                with self.opener(
                    request, timeout=max(0.001, deadline - time.monotonic())
                ) as response:
                    status = int(getattr(response, "status", 200))
                    raw = response.read(limit + 1)
            except urllib.error.HTTPError as error:
                error.close()
                if error.code in (401, 403):
                    raise QualificationError(
                        "service endpoint denied authentication or authorization"
                    ) from error
                raise QualificationObservationUnknown(
                    "service response is unavailable"
                ) from error
            if status in (401, 403):
                raise QualificationError(
                    "service endpoint denied authentication or authorization"
                )
            if status != 200 or len(raw) > limit:
                raise QualificationObservationUnknown(
                    "service response is not yet available within its byte contract"
                )
            response_object = _strict_json_loads(raw)
            if not isinstance(response_object, Mapping):
                raise QualificationObservationUnknown(
                    "service response projection is malformed"
                )
            _assert_service_response(response_object, raw, case["assertions"])
            return {
                "case_id": case["id"],
                "state": SUCCEEDED,
                "method": method,
                "path": path,
                "http_status": status,
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
                "response_bytes": len(raw),
            }

        try:
            return _observe(observe, deadline, time.monotonic, time.sleep, 0.5)
        except QualificationQualityFailure as error:
            return {"case_id": case["id"], "state": FAILED, "detail": str(error)}
