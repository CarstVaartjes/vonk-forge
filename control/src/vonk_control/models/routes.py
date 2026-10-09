"""Models: routes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column
from vonk_agent_protocol import RoutePublicationState, machine_check

from ..model_primitives import Base


class RecipeRouteAuthority(Base):
    """Current database identity for atomic recipe route publications."""

    __tablename__ = "recipe_route_authorities"
    authority_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RoutePublication(Base):
    __tablename__ = "route_publications"
    __table_args__ = (
        CheckConstraint(
            machine_check(RoutePublicationState),
            name="ck_route_publications_state",
        ),
        CheckConstraint(
            "generation IS NULL OR generation >= 0",
            name="ck_route_publications_generation",
        ),
        CheckConstraint(
            "length(plan_digest) = 64",
            name="ck_route_publications_plan_digest_length",
        ),
        CheckConstraint(
            "route_digest IS NULL OR length(route_digest) = 64",
            name="ck_route_publications_route_digest_length",
        ),
        CheckConstraint(
            "litellm_digest IS NULL OR length(litellm_digest) = 64",
            name="ck_route_publications_litellm_digest_length",
        ),
        CheckConstraint(
            "bundle_digest IS NULL OR length(bundle_digest) = 64",
            name="ck_route_publications_bundle_digest_length",
        ),
        CheckConstraint(
            "activation_marker_digest IS NULL OR length(activation_marker_digest) = 64",
            name="ck_route_publications_activation_marker_digest_length",
        ),
    )
    authority_id: Mapped[str] = mapped_column(
        ForeignKey("recipe_route_authorities.authority_id", ondelete="CASCADE"),
        primary_key=True,
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    generation: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    plan_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    route_digest: Mapped[str | None] = mapped_column(String(64))
    litellm_digest: Mapped[str | None] = mapped_column(String(64))
    bundle_digest: Mapped[str | None] = mapped_column(String(64))
    activation_marker: Mapped[dict[str, object] | None] = mapped_column(JSON)
    activation_marker_digest: Mapped[str | None] = mapped_column(String(64))


class RoutePublicationOwner(Base):
    """Singleton authority for the one global LiteLLM activation marker."""

    __tablename__ = "route_publication_owner"
    __table_args__ = (
        CheckConstraint(
            "singleton_id = 1",
            name="ck_route_publication_owner_singleton",
        ),
        CheckConstraint(
            "owner_generation >= 0",
            name="ck_route_publication_owner_generation",
        ),
    )
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    authority_id: Mapped[str | None] = mapped_column(
        ForeignKey("recipe_route_authorities.authority_id", ondelete="SET NULL"),
        unique=True,
    )
    owner_generation: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
    )
    reconciliation_attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    reconciliation_next_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    reconciliation_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
