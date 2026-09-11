from __future__ import annotations

from collections.abc import Mapping, Sequence

from starlette.routing import Route
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import MUTATION_ROLES, TokenCodec


def test_every_mutating_route_has_explicit_role() -> None:
    class Jobs:
        def enqueue(
            self,
            kind: str,
            actor: str,
            authority_revision: str,
            targets: Sequence[str],
            payload: Mapping[str, object],
            *,
            request_id: str,
        ) -> object:
            raise AssertionError

        def get(self, job_id: str) -> object:
            raise KeyError

        def list(self, *, limit: int = 100) -> list[object]:
            return []

        def list_page(
            self,
            *,
            limit: int = 100,
            cursor: str | None = None,
            status: str | None = None,
            target: str | None = None,
        ) -> tuple[list[object], str | None, int]:
            raise AssertionError

    app = create_app(jobs=Jobs(), tokens=TokenCodec(b"k" * 32), audits=MemoryAuditStore())
    routes = {
        (method, route.path)
        for route in app.routes
        if isinstance(route, Route)
        for method in getattr(route, "methods", set())
        if method in {"POST", "PUT", "PATCH", "DELETE"} and route.path.startswith("/api/")
    }
    assert routes == set(MUTATION_ROLES)
    assert all(roles and roles <= {"viewer", "operator", "administrator"} for roles in MUTATION_ROLES.values())


def test_viewer_has_no_mutating_permission() -> None:
    assert all("viewer" not in roles for roles in MUTATION_ROLES.values())
