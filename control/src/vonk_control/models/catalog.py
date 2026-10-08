"""Models: catalog."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    event,
    inspect,
    literal_column,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from ..exact_integer_storage import DecimalIntegerToken, ExactNonnegativeInteger
from ..model_primitives import Base, _lower_hex


class RecipeSourceBundle(Base):
    __tablename__ = "recipe_source_bundles"
    __table_args__ = (
        CheckConstraint(
            _lower_hex("sha256", 64), name="ck_recipe_source_bundle_digest"
        ),
        CheckConstraint(
            "archive_bytes > 0 AND total_bytes >= 0 AND file_count >= 1",
            name="ck_recipe_source_bundle_sizes",
        ),
    )
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    media_type: Mapped[str] = mapped_column(String(96), nullable=False)
    archive_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    manifest: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class SourceBundleArchive(Base):
    """Content-addressed source archive bytes stored in PostgreSQL."""

    __tablename__ = "source_bundle_archives"
    sha256: Mapped[str] = mapped_column(
        ForeignKey("recipe_source_bundles.sha256", ondelete="CASCADE"),
        primary_key=True,
    )
    archive: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class CatalogDocument(Base):
    """Stable lookup identity for one canonical Model or Recipe document."""

    __tablename__ = "catalog_documents"
    __table_args__ = (
        UniqueConstraint(
            "kind", "publisher", "slug", name="uq_catalog_document_identity"
        ),
        UniqueConstraint(
            "id", "kind", "publisher", "slug", name="uq_catalog_document_root_target"
        ),
        CheckConstraint("kind IN ('model','recipe')", name="ck_catalog_document_kind"),
        CheckConstraint(
            "publisher = lower(publisher) AND length(publisher) BETWEEN 2 AND 63",
            name="ck_catalog_document_publisher",
        ),
        CheckConstraint(
            "slug = lower(slug) AND length(slug) BETWEEN 2 AND 63",
            name="ck_catalog_document_slug",
        ),
        CheckConstraint(
            "length(title) BETWEEN 1 AND 120", name="ck_catalog_document_title"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    publisher: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )


class CatalogDocumentRevision(Base):
    """Validated canonical document and derived query projections."""

    __tablename__ = "catalog_document_revisions"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "revision_number", name="uq_catalog_document_revision_number"
        ),
        UniqueConstraint(
            "kind",
            "publisher",
            "slug",
            "content_digest",
            name="uq_catalog_document_revision_identity_digest",
        ),
        UniqueConstraint(
            "id",
            "kind",
            "publisher",
            "slug",
            "content_digest",
            name="uq_catalog_document_revision_fk_target",
        ),
        UniqueConstraint("id", "kind", name="uq_catalog_document_revision_kind"),
        ForeignKeyConstraint(
            ["document_id", "kind", "publisher", "slug"],
            [
                "catalog_documents.id",
                "catalog_documents.kind",
                "catalog_documents.publisher",
                "catalog_documents.slug",
            ],
            name="fk_catalog_document_revision_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "revision_number >= 1", name="ck_catalog_document_revision_number"
        ),
        CheckConstraint(
            "schema_version = 2", name="ck_catalog_document_revision_schema"
        ),
        CheckConstraint(
            "state IN ('candidate','active','failed')",
            name="ck_catalog_document_revision_state",
        ),
        CheckConstraint(
            "kind IN ('model','recipe')", name="ck_catalog_document_revision_kind"
        ),
        CheckConstraint(
            _lower_hex("content_digest", 64), name="ck_catalog_document_revision_digest"
        ),
        CheckConstraint(
            literal_column("download_bytes").is_(None)
            | DecimalIntegerToken(literal_column("download_bytes")),
            name="ck_catalog_document_revision_download_bytes",
        ),
        CheckConstraint(
            literal_column("installed_bytes").is_(None)
            | DecimalIntegerToken(literal_column("installed_bytes")),
            name="ck_catalog_document_revision_installed_bytes",
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_documents.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    publisher: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(63), nullable=False, index=True)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    document: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    artifact_key: Mapped[str | None] = mapped_column(String(64), index=True)
    execution_key: Mapped[str | None] = mapped_column(String(64), index=True)
    download_bytes: Mapped[int | None] = mapped_column(ExactNonnegativeInteger())
    installed_bytes: Mapped[int | None] = mapped_column(ExactNonnegativeInteger())
    projected: Mapped[dict[str, object]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_by: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    @property
    def entity_id(self) -> str:
        return self.document_id


class CatalogDocumentHead(Base):
    """Atomic active/candidate pointer; a failed candidate cannot replace active."""

    __tablename__ = "catalog_document_heads"
    __table_args__ = (
        UniqueConstraint(
            "kind", "publisher", "slug", name="uq_catalog_document_head_identity"
        ),
        ForeignKeyConstraint(
            ["kind", "publisher", "slug"],
            [
                "catalog_documents.kind",
                "catalog_documents.publisher",
                "catalog_documents.slug",
            ],
            name="fk_catalog_document_head_identity",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "kind IN ('model','recipe')", name="ck_catalog_document_head_kind"
        ),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    publisher: Mapped[str] = mapped_column(String(63), nullable=False)
    slug: Mapped[str] = mapped_column(String(63), nullable=False)
    active_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_document_revisions.id", ondelete="RESTRICT"), index=True
    )
    candidate_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey("catalog_document_revisions.id", ondelete="RESTRICT"), index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class CatalogRecipeModelReference(Base):
    """Exact recipe-to-model revision binding enforced by composite foreign keys."""

    __tablename__ = "catalog_recipe_model_references"
    __table_args__ = (
        UniqueConstraint(
            "recipe_revision_id",
            "selection_id",
            name="uq_catalog_recipe_model_selection",
        ),
        ForeignKeyConstraint(
            ["recipe_revision_id", "recipe_kind"],
            ["catalog_document_revisions.id", "catalog_document_revisions.kind"],
            name="fk_catalog_recipe_revision_kind",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            [
                "model_revision_id",
                "model_kind",
                "model_publisher",
                "model_slug",
                "model_content_digest",
            ],
            [
                "catalog_document_revisions.id",
                "catalog_document_revisions.kind",
                "catalog_document_revisions.publisher",
                "catalog_document_revisions.slug",
                "catalog_document_revisions.content_digest",
            ],
            name="fk_catalog_recipe_model_exact_revision",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "recipe_kind = 'recipe'", name="ck_catalog_recipe_revision_kind"
        ),
        CheckConstraint("model_kind = 'model'", name="ck_catalog_recipe_model_kind"),
    )
    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    recipe_revision_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    recipe_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="recipe"
    )
    selection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    model_revision_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True
    )
    model_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    model_publisher: Mapped[str] = mapped_column(String(63), nullable=False)
    model_slug: Mapped[str] = mapped_column(String(63), nullable=False)
    model_content_digest: Mapped[str] = mapped_column(String(64), nullable=False)


@event.listens_for(CatalogDocumentRevision, "before_update")
def _catalog_document_revision_is_immutable(
    _mapper, _connection, target: CatalogDocumentRevision
) -> None:
    state = inspect(target)
    changed = {
        attribute.key for attribute in state.attrs if attribute.history.has_changes()
    }
    previous_state = state.attrs.state.history.deleted
    if previous_state and previous_state[0] == "active":
        raise ValueError("active catalog document revisions are immutable")
    if target.state == "active" and (
        not previous_state
        or changed - {"state", "artifact_key", "execution_key", "projected"}
    ):
        raise ValueError("active catalog document revisions are immutable")


@event.listens_for(CatalogDocumentRevision, "before_delete")
def _active_catalog_document_revision_cannot_be_deleted(
    _mapper, _connection, target: CatalogDocumentRevision
) -> None:
    if target.state == "active":
        raise ValueError("active catalog document revisions are immutable")


@event.listens_for(Session, "before_commit")
def _active_catalog_document_json_is_immutable(session: Session) -> None:
    for value in session.identity_map.values():
        if isinstance(value, CatalogDocumentRevision) and value.state == "active":
            payload = json.dumps(
                value.document,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            if hashlib.sha256(payload).hexdigest() != value.content_digest:
                raise ValueError("active catalog document revisions are immutable")
