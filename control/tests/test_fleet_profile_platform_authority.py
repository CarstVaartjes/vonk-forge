"""Current user authority is consulted only at request acceptance boundaries."""

import ast
from pathlib import Path
from uuid import uuid4

import pytest

from .test_recipe_update_batches import update_env

__all__ = ["update_env"]


@pytest.mark.parametrize(
    ("module", "request_boundaries"),
    [
        (
            "fleet_profiles",
            {
                "_authorize",
                "_admission_session",
                "_load_replay",
                "_create_pending_application",
                "retry",
                "application_cancellation_by_request",
                "application_by_request_key",
                "cancel",
            },
        ),
        ("recipe_update_batches", {"start", "cancel"}),
        ("recipe_image_availability", {"_request_cancellation", "cancel"}),
    ],
)
def test_maintenance_cannot_add_an_original_author_permission_gate(
    module: str, request_boundaries: set[str]
) -> None:
    # Catches a new requester-permission gate in a worker, adapter or projection.
    # Only the named request boundaries may consult current user authority;
    # behavioral tests cover their accepted-intent branches.
    source = Path(__file__).parents[1] / "src" / "vonk_control" / f"{module}.py"
    tree = ast.parse(source.read_text())
    violations = []
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name in request_boundaries:
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"_authorize", "_user_has_profile_authority"}
            ):
                violations.append((function.name, node.lineno))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_request_cancellation"
                and any(
                    keyword.arg == "authorize"
                    and not (
                        isinstance(keyword.value, ast.Constant)
                        and keyword.value.value is False
                    )
                    for keyword in node.keywords
                )
            ):
                violations.append((function.name, node.lineno))
    assert not violations, f"Maintenance depends on the original author: {violations}"


@pytest.mark.parametrize("maintenance", ["batch-child", "profile-preparation-cancel"])
def test_accepted_preparation_survives_author_removal(update_env, maintenance):
    from sqlalchemy import select
    from vonk_control.models import Job, User

    from .test_recipe_update_batches import _start

    # Use the real preparation producer/store/worker seam. The wrong
    # implementation reauthorizes an accepted child or its profile cleanup.
    sessions, recipes, _now, fresh = update_env
    service = fresh()
    if maintenance == "batch-child":
        parent = _start(service, list(recipes))
        service.run_update_claim(service.claim_update(owner="worker"))
        issued = service.get_operator_operation(parent.id).children[0].operation_id
    else:
        revision_id = next(iter(recipes))
        service.ensure_preparation(revision_id, actor="operator")
        with sessions() as session:
            operation = session.scalar(select(Job))
            assert operation is not None
            issued = operation.id
    with sessions.begin() as session:
        author = session.scalar(select(User).where(User.subject == "operator"))
        assert author is not None
        session.delete(author)
    if maintenance == "batch-child":
        service.run_update_claim(service.claim_update(owner="worker"))
        observed = service.get_operator_operation(parent.id)
        assert observed.children[0].operation_id == issued
        assert observed.children[1].operation_id is not None
        assert observed.children[1].failure is None
    else:
        cancelled = service.cancel_profile_preparation(
            revision_id, actor="operator", reason="Accepted profile was cancelled"
        )
        assert cancelled == (issued,)
        with sessions() as session:
            operation = session.get(Job, issued)
            assert operation is not None
            assert service._stored_cancellation(operation) is not None

    # Neither accepted maintenance nor completed cancellation may wedge a new
    # authorized request after the old author disappears.
    with sessions.begin() as session:
        session.add(User(subject="replacement-admin", role="administrator"))
    replacement = service.start(
        next(iter(recipes)),
        actor="replacement-admin",
        request_id=str(uuid4()),
    )
    assert replacement.id != issued
    assert replacement.state == "queued"
