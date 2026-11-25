"""Models: gateway external models, their credential bindings and the alias namespace."""

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

from vonk_agent_protocol.state_machines import ExternalPublicationState, machine_check
from vonk_control.gateway.external_contract import (
    ApprovedDestination,
    ExternalDeploymentSpec,
)
from vonk_control.lifecycle.evidence import Residue
from vonk_control.model_primitives import Base
from vonk_control.stored.json_values import ContractJSON


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
    #: The typed reason of ``blocked`` (the publisher owns its vocabulary).
    reason: Mapped[str | None] = mapped_column(String(64))
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


class GatewayAliasNamespace(Base):
    """The one row every alias decision locks, so two owners cannot take one name.

    An alias decides where a client's data goes; the lock turns "check, then
    write" into one decision across the profile, run and external owners.
    """

    __tablename__ = "gateway_alias_namespace"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1", name="ck_gateway_alias_namespace_singleton"
        ),
    )
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
