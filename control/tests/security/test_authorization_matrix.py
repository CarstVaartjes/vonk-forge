from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import MUTATION_ROLES, TokenCodec


def test_every_mutating_route_has_explicit_role() -> None:
    class Jobs:
        def list(self): return []
        def get(self, _): raise KeyError
        def enqueue(self, *_args, **_kwargs): raise AssertionError
    app = create_app(jobs=Jobs(), tokens=TokenCodec(b"k" * 32), audits=MemoryAuditStore())
    routes = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if method in {"POST", "PUT", "PATCH", "DELETE"} and route.path.startswith("/api/v1/")
    }
    assert routes == set(MUTATION_ROLES)
    assert all(roles and roles <= {"viewer", "operator", "administrator"} for roles in MUTATION_ROLES.values())


def test_viewer_has_no_mutating_permission() -> None:
    assert all("viewer" not in roles for roles in MUTATION_ROLES.values())
