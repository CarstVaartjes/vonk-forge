"""An installation stays removable when its recipe revision can no longer be read.

Production (2026-10-13): a 178 GB installation on both Sparks whose stored
revision no longer parsed under the current recipe contract was refused by the
storage sweep every pass ("1 validation error for RecipeDefinition ...
input_value=None"), so its disk could never be freed. Removal works from the
installation's own record, never from the recipe document.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select, update
from vonk_control.models import (
    CatalogDocumentRevision,
    Job,
    RecipeInstallation,
    ResourceReservation,
)
from vonk_control.run_switch_contract import RunSwitchCleanupApplyRequest
from vonk_forge_contracts import RecipeDefinition

from .test_recipe_operations import installed_recipe, setup_services
from .test_run_switch_operations import (
    RecordingArtifactExecutor,
    _child_operation_id,
    _service,
)
from .test_unused_storage_collection import (
    _agents_succeed,
    _real_collector,
    _real_lifecycle,
    _required,
)


def _contract_moved_on(monkeypatch: pytest.MonkeyPatch, revision_id: str) -> None:
    """The stored document is intact, but the current contract refuses it."""

    del revision_id
    refuse = RecipeDefinition.model_validate

    def unreadable(*_args: object, **_kwargs: object) -> RecipeDefinition:
        return refuse({})  # a real ValidationError, as the contract raises

    monkeypatch.setattr(RecipeDefinition, "model_validate", unreadable)
    monkeypatch.setattr(RecipeDefinition, "model_validate_json", unreadable)


def _damaged_and_retired(sessions, revision_id: str) -> None:
    """The revision is no longer active and its stored document is damaged."""

    with sessions.begin() as session:
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision_id)
            .values(state="failed", document=["not", "a", "recipe"])
            .execution_options(synchronize_session=False)
        )


@pytest.mark.parametrize("unreadable", ["contract-moved-on", "damaged-and-retired"])
@pytest.mark.parametrize("sparks", [1, 2])
@pytest.mark.usefixtures("damaged_json_rows")
def test_installation_with_an_unreadable_recipe_revision_is_removed_and_its_disk_freed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unreadable: str, sparks: int
) -> None:
    sessions, service, installation_id, nodes = _real_lifecycle(tmp_path, nodes=sparks)
    with sessions() as session:
        revision_id = _required(
            session.get(RecipeInstallation, installation_id)
        ).recipe_revision_id
    if unreadable == "contract-moved-on":
        _contract_moved_on(monkeypatch, revision_id)
    else:
        _damaged_and_retired(sessions, revision_id)
    collector = _real_collector(sessions, service, nodes)

    result = collector.collect()

    assert result.installations == 1, result.kept
    with sessions() as session:
        uninstall = session.scalar(select(Job).where(Job.kind == "recipe.uninstall"))
        assert uninstall is not None
    _agents_succeed(sessions, service, uninstall.id)
    with sessions() as session:
        removed = _required(session.get(RecipeInstallation, installation_id))
        assert removed.state == "uninstalled"
        claims = session.scalars(
            select(ResourceReservation).where(
                ResourceReservation.owner_kind == "installation",
                ResourceReservation.owner_id == installation_id,
            )
        ).all()
        assert claims
        assert all(claim.state == "released" for claim in claims)


def test_operator_cleanup_of_an_installation_whose_recipe_no_longer_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run/Switch cleanup (operator, profile and reconcile) reads the recipe's
    role envelopes for its descriptive fit; an unreadable recipe refused the
    whole cleanup preview instead of only leaving the fit unknown."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installation = installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )
    service = _service(
        sessions, lifecycle._clock(), lifecycle, RecordingArtifactExecutor()
    )
    _contract_moved_on(monkeypatch, "")

    operation = service.apply_cleanup(
        RunSwitchCleanupApplyRequest(
            installation_id=installation.owner_id, request_key=str(uuid.uuid4())
        ),
        actor="admin",
    )
    assert service.tick() is True
    child_id = _child_operation_id(service.get(operation.operation_id))
    for node_id in nodes:
        lifecycle.record_node_result(child_id, node_id, succeeded=True, evidence={})
    for _ in range(4):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        service.tick()

    completed = service.get(operation.operation_id)
    assert completed.state == "succeeded", completed.status_reason
    with sessions() as session:
        row = _required(session.get(RecipeInstallation, installation.owner_id))
        assert row.state == "uninstalled"
