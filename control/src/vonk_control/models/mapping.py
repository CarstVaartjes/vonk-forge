"""Models: mapping."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Connection,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column
from vonk_agent_protocol import ClusterMappingCode

from ..model_primitives import Base, _lower_hex


class ClusterMapping(Base):
    __tablename__ = "cluster_mappings"
    __table_args__ = (
        CheckConstraint("generation >= 1", name="ck_cluster_mappings_generation"),
        CheckConstraint("node_count >= 1", name="ck_cluster_mappings_node_count"),
        CheckConstraint(
            "state IN ('planned','ready','stale')", name="ck_cluster_mappings_state"
        ),
        CheckConstraint(
            _lower_hex("placement_digest", 64),
            name="ck_cluster_mappings_placement_digest",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_revision_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_document_revisions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    topology_name: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    node_count: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    parameters: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    placement_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    endpoint_owner_node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class ClusterMappingNode(Base):
    __tablename__ = "cluster_mapping_nodes"
    __table_args__ = (
        UniqueConstraint("mapping_id", "node_id", name="uq_cluster_mapping_node"),
        UniqueConstraint("mapping_id", "rank", name="uq_cluster_mapping_rank"),
        CheckConstraint("rank >= 0", name="ck_cluster_mapping_nodes_rank"),
        CheckConstraint(
            "length(role) BETWEEN 1 AND 64", name="ck_cluster_mapping_nodes_role"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    mapping_id: Mapped[str] = mapped_column(
        ForeignKey("cluster_mappings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("agent_nodes.node_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    endpoint_owner: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


def _reject_ready_mapping_node_mutation(
    _mapper: object, connection: Connection, target: ClusterMappingNode
) -> None:
    state = connection.execute(
        select(ClusterMapping.state).where(ClusterMapping.id == target.mapping_id)
    ).scalar_one_or_none()
    if state == "ready":
        raise ValueError(ClusterMappingCode.READY_IMMUTABLE)


event.listen(ClusterMappingNode, "before_update", _reject_ready_mapping_node_mutation)
event.listen(ClusterMappingNode, "before_delete", _reject_ready_mapping_node_mutation)
