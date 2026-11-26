"""Models: gateway external models, and their credential bindings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import ClassVar

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column
from vonk_forge_contracts.endpoint_alias import ENDPOINT_ALIAS_MAX_LENGTH

from vonk_agent_protocol.state_machines import (
    ExternalBlockReason,
    ExternalPublicationState,
    machine_check,
)
from vonk_control.gateway.external_contract import (
    ApprovedDestination,
    ExternalDeploymentSpec,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.model_primitives import Base
from vonk_control.models.waits import wait_due_column, wait_evidence_column
from vonk_control.stored.json_values import ContractJSON
from vonk_control.waits.contract import WaitEvidence


class GatewayExternalDeployment(Base):
    """One external model: the operator's intent and what the gateway serves.

    ``desired_spec`` is what the next publication renders; ``applied_spec`` is
    what the last acknowledged generation serves, and the one a withdrawal
    removes.  The alias is unique and stays reserved until a withdrawal is
    confirmed and the row is deleted.
    """

    __tablename__ = "gateway_external_deployments"
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__: ClassVar = {"version_id_col": version}
    __table_args__ = (
        CheckConstraint(
            machine_check(ExternalPublicationState),
            name="ck_gateway_external_deployments_state",
        ),
        CheckConstraint(
            machine_check(ExternalBlockReason, column="reason"),
            name="ck_gateway_external_deployments_reason",
        ),
        CheckConstraint(
            "desired_revision >= 1 AND (applied_revision IS NULL OR applied_revision >= 1)",
            name="ck_gateway_external_deployments_revisions",
        ),
        CheckConstraint(
            "(applied_revision IS NULL) = (applied_spec IS NULL)",
            name="ck_gateway_external_deployments_applied_pair",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    alias: Mapped[str] = mapped_column(
        String(ENDPOINT_ALIAS_MAX_LENGTH), nullable=False, unique=True
    )
    desired_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    desired_spec: Mapped[ExternalDeploymentSpec | Residue] = mapped_column(
        ContractJSON[ExternalDeploymentSpec](__tablename__, "desired_spec"),
        nullable=False,
    )
    applied_revision: Mapped[int | None] = mapped_column(Integer)
    applied_spec: Mapped[ExternalDeploymentSpec | Residue | None] = mapped_column(
        ContractJSON[ExternalDeploymentSpec](
            __tablename__, "applied_spec", none_as_null=True
        ),
        nullable=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    removal_requested: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    state: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        default=ExternalPublicationState.PENDING.value,
        server_default=ExternalPublicationState.PENDING.value,
    )
    #: Consecutive failed starts of the desired content; an edit, an enable or an
    #: acknowledged generation resets it.
    start_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    #: Why the deployment is not serving (set with ``blocked``, kept while it is
    #: withdrawn because of it).
    reason: Mapped[str | None] = mapped_column(String(32))
    #: A blocked deployment is re-planned when ``next_action_at`` falls due.
    wait_evidence: Mapped[WaitEvidence | Residue | None] = wait_evidence_column(
        __tablename__
    )
    next_action_at: Mapped[datetime | None] = wait_due_column("next_action_at")
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class GatewayCredentialBinding(Base):
    """A credential binding: its name and where its secret may be sent.

    The secret itself lives in the NAS custody directory, never in this table.
    """

    __tablename__ = "gateway_credential_bindings"
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__: ClassVar = {"version_id_col": version}
    __table_args__ = (
        CheckConstraint(
            "revision >= 1", name="ck_gateway_credential_bindings_revision"
        ),
    )
    name: Mapped[str] = mapped_column(String(63), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    destinations: Mapped[list[ApprovedDestination] | Residue] = mapped_column(
        ContractJSON[list[ApprovedDestination]](__tablename__, "destinations"),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
