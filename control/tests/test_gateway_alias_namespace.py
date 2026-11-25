"""One model name, one owner: every local producer yields to an external model."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from vonk_control.gateway.alias_namespace import (
    AliasOwner,
    AliasTaken,
    alias_owner,
    claim_external_alias,
)
from vonk_control.gateway.external_contract import (
    ExternalDeploymentSpec,
    ExternalDestination,
)
from vonk_control.models.execution import RecipeRun
from vonk_control.models.gateway import GatewayExternalDeployment
from vonk_control.runs.admission import RunAdmissionService, RunPlanInvalid
from vonk_control.runs.route_name import rename_run

from .test_run_admission import setup as run_setup

EXTERNAL = "partner-large"


def _spec() -> ExternalDeploymentSpec:
    return ExternalDeploymentSpec(
        alias=EXTERNAL,
        litellm_params={"model": "openai/partner-large"},
        destination=ExternalDestination(origins=["provider:openai"]),
    )


def _deployment() -> GatewayExternalDeployment:
    now = datetime.now(UTC)
    return GatewayExternalDeployment(
        alias=EXTERNAL,
        desired_revision=1,
        desired_spec=_spec(),
        request_id=str(uuid4()),
        created_at=now,
        updated_at=now,
    )


def test_an_external_row_keeps_its_spec_and_owns_its_name(tmp_path) -> None:
    # Break caught: an external row that never reads back (or never reserves its
    # name) lets a local run take the name a client already reaches externally.
    sessions, _now, _node, _installation = run_setup(tmp_path)
    with sessions.begin() as session:
        claim_external_alias(session, EXTERNAL)
        session.add(_deployment())
    with sessions.begin() as session:
        row = session.scalars(select(GatewayExternalDeployment)).one()
        assert row.desired_spec == _spec()
        assert alias_owner(session, EXTERNAL) is AliasOwner.EXTERNAL
        with pytest.raises(AliasTaken):
            claim_external_alias(session, EXTERNAL)


def test_a_local_run_owns_its_name_and_yields_it_only_by_rename(tmp_path) -> None:
    sessions, now, _node, installation = run_setup(tmp_path)
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    run_id = service.accept_run(
        service.plan_run(installation, alias="qwen", now=now), actor="admin", now=now
    )
    with sessions.begin() as session:
        assert alias_owner(session, "qwen") is AliasOwner.LOCAL
        assert alias_owner(session, "free") is None
        with pytest.raises(AliasTaken):
            claim_external_alias(session, "qwen")
        claim_external_alias(session, "free")
        run = session.get(RecipeRun, run_id)
        assert run is not None
        assert rename_run(session, run, "renamed", now=now) is True


def test_run_creation_and_rename_refuse_a_name_an_external_model_owns(tmp_path) -> None:
    # Break caught: dropping the check from run creation or from rename_run lets
    # a profile or a start repoint a client's name at a Spark.
    sessions, now, _node, installation = run_setup(tmp_path)
    service = RunAdmissionService(
        sessions, inventory_max_age=300, memory_floor_bytes=50
    )
    with sessions.begin() as session:
        session.add(_deployment())
    with pytest.raises(RunPlanInvalid):
        service.accept_run(
            service.plan_run(installation, alias=EXTERNAL, now=now),
            actor="admin",
            now=now,
        )
    with sessions() as session:
        assert session.scalars(select(RecipeRun)).all() == []
    run_id = service.accept_run(
        service.plan_run(installation, alias="qwen", now=now), actor="admin", now=now
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, run_id)
        assert run is not None
        with pytest.raises(AliasTaken):
            rename_run(session, run, EXTERNAL, now=now)
        assert run.alias == "qwen"
