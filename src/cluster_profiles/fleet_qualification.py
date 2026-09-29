"""Smoke adapters that exercise one qualified workload through the Controller.

An artifact-job recipe runs every reviewed fixture case through the durable
artifact-job lifecycle; an OpenAI-service recipe runs its reviewed HTTP cases
against the published endpoint. Both return a small result summary.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .qualification_fixtures import (
    FixtureError,
    FixtureRegistry,
    RecipeFixture,
    validate_outputs,
)


class QualificationError(RuntimeError):
    """A qualification step failed."""


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
        raise QualificationError(f"{label} must be an object")
    return value


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class ArtifactJobSmokeAdapter:
    """Run every digest-bound fixture case through the artifact-job lifecycle."""

    def __init__(self, fixtures: FixtureRegistry) -> None:
        self.fixtures = fixtures

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
        recipe, blocker = self.fixtures.resolve(
            recipe_key, recipe_content_sha256, interface
        )
        if recipe is None:
            raise QualificationError(
                str(blocker["detail"] if blocker else "fixture unavailable")
            )
        return {
            "cases": [
                self._run_case(
                    client,
                    run_id,
                    case,
                    timeout_seconds=timeout_seconds,
                    poll_interval_seconds=poll_interval_seconds,
                    clock=clock,
                    sleeper=sleeper,
                )
                for case in recipe.all_cases
            ]
        }

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
        created = client.request(
            "POST",
            f"/api/recipe/runs/{_quote(run_id)}/artifact-jobs",
            {
                "interface": recipe.interface,
                "parameters": recipe.parameters,
                "inputs": [
                    fixture.declaration(slot) for slot, fixture in recipe.inputs
                ],
                "output_limits": recipe.output_limits,
                "timeout_seconds": recipe.timeout_seconds,
            },
            extra_headers={"X-Request-ID": str(uuid.uuid4())},
        )
        job_id = created.get("id")
        if not isinstance(job_id, str):
            raise QualificationError("controller returned an invalid artifact job ID")
        job_path = f"/api/artifact-jobs/{_quote(job_id)}"
        with recipe.materialize() as inputs:
            for declaration, source in inputs:
                size = declaration["size_bytes"]
                assert isinstance(size, int)
                client.upload_file(
                    f"{job_path}/inputs/{_quote(str(declaration['name']))}",
                    source,
                    media_type=str(declaration["media_type"]),
                    expected_sha256=str(declaration["sha256"]),
                    expected_size=size,
                )
        status = client.request("POST", f"{job_path}/finalize")
        if status.get("state") == "ready":
            status = client.request("POST", f"{job_path}/submit")
        deadline = clock() + min(timeout_seconds, recipe.timeout_seconds + 300)
        while status.get("state") not in {"succeeded", "failed", "cancelled"}:
            if clock() >= deadline:
                client.request(
                    "POST",
                    f"{job_path}/cancel",
                    {"reason": "qualification smoke timed out"},
                )
                raise QualificationError(
                    "artifact-job smoke timed out and was cancelled"
                )
            sleeper(poll_interval_seconds)
            status = client.request("GET", job_path)
        if status.get("state") != "succeeded":
            raise QualificationError(
                f"artifact-job smoke entered terminal state {status.get('state')}"
            )
        try:
            assertions = validate_outputs(recipe, status, client)
        except FixtureError as error:
            raise QualificationError(str(error)) from error
        return {"case_id": recipe.case_id, "job_id": job_id, **assertions}


def _service_path(value: object, path: str) -> object:
    current = value
    for part in path.split("."):
        if isinstance(current, Mapping):
            if part not in current:
                raise QualificationError(f"service assertion path is missing: {path}")
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index >= len(current):
                raise QualificationError(f"service assertion path is missing: {path}")
            current = current[index]
        else:
            raise QualificationError(f"service assertion path is missing: {path}")
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
                raise QualificationError("service raw-token assertion failed")
            continue
        path = assertion.get("path")
        if not isinstance(path, str):
            raise QualificationError("service assertion path is invalid")
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
            raise QualificationError(f"unsupported service assertion: {kind}")
        if failed:
            raise QualificationError(f"service assertion failed: {kind} at {path}")


class ServiceSmokeAdapter:
    """Run digest-bound, capability-aware OpenAI service acceptance cases."""

    def __init__(
        self,
        fixtures: FixtureRegistry,
        *,
        timeout_seconds: float = 180,
        opener: Any = urllib.request.urlopen,
    ):
        self.fixtures = fixtures
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    def run(
        self,
        client: Any,
        alias: str,
        *,
        recipe_key: str,
        recipe_content_sha256: str,
    ) -> dict[str, object]:
        recipe, blocker = self.fixtures.resolve_service(
            recipe_key, recipe_content_sha256
        )
        if recipe is None:
            raise QualificationError(
                str(blocker["detail"] if blocker else "service fixture unavailable")
            )
        cases = [case.render(alias, self.fixtures.fixtures) for case in recipe.cases]
        endpoint = client.request("GET", f"/api/endpoints/{_quote(alias)}")
        base = endpoint.get("api_base")
        if not isinstance(base, str):
            raise QualificationError("published endpoint API base is invalid")
        parsed = urllib.parse.urlsplit(base)
        if (
            parsed.scheme != "https"
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise QualificationError(
                "published endpoint API base must be credential-free HTTPS"
            )
        if not cases:
            raise QualificationError("service smoke has no reviewed cases")
        results: list[dict[str, object]] = []
        for case in cases:
            method = str(case.get("method"))
            path = str(case.get("path"))
            body_value = case.get("body")
            body = _canonical(body_value) if method == "POST" else None
            request = urllib.request.Request(
                base.rstrip("/") + path,
                data=body,
                method=method,
                headers={
                    "Accept": "application/json",
                    **(
                        {"Content-Type": "application/json"} if body is not None else {}
                    ),
                },
            )
            limit = case.get("max_response_bytes")
            timeout = case.get("timeout_seconds")
            if not isinstance(limit, int) or not isinstance(timeout, int):
                raise QualificationError("service smoke case bounds are invalid")
            started = time.monotonic()
            try:
                with self.opener(
                    request, timeout=min(float(timeout), self.timeout_seconds)
                ) as response:
                    status = int(getattr(response, "status", 200))
                    raw = response.read(limit + 1)
            except (OSError, urllib.error.URLError) as error:
                raise QualificationError(
                    f"service smoke {case.get('id')} request failed: {type(error).__name__}"
                ) from None
            latency_ms = round((time.monotonic() - started) * 1000, 3)
            if status != 200:
                raise QualificationError(
                    f"service smoke {case.get('id')} returned HTTP {status}"
                )
            if len(raw) > limit:
                raise QualificationError(
                    f"service smoke {case.get('id')} response exceeds its bound"
                )
            try:
                value = _strict_json_loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                raise QualificationError(
                    f"service smoke {case.get('id')} response is invalid JSON"
                ) from error
            response_object = _object(value, "service smoke response")
            assertions = case.get("assertions")
            if not isinstance(assertions, list):
                raise QualificationError("service smoke assertions are invalid")
            _assert_service_response(response_object, raw, assertions)
            results.append(
                {
                    "case_id": case.get("id"),
                    "method": method,
                    "path": path,
                    "http_status": status,
                    "latency_ms": latency_ms,
                    "response_bytes": len(raw),
                }
            )
        return {"endpoint_alias": alias, "cases": results}
