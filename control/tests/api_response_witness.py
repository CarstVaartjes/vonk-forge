"""Opt-in test observer of bytes emitted by production ASGI endpoints."""

from __future__ import annotations

import json
from contextvars import ContextVar
from pathlib import Path
from typing import Any
from weakref import WeakKeyDictionary

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from jsonschema import Draft202012Validator, FormatChecker
from starlette.types import Receive, Scope, Send
from vonk_control.contract_graph import discover_contracts, schema_application

_active_trace: ContextVar[dict | None] = ContextVar(
    "api_response_witness_trace", default=None
)


class WitnessViolation(ValueError):
    pass


class ResponseWitnesses:
    def __init__(self) -> None:
        self.test_id = ""
        self.recording = True
        self.successes: dict[tuple[str, str, int, str], dict[str, Any]] = {}
        self.failures: list[dict[str, Any]] = []
        self.validators: WeakKeyDictionary = WeakKeyDictionary()
        self.pending: list[tuple] = []

    def validate(
        self,
        app: FastAPI,
        scope: dict,
        status: int,
        headers: list,
        body: bytes,
        *,
        body_size: int | None = None,
    ) -> None:
        route = scope.get("route")
        if (
            not isinstance(route, APIRoute)
            or not any(route is candidate for candidate in app.routes)
            or not route.endpoint.__module__.startswith("vonk_control.")
        ):
            return
        if not 200 <= status < 300:
            return
        method, path = scope["method"], scope.get("root_path", "") + route.path_format
        body_size = len(body) if body_size is None else body_size
        media = next(
            (
                v.decode().split(";", 1)[0].strip().lower()
                for k, v in headers
                if k.lower() == b"content-type"
            ),
            "",
        )
        evidence = {
            "method": method,
            "path": path,
            "status": status,
            "media_type": media,
            "test": self.test_id,
        }
        try:
            operation = app.openapi()["paths"][route.path_format][method.lower()]
            response = operation["responses"][str(status)]
            if status in {204, 205}:
                if body_size:
                    raise WitnessViolation("no_content_body")
            else:
                content = response.get("content", {})
                selected = (
                    content.get(media)
                    or content.get(media.split("/", 1)[0] + "/*")
                    or content.get("*/*")
                )
                if selected is None:
                    raise WitnessViolation("undeclared_media_type")
                schema = selected["schema"]
                if schema == {"type": "string", "format": "binary"}:
                    pass  # Exact bytes are transport evidence; no JSON reconstruction.
                else:
                    key = (method, path, status, media)
                    validators = self.validators.setdefault(app, {})
                    validator = validators.get(key)
                    if validator is None:
                        validator = Draft202012Validator(
                            {
                                "components": app.openapi().get("components", {}),
                                **schema,
                            },
                            format_checker=FormatChecker(),
                        )
                        validators[key] = validator
                    documents = [body]
                    if media == "text/event-stream":
                        documents = []
                        for frame in body.replace(b"\r\n", b"\n").split(b"\n\n"):
                            data = b"\n".join(
                                line[5:].lstrip(b" ")
                                for line in frame.splitlines()
                                if line.startswith(b"data:")
                            )
                            if data:
                                documents.append(data)
                        if not documents:
                            raise WitnessViolation("no_sse_data_frames")
                    for document in documents:
                        if (
                            media == "application/json"
                            or media.endswith("+json")
                            or media == "text/event-stream"
                        ):
                            decoded = json.loads(document)
                        elif schema.get("type") == "string":
                            decoded = document.decode("utf-8")
                        else:
                            raise WitnessViolation("unsupported_declared_transport")
                        violation = next(validator.iter_errors(decoded), None)
                        if violation is not None:
                            # Do not render the instance or schema values: tests may
                            # exercise credentials and signed authority documents.
                            raise WitnessViolation("schema_" + str(violation.validator))
        except (KeyError, ValueError, TypeError) as error:
            category = (
                str(error)
                if isinstance(error, WitnessViolation)
                else type(error).__name__
            )
            self.failures.append({**evidence, "category": category})
            raise AssertionError(
                f"ASGI contract violation: {method} {path} {status} ({category})"
            ) from None
        key = method, path, status, media
        record = self.successes.setdefault(
            key, {**evidence, "tests": set(), "response_count": 0}
        )
        record.pop("test", None)
        record["tests"].add(self.test_id)
        record["response_count"] += 1

    async def observe(self, original, app, scope, receive, send):
        if scope["type"] != "http" or not self.recording:
            return await original(app, scope, receive, send)
        status, headers, chunks, body_size = None, [], [], 0
        exact_bytes = False
        trace = {"app": app, "route": None}
        token = _active_trace.set(trace)

        async def capture(message):
            nonlocal status, headers, body_size, exact_bytes
            if message["type"] == "http.response.start":
                status, headers = message["status"], message.get("headers", [])
                route = trace["route"]
                if isinstance(route, APIRoute) and any(
                    route is candidate for candidate in app.routes
                ):
                    response = route.responses.get(status, {})
                    exact_bytes = bool(response.get("content")) and all(
                        value.get("schema") == {"type": "string", "format": "binary"}
                        for value in response.get("content", {}).values()
                    )
            elif (
                message["type"] == "http.response.body"
                and status is not None
                and 200 <= status < 300
            ):
                chunk = message.get("body", b"")
                body_size += len(chunk)
                if not exact_bytes:
                    if body_size > 16 * 1024 * 1024:
                        raise AssertionError(
                            "ASGI witness structured body exceeds bounded capture"
                        )
                    chunks.append(chunk)
                if not message.get("more_body", False):
                    route = trace["route"]
                    if isinstance(
                        route, APIRoute
                    ) and route.endpoint.__module__.startswith("vonk_control."):
                        # Validate after the test, outside endpoint deadlines. Generating
                        # the schema must not turn a one-second transport test into a timeout.
                        self.pending.append(
                            (
                                app,
                                {
                                    "method": scope["method"],
                                    "root_path": scope.get("root_path", ""),
                                    "route": route,
                                },
                                status,
                                [
                                    (k, v)
                                    for k, v in headers
                                    if k.lower() == b"content-type"
                                ],
                                b"".join(chunks),
                                body_size,
                            )
                        )
            await send(message)

        try:
            return await original(app, scope, receive, capture)
        finally:
            _active_trace.reset(token)

    def flush(self) -> None:
        pending, self.pending = self.pending, []
        errors = []
        for app, scope, status, headers, body, body_size in pending:
            try:
                self.validate(app, scope, status, headers, body, body_size=body_size)
            except AssertionError as error:
                errors.append(str(error))
        if errors:
            raise AssertionError("\n".join(errors))

    def report(self) -> dict[str, Any]:
        operations, _ = discover_contracts(schema_application(browser_auth=True))
        required = {(op["method"], op["path"]) for op in operations}
        witnessed = {(method, path) for method, path, _status, _media in self.successes}
        rows = [
            {**row, "tests": sorted(row["tests"])}
            for _key, row in sorted(self.successes.items())
        ]
        return {
            "scope": "successful ASGI bytes from production route handlers in selected test applications; fixture/service setup varies; consumer parsing and business-state coverage are not implied",
            "discovered_operation_count": len(required),
            "witnessed_operation_count": len(required & witnessed),
            "missing_operations": [
                {"method": method, "path": path}
                for method, path in sorted(required - witnessed)
            ],
            "outside_discovered_inventory": [
                {"method": method, "path": path}
                for method, path in sorted(witnessed - required)
            ],
            "witnesses": rows,
            "failures": self.failures,
        }


def pytest_addoption(parser):
    parser.addoption(
        "--api-response-witness",
        type=Path,
        help="write successful production response coverage and missing route IDs",
    )


def install_observer(collector):
    original = FastAPI.__call__
    original_handle = APIRoute.handle

    async def handled(self, scope: Scope, receive: Receive, send: Send) -> None:
        trace = _active_trace.get()
        if trace is not None and any(
            self is candidate for candidate in trace["app"].routes
        ):
            trace["route"] = self
        return await original_handle(self, scope, receive, send)

    async def observed(self, scope: Scope, receive: Receive, send: Send) -> None:
        return await collector.observe(original, self, scope, receive, send)

    FastAPI.__call__ = observed
    APIRoute.handle = handled
    return original, original_handle


def pytest_configure(config):
    if config.getoption("api_response_witness"):
        if getattr(config.option, "numprocesses", 0):
            raise pytest.UsageError(
                "response witness collection requires serial pytest; "
                "parallel reports must be collected separately"
            )
        collector = ResponseWitnesses()
        config._api_response_witness = collector, *install_observer(collector)


def pytest_runtest_setup(item):
    state = getattr(item.config, "_api_response_witness", None)
    if state:
        state[0].test_id = item.nodeid
        # Deliberate recorder mutations are not application behavior witnesses.
        state[0].recording = item.path.name != "test_api_response_witness.py"


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item):
    state = getattr(item.config, "_api_response_witness", None)
    if state:
        state[0].flush()


def pytest_sessionfinish(session, exitstatus):
    state = getattr(session.config, "_api_response_witness", None)
    if not state:
        return
    collector, original, original_handle = state
    FastAPI.__call__ = original
    APIRoute.handle = original_handle
    report = {**collector.report(), "pytest_exitstatus": int(exitstatus)}
    session.config._api_response_witness_report = report
    destination = session.config.getoption("api_response_witness")
    destination.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    report = getattr(config, "_api_response_witness_report", None)
    if report is not None:
        terminalreporter.write_line(
            "Production ASGI response witnesses: "
            f"{report['witnessed_operation_count']}/{report['discovered_operation_count']} "
            f"operations; {len(report['missing_operations'])} without witnesses; "
            f"{len(report['failures'])} response violations."
        )
